"""Append-only JSONL run record (design section 6).

One json line per event, UTF-8, \\n line ends, fsynced; the file is never seeked, truncated or rewritten.
The writer rejects unknown events and missing fields.

The file is written as unbuffered bytes on an O_APPEND descriptor, not through a buffered text handle: a
buffered handle keeps the bytes of a failed flush and writes them on the next successful one, so a refused
action would reach the record later, with a duplicate seq (OC-22: an unrecorded registration did not
happen). A completed write is the commit point: seq advances and the caller applies its effect. A write
that fails part-way leaves a fragment, so the next record starts on a fresh line and JSONL readers skip
only the fragment.

Stage 1 (Stage 1 design 7, 8): the record is also the commit log of runs and turns. read_events reads it back
at start, and run and turn state is rebuilt by replaying it.
"""

import datetime as dt
import json
import logging
import os
import threading
from collections.abc import Collection
from pathlib import Path
from typing import Any

_PRESENCE = ("client", "last_seen_age_s", "open_requests", "threshold_s")
_REGISTRATION = ("client", "handle", "registration_id", "name_arg", "era", "client_info", "session", "skill_count")

SCHEMA: dict[str, tuple[str, ...]] = {
    "server_start": (
        "host", "port", "url", "pid", "python_exe", "python_version", "mcp_version", "config_path",
        "config_sha256", "registry_path", "registry_sha256", "data_dir", "data_dir_realpath", "package_identity",
        "canary_writable",
        "token_sources", "staleness_s", "session_idle_timeout",
    ),
    "server_start_failed": ("reason", "port", "config_key"),
    "server_stop": ("reason", "uptime_s"),
    "client_registered": _REGISTRATION,
    "client_re_registered": _REGISTRATION + ("count", "first_registered_at"),
    "client_register_rejected": ("client", "name_arg", "reason"),
    "identity_hint_mismatch": ("client", "client_info", "expected_hints"),
    "auth_rejected": ("reason", "count_minute"),
    "hub_ready": ("handles",),
    "client_missing": _PRESENCE,
    "client_stale": _PRESENCE,
    "client_active_again": _PRESENCE,
    "skill_discovered": ("name", "file", "sha256", "declared"),
    "skill_changed": ("name", "file"),
    "skill_removed": ("name", "file"),
    "skill_rejected": ("file", "sha256", "reason"),
    "operator_command": ("command", "args", "result"),
    "hub_paused": ("by",),
    "hub_resumed": ("by",),
    # Runs and turns (Stage 1 design 8.1). Every listed field is present, null where it does not apply. Every turn_*
    # event carries the handoff block {mode, wait_requested_s, wait_cap_s, waits, waited_s, fetches}.
    "run_proposed": ("proposal_id", "client", "handle", "task", "task_sha256", "task_chars"),
    "run_opened": (
        "run_id", "task", "task_sha256", "task_chars", "task_source", "task_file", "proposal_id", "dropped_proposal",
    ),
    "run_note": ("run_id", "text"),  # plus turn_id for a note on one turn (note T<k> ...)
    "run_ended": (
        "run_id", "outcome", "turns_submitted", "turns_recalled", "turns_by_client", "turns_done_by_client",
        "handles_used", "two_model", "open_s", "clock_suspect", "handoff_totals", "final_edits",
    ),
    "turn_offered": (
        "run_id", "turn_id", "step", "client", "handle", "role", "note", "inputs", "packet_file", "packet_sha256",
        "packet_chars", "code_required", "handoff",
    ),
    "turn_leased": ("run_id", "turn_id", "client", "offer_id", "lease_s", "prior_lease_lapsed", "handoff"),  # dormant
    "turn_claimed": (
        "run_id", "turn_id", "client", "handle", "via", "nonce_sha256", "offered_for_s", "era", "session",
        "client_info", "code_required", "code_given", "handoff",
    ),
    "turn_submitted": (
        "run_id", "turn_id", "client", "handle", "status", "output", "output_sha256", "output_chars", "output_file",
        "files_changed", "claimed_for_s", "handoff",
    ),
    "turn_submit_duplicate": ("run_id", "turn_id", "client", "output_sha256", "handoff"),
    "turn_recalled": ("run_id", "turn_id", "client", "prior_state", "age_s", "needs_stop_confirm", "handoff"),
    "turn_stop_confirmed": ("run_id", "turn_id", "client", "by", "handoff"),
    "turn_refused": (
        "source", "client", "reason", "run_id", "turn_id", "detail", "output_sha256", "output_chars", "late_file",
        "code_given", "handoff",
    ),
    # One per operator `next`, written after turn_offered with the extra fields turn_id and handoff (Stage 1 design
    # 8.1, 8.2). The M1 hub never emitted it.
    "routing_decision": (
        "run_id", "step", "round", "skill", "model", "role", "alternatives", "reason", "decided_by", "bounds_ref",
    ),
}


