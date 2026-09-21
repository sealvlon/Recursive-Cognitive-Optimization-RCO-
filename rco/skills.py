"""Format-agnostic skill discovery (design section 11).

The core contract: a skill is a file in skills_dir matching the adapter's PATTERN that describes itself.
The adapter (named in rco.toml) exposes PATTERN and describe(path, data) -> dict with a non-empty "name",
or raises its Rejected(reason). The core stores {name, file, sha256, declared} and reads nothing but
"name". An adapter may also define brief(declared) -> str, the skill as a work instruction; the core puts that
text into a turn's packet unread. Mechanism: stdlib polling in a worker thread, no file watcher.
"""

import asyncio
import hashlib
import importlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

log = logging.getLogger("rco.skills")

# FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_OFFLINE: opening would start a OneDrive download.
CLOUD_ONLY = 0x400000 | 0x1000
LIST_CAP_BYTES = 16 * 1024


@dataclass(frozen=True)
class Skill:
    name: str
    file: str
    sha256: str
    declared: dict[str, Any]


@dataclass(frozen=True)
class Rejection:
    file: str
    sha256: str | None
    reason: str


# (event, record fields, console line or None)
Event = tuple[str, dict[str, Any], str | None]


def load_adapter(name: str) -> ModuleType:
    mod = importlib.import_module(name)
    if not isinstance(getattr(mod, "PATTERN", None), str) or not callable(getattr(mod, "describe", None)):
        raise ImportError(f"skill adapter {name} must define PATTERN (str) and describe(path, data)")
    return mod


def _stat(path: Path) -> os.stat_result:
    return os.stat(path)


def _read_bytes(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class SkillCatalog:
    def __init__(
        self,
        skills_dir: Path,
        adapter_name: str,
        *,
        settle_ms: int,
        deadline_ms: int,
        emit: Callable[[str, dict[str, Any], str | None], bool],
    ) -> None:
        """emit(event, fields, console_line) records the event and prints the line; False = not recorded."""
        self.dir = skills_dir
        self.adapter_name = adapter_name
        self.adapter = load_adapter(adapter_name)
        self.settle_ns = settle_ms * 1_000_000
        self.deadline_s = deadline_ms / 1000
        self._emit = emit
        self.skills: dict[str, Skill] = {}  # by file name
        self.rejected: dict[str, Rejection] = {}  # by file name
        self._reported: set[tuple[str, str]] = set()  # (file, sha256 or reason): rejections already recorded
        self._observed: dict[str, tuple[tuple[int, int], float]] = {}  # file -> ((size, mtime_ns), first seen)
        self._task: asyncio.Future[None] | None = None

    @property
    def count(self) -> int:
        return len(self.skills)

    def find(self, name: str) -> Skill | None:
        """The loaded skill with exactly this name, or None."""
        return next((s for s in self.skills.values() if s.name == name), None)

    def brief(self, skill: Skill, agent: str | None = None) -> str:
        """The adapter's optional brief(declared[, agent]): the text a packet carries for this skill and, when the
        turn names one, that agent of the skill. An adapter without it, or one that fails, gives the empty text
        (the packet then names the skill only)."""
        render = getattr(self.adapter, "brief", None)
        if not callable(render):
            return ""
        try:
            text = render(skill.declared) if agent is None else render(skill.declared, agent)
        except Exception:
            log.exception("skill adapter brief() failed for %s", skill.name)
            return ""
        return text if isinstance(text, str) else ""

    def agents(self, skill: Skill) -> list[str]:
        """The adapter's optional agents(declared): the named roles within the skill, for the operator panel."""
        names = getattr(self.adapter, "agents", None)
        if not callable(names):
            return []
        try:
            got = names(skill.declared)
        except Exception:
            log.exception("skill adapter agents() failed for %s", skill.name)
            return []
        return [n for n in got if isinstance(n, str)] if isinstance(got, list) else []

    async def rescan(self, deadline: bool = True) -> None:
        """Scan (after any scan already running, which may predate the caller) and wait up to scan_deadline_ms;
        past the deadline the caller answers from the last good catalog and the scan finishes in the background."""

        async def fresh() -> None:
            if self._task is not None and not self._task.done():
                await asyncio.shield(self._task)
            if self._task is None or self._task.done():
                self._task = asyncio.ensure_future(self._run())
            await asyncio.shield(self._task)

        try:
            await asyncio.wait_for(fresh(), self.deadline_s if deadline else None)
        except TimeoutError:
            log.info("skills scan passed its %s s deadline; answering from the last catalog", self.deadline_s)

    async def _run(self) -> None:
        try:
            result = await asyncio.to_thread(self._scan)
            self._apply(*result)
        except Exception:
            log.exception("skills scan failed; keeping the last catalog")

    def _scan(self) -> tuple[dict[str, Skill], dict[str, Rejection], list[Event]]:
        old = self.skills
        skills: dict[str, Skill] = {}
        rejected: dict[str, Rejection] = {}
        names: dict[str, str] = {}  # skill name -> file that owns it
        now_ns, mono = time.time_ns(), time.monotonic()
        try:
            files = sorted(p for p in self.dir.glob(self.adapter.PATTERN) if p.is_file())
        except OSError:
            files = []

        def keep_old(fname: str) -> None:
            if fname in old and old[fname].name not in names:
                skills[fname] = old[fname]
                names[old[fname].name] = fname

        for p in files:
            fname = p.name
            try:
                st = _stat(p)
            except OSError:
                continue  # vanished between glob and stat
            if getattr(st, "st_file_attributes", 0) & CLOUD_ONLY:
                rejected[fname] = Rejection(fname, None, "unreadable: cloud-only placeholder")
                keep_old(fname)
                continue
            key = (st.st_size, st.st_mtime_ns)
            if self._observed.get(fname, (None,))[0] != key:
                self._observed[fname] = (key, mono)
            stable_ns = (mono - self._observed[fname][1]) * 1e9
            if now_ns - st.st_mtime_ns < self.settle_ns and stable_ns < self.settle_ns:
                keep_old(fname)  # still being written: wait for settle_ms without a change
                if fname in self.rejected:
                    rejected[fname] = self.rejected[fname]
                continue
            try:
                data = _read_bytes(p)
            except OSError as e:
                rejected[fname] = Rejection(fname, None, f"unreadable: {e.strerror or e}")
                keep_old(fname)
                continue
            sha = hashlib.sha256(data).hexdigest()
            skill = old.get(fname)
            if skill is None or skill.sha256 != sha:
                try:
                    declared = self.adapter.describe(p, data)
                    name = declared.get("name") if isinstance(declared, dict) else None
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError("the adapter returned no non-empty 'name'")
                    skill = Skill(name, fname, sha, declared)
                except Exception as e:  # adapter exceptions become rejections; the hub keeps running
                    rejected_cls = getattr(self.adapter, "Rejected", ())
                    reason = str(e) if isinstance(e, rejected_cls) else f"adapter error: {type(e).__name__}: {e}"
                    rejected[fname] = Rejection(fname, sha, reason)
                    continue
            if skill.name in names:
                rejected[fname] = Rejection(fname, sha, f"duplicate name '{skill.name}' (already in {names[skill.name]})")
                continue
            names[skill.name] = fname
            skills[fname] = skill
        for fname in set(self._observed) - {p.name for p in files}:
            del self._observed[fname]

        events: list[Event] = []
        for fname, s in skills.items():
            if fname not in old:
                fields = {"name": s.name, "file": s.file, "sha256": s.sha256, "declared": s.declared}
                events.append(("skill_discovered", fields, f"[INFO] Skill discovered: {s.name} ({s.file})"))
            elif old[fname].sha256 != s.sha256:
                events.append(("skill_changed", {"name": s.name, "file": fname, "sha256": s.sha256}, None))
        for fname, s in old.items():
            if fname not in skills:
                events.append(("skill_removed", {"name": s.name, "file": fname}, None))
        for r in rejected.values():
            if (r.file, r.sha256 or r.reason) not in self._reported:
                fields = {"file": r.file, "sha256": r.sha256, "reason": r.reason}
                events.append(("skill_rejected", fields, f"[WARN] Skill file rejected: {r.file}: {r.reason}"))
        return skills, rejected, events

    def _apply(self, skills: dict[str, Skill], rejected: dict[str, Rejection], events: list[Event]) -> None:
        for event, fields, line in events:
            recorded = self._emit(event, fields, line)
            if event == "skill_discovered" and not recorded:
                skills.pop(fields["file"], None)  # an unrecorded discovery did not happen: retried next scan
            elif event == "skill_rejected" and recorded:
                self._reported.add((fields["file"], fields["sha256"] or fields["reason"]))
        self.skills, self.rejected = skills, rejected

    def listing(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter_name,
            "count": self.count,
            "skills": [
                {"name": s.name, "file": s.file, "sha256": s.sha256, "declared": s.declared}
                for s in sorted(self.skills.values(), key=lambda s: s.name)
            ],
            "rejected": [{"file": r.file, "reason": r.reason} for r in sorted(self.rejected.values(), key=lambda r: r.file)],
            "truncated": False,
        }

    def listing_json(self) -> str:
        """The list_skills payload: the same bytes for every caller, capped at 16 KB."""
        doc = self.listing()
        text = json.dumps(doc, ensure_ascii=False, default=str)
        while len(text.encode("utf-8")) > LIST_CAP_BYTES and (doc["skills"] or doc["rejected"]):
            (doc["rejected"] or doc["skills"]).pop()
            doc["truncated"] = True
            text = json.dumps(doc, ensure_ascii=False, default=str)
        return text