class SchemaError(ValueError):
    """An event that does not match SCHEMA (a programming error, never written)."""


class RecordWriteError(OSError):
    """The record file could not be appended to (policy OC-22 decides what happens next)."""


class RecordReadError(OSError):
    """The record file exists but cannot be read; the hub then refuses to start (Stage 1 design 7.3)."""


def validate(event: str, fields: dict[str, Any]) -> None:
    required = SCHEMA.get(event)
    if required is None:
        raise SchemaError(f"unknown record event '{event}'")
    missing = [f for f in required if f not in fields]
    if missing:
        raise SchemaError(f"record event '{event}' is missing {', '.join(missing)}")
    if event == "auth_rejected" and fields["reason"] == "placeholder" and not fields.get("env_var"):
        raise SchemaError("auth_rejected with reason 'placeholder' needs env_var")
    if event == "routing_decision":
        _validate_routing(fields)


def _validate_routing(f: dict[str, Any]) -> None:
    def need(ok: bool, what: str) -> None:
        if not ok:
            raise SchemaError(f"routing_decision: {what}")

    skill = f["skill"]
    need(isinstance(skill, dict) and {"name", "sha256"} <= skill.keys(), "skill must be {name, sha256}")
    need(isinstance(f["model"], list) and all(isinstance(m, str) for m in f["model"]), "model must be a list of handles")
    need(f["role"] is None or isinstance(f["role"], str), "role must be text or null")
    need(isinstance(f["decided_by"], str), "decided_by must be text")
    need(f["bounds_ref"] is None or isinstance(f["bounds_ref"], str), "bounds_ref must be text or null")
    alts = f["alternatives"]
    need(
        isinstance(alts, list) and all(isinstance(a, dict) and {"skill", "model", "why_not"} <= a.keys() for a in alts),
        "alternatives must be a list of {skill, model, why_not}",
    )


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


log = logging.getLogger("rco.records")

_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no \n -> \r\n translation


def read_events(path: Path, events: Collection[str] | None = None) -> list[dict[str, Any]]:
    """The records in `path`, whole (envelope included), in file order: seq restarts with every hub run.

    The file is read as bytes and split on \\n. A line that is not UTF-8, not a JSON object, not v 1 or without an
    event name is skipped, so a torn fragment loses only itself. Every line is parsed; `events`, if given, selects
    on rec["event"] (no substring pre-filter). A missing file is an empty history; a file that exists but cannot be
    read raises RecordReadError, so the hub never starts with empty run state over an unreadable history.
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return []
    except OSError as e:
        raise RecordReadError(e.errno, e.strerror or str(e), str(path)) from e
    out: list[dict[str, Any]] = []
    skipped = 0
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line.decode("utf-8"))  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        except ValueError:
            skipped += 1
            continue
        v = rec.get("v") if isinstance(rec, dict) else None
        if isinstance(v, bool) or v != 1 or not isinstance(rec.get("event"), str):
            skipped += 1
            continue
        if events is None or rec["event"] in events:
            out.append(rec)
    if skipped:
        log.warning("%s: skipped %d record line(s) that are torn, not JSON or not v 1", path, skipped)
    return out


class Records:
    def __init__(self, path: Path, hub_run: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.hub_run = hub_run
        self._lock = threading.Lock()
        self._seq = 0
        self._torn = False  # a failed write left a fragment: the next record starts with "\n"
        self._fd: int | None = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY, 0o644)

    @property
    def closed(self) -> bool:
        return self._fd is None

    def write(self, event: str, **fields: Any) -> dict[str, Any]:
        validate(event, fields)
        with self._lock:
            rec = {"v": 1, "ts": utc_now(), "seq": self._seq + 1, "hub_run": self.hub_run, "event": event, **fields}
            line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
            try:
                self._append(line)
            except OSError as e:
                raise RecordWriteError(e.errno, f"cannot append to {self.path}: {e}") from e
            self._seq += 1
        return rec

    def _append(self, line: str) -> None:
        """Write every byte or raise; nothing is held back for a later write."""
        if self._fd is None:
            raise OSError(9, "the record file is closed")
        data = memoryview((b"\n" if self._torn else b"") + line.encode("utf-8"))
        written = 0
        try:
            while written < len(data):
                n = os.write(self._fd, data[written:])
                if n <= 0:
                    raise OSError(28, "short write to the record file")
                written += n
        except OSError:
            if written:
                self._torn = True
            raise
        self._torn = False
        try:
            os.fsync(self._fd)
        except OSError as e:
            # The line is in the file (readers see it), so it counts as written; only durability is in doubt.
            log.warning("fsync of %s failed after a complete write: %s", self.path, e)

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                fd, self._fd = self._fd, None
                os.close(fd)
