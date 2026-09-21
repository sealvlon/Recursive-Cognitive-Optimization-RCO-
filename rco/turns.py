"""Turn engine (Stage 1 design sections 4, 5, 7, 8, 9): run and turn state, pure checks, packets and replay.

No I/O, no clock, no client literals. The hub passes in the time (At: a UTC ts and hub.mono), the registry clients,
the config values and a reader for file bytes; the engine answers every action with a Refusal (reason, texts, the
turn_refused fields) or a Plan (the files to stage, the record that commits it, the console lines, the tool result).
The hub then runs the effect order of design 5.6: stage files -> write the record (the commit point) ->
book.commit(plan, rec, mono) -> print -> return. The same apply serves the live path and replay (fold), so a
replayed TurnBook equals the live one apart from the memory-only fields (handoff counters, leases, codes, monotonic
times), which are excluded from comparison.
"""

import datetime as dt
import hashlib
import hmac
import math
import os
import re
import secrets
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

STATUSES = ("done", "blocked", "declined")
LIVE = ("offered", "leased", "claimed", "broken")
ROLE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}(?::[A-Za-z][A-Za-z0-9_-]{0,31})?")  # role, or <skill>:<agent>
TURN_REF_RE = re.compile(r"(?:R(\d+)-)?T(\d+)", re.IGNORECASE)
NONCE_HEX = 16  # secrets.token_hex(8): 64 bits
SUSPECT_SPAN_S = 7 * 24 * 3600  # a ts span over 7 days (or negative) is a wall-clock step (7.6)
WAITABLE = frozenset({"no_run", "not_yours"})  # at an effective wait > 0 these wait for a turn; others return
SUMMARY_CHARS = 70
DECIDE = "next <client> <role> [+T<n>...] [-- note]"
DECIDE_LINE = f"[TURN] Decide: {DECIDE} | end [outcome]"
NEXT_USAGE = "Usage: next <client> <role> [+T<k>...|+none] [-- note]"
RECEIVED_TAIL = "The operator decides the next step at the hub console."
PACKET_OPEN, PACKET_CLOSE = "----- packet -----", "----- end of packet -----"


class ClientLike(Protocol):
    """The registry fields the engine reads (registry.Client once it has the four turn fields, design 6.1)."""

    id: str
    display_name: str
    handle: str
    turn_hint: str
    turn_wait_max_s: float
    turn_offer_lease_s: float


@dataclass(frozen=True)
class At:
    """The time of an action: ts = UTC as records.utc_now() writes it; mono = hub.mono() (durations in one hub run)."""

    ts: str
    mono: float


@dataclass(frozen=True)
class Gate:
    """hub.quorum()['waiting'] and the invocation_hint of each client in it (empty = quorum_ok)."""

    waiting: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class Caller:
    """Diagnostics of the calling session for turn_claimed (never identity): era, 8-char session prefix, clientInfo."""

    era: str | None = None
    session: str | None = None
    client_info: dict[str, Any] | None = None


# ------------------------------------------------------------------------------------------ text and time


def sanitize_line(text: str) -> str:
    """Model or file text made safe for one console line (5.8): control characters (C0 other than tab and newline,
    DEL, C1) become '?', whitespace runs collapse to one space, and the result is cut to 70 characters + '...'."""
    out = "".join("?" if (ord(ch) < 32 and ch not in "\t\n") or 127 <= ord(ch) <= 159 else ch for ch in text)
    out = " ".join(out.split())
    return out if len(out) <= SUMMARY_CHARS else out[:SUMMARY_CHARS] + "..."


def summary(text: str) -> str:
    """The first non-empty line of an output or task, sanitised (outputs start with a one-line summary)."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return sanitize_line(first)


def normalise(data: bytes) -> str:
    """File bytes -> text as 9.1 compares it: UTF-8 (raises UnicodeDecodeError), BOM stripped, CRLF -> LF."""
    return data.removeprefix(b"\xef\xbb\xbf").decode("utf-8").replace("\r\n", "\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mint_nonce() -> str:
    return secrets.token_hex(NONCE_HEX // 2)


def nonce_hash(turn_id: str, nonce: str) -> str:
    return hashlib.sha256(f"{turn_id}:{nonce}".encode("utf-8")).hexdigest()


def nonce_ok(turn_id: str, nonce: str, stored: str | None) -> bool:
    return stored is not None and hmac.compare_digest(nonce_hash(turn_id, nonce), stored)


def mint_offer() -> str:
    return secrets.token_hex(4)


def mint_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


def parse_ts(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def span(start_ts: str, end_ts: str) -> tuple[float, bool]:
    """A duration across hub runs from UTC ts (7.6): clamped at 0; suspect when negative or over 7 days."""
    raw = (parse_ts(end_ts) - parse_ts(start_ts)).total_seconds()
    return max(raw, 0.0), raw < 0 or raw > SUSPECT_SPAN_S


def elapsed(start_ts: str, start_mono: float | None, at: At) -> tuple[float, bool]:
    """Inside one hub run from hub.mono; for a restored start (no mono) from the ts span."""
    if start_mono is not None:
        return max(at.mono - start_mono, 0.0), False
    return span(start_ts, at.ts)


def show_time(ts: str, now_ts: str) -> str:
    """Local HH:MM:SS, with the date when it is not today (7.6)."""
    local, now = parse_ts(ts).astimezone(), parse_ts(now_ts).astimezone()
    return local.strftime("%H:%M:%S") if local.date() == now.date() else local.strftime("%Y-%m-%d %H:%M:%S")


def ago(seconds: float) -> str:
    return f"{int(seconds // 60)} min" if seconds >= 60 else f"{int(seconds)} s"


def _secs(v: float) -> str:
    return f"{v:g} s"


def names_data_folder(text: str, data_dir: str) -> bool:
    """9.3: the text names the hub data folder, case-insensitively, in either slash form."""
    base = data_dir.rstrip("\\/").casefold()
    low = text.casefold()
    return any(form in low for form in {base.replace("/", "\\"), base.replace("\\", "/")})


def parse_turn_ref(ref: str) -> tuple[int | None, int] | None:
    """'T<k>' or 'R<n>-T<k>' (case-insensitive) -> (n or None, k); anything else -> None."""
    m = TURN_REF_RE.fullmatch(ref.strip())
    return (int(m.group(1)) if m.group(1) else None, int(m.group(2))) if m else None


def claim_text(turn_id: str, nonce: str, role: str, output_cap: int, packet: str) -> str:
    return "\n".join([
        "RCO turn: claimed", f"turn_id: {turn_id}", f"nonce: {nonce}", f"role: {role}",
        f"output limit: {output_cap} characters", PACKET_OPEN, packet, PACKET_CLOSE,
    ])


def offer_text(turn_id: str, offer_id: str, lease_s: float) -> str:
    return "\n".join(["RCO turn: offer", f"turn_id: {turn_id}", f"offer: {offer_id}", f"lease: {_secs(lease_s)}"])


def received_text(turn_id: str, chars: int, status: str) -> str:
    lines = ["RCO turn: received", f"turn_id: {turn_id}", f"characters: {chars}"]
    if status != "done":
        lines.append(f"status: {status}")
    return "\n".join(lines + [RECEIVED_TAIL])


def duplicate_text(turn_id: str, when: str) -> str:
    return "\n".join([
        "RCO turn: duplicate", f"turn_id: {turn_id}", f"This same output was already received at {when}; nothing changed.",
    ])


def proposed_text(proposal_id: str) -> str:
    return f"RCO run: proposed {proposal_id}\nThe operator opens it at the hub console."


def refused_text(reason: str, detail: str, kind: str = "turn") -> str:
    return f"RCO {kind}: refused {reason}\n{detail}"


def saved_line(path: str) -> str:
    """The Saved line of an accepted submit; the hub replaces it with [ERROR] Cannot save <path>: <error> when
    the output view cannot be written (7.5)."""
    return f"[TURN] Saved: {path}"


# ------------------------------------------------------------------------------------------ state


@dataclass
class Handoff:
    """The handoff block of every turn_* event (8.1). Memory only, per hub run."""

    mode: str = "operator_nudge"
    wait_requested_s: float | None = None
    wait_cap_s: float = 0
    waits: int = 0
    waited_s: float = 0
    fetches: int = 0

    def block(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "wait_requested_s": self.wait_requested_s, "wait_cap_s": self.wait_cap_s,
            "waits": self.waits, "waited_s": round(self.waited_s, 3), "fetches": self.fetches,
        }


@dataclass
class Proposal:
    id: str
    client: str
    handle: str
    task: str
    ts: str


@dataclass
class Turn:
    run_id: str
    k: int
    client: str
    handle: str
    role: str
    note: str | None
    inputs: list[dict[str, Any]]
    packet_file: str
    packet_sha256: str
    packet_chars: int
    offered_ts: str
    state: str = "offered"  # offered | leased | claimed | submitted | recalled | broken
    nonce_sha256: str | None = None
    claimed_ts: str | None = None
    status: str | None = None
    output: str | None = None
    output_sha256: str | None = None
    output_file: str | None = None
    submitted_ts: str | None = None
    recalled_ts: str | None = None
    prior_state: str | None = None
    skill: str | None = None  # the loaded skill whose name was the turn's role, if any
    # memory only (never replayed, never compared)
    offered_mono: float | None = field(default=None, compare=False)
    claimed_mono: float | None = field(default=None, compare=False)
    offer_id: str | None = field(default=None, compare=False)
    lease_until: float | None = field(default=None, compare=False)
    lease_lapsed: bool = field(default=False, compare=False)
    code: str | None = field(default=None, compare=False)
    reminded: set[str] = field(default_factory=set, compare=False)
    handoff: Handoff = field(default_factory=Handoff, compare=False)

    @property
    def id(self) -> str:
        return f"{self.run_id}-T{self.k}"

    @property
    def live(self) -> bool:
        return self.state in LIVE


@dataclass
class Run:
    id: str
    task: str
    opened_ts: str
    turns: dict[int, Turn] = field(default_factory=dict)
    pause_reasons: list[str] = field(default_factory=list)
    stop_pending: str | None = None  # turn id recalled while claimed, waiting for 'stopped'
    suspect_lines: int = 0
    suspect_turns: set[str] = field(default_factory=set)

    @property
    def live_turn(self) -> Turn | None:
        return next((t for t in self.turns.values() if t.live), None)

    def turns_in(self, *states: str) -> list[Turn]:
        return [t for t in self.turns.values() if t.state in states]


@dataclass
class Refusal:
    """An action the engine refuses. Tool path: `text` is the result (is_error=True), `lines` the console [WARN] line
    (empty = record only), `fields` the turn_refused record (None = nothing to record), `files` the late copies to
    write before that record. Console path: `lines` is the reply and `result` what operator_command records."""

    reason: str
    detail: str = ""
    text: str = ""
    lines: list[str] = field(default_factory=list)
    fields: dict[str, Any] | None = None
    files: list[tuple[str, str]] = field(default_factory=list)
    result: str = ""


@dataclass
class Plan:
    """An allowed action. The hub creates `mkdir` (exclusive) and writes `files` (atomic) first, then the record
    `event`/`fields` (the commit point), then book.commit(plan, rec, mono), then the records in `after`, then the
    `views` (atomic; see saved_line), then prints `lines` and returns `text`."""

    event: str
    fields: dict[str, Any]
    text: str = ""
    lines: list[str] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)
    views: list[tuple[str, str]] = field(default_factory=list)
    after: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    mkdir: str | None = None
    result: str = "ok"
    code: str | None = None  # memory only: the entry code of an offered turn (code mode)


FOLDED = frozenset({
    "run_proposed", "run_opened", "run_ended", "turn_offered", "turn_claimed", "turn_submitted", "turn_recalled",
    "turn_stop_confirmed", "hub_resumed",
})


def _num(ident: str | None, prefix: str) -> int:
    m = re.fullmatch(prefix + r"(\d+)", ident or "")
    return int(m.group(1)) if m else 0


@dataclass
class TurnBook:
    """All run and turn state. One open run, one live turn, at most one pending proposal (5.1)."""

    clients: dict[str, Any] = field(default_factory=dict, compare=False)  # id -> ClientLike, registry order
    runs_dir: str = field(default="", compare=False)  # <data_dir>\records\runs
    data_dir: str = field(default="", compare=False)
    packet_cap: int = field(default=24000, compare=False)
    output_cap: int = field(default=12000, compare=False)
    run: Run | None = None
    proposal: Proposal | None = None
    max_run: int = 0
    max_proposal: int = 0
    skipped: int = field(default=0, compare=False)  # inconsistent lines skipped at replay outside any run

    @classmethod
    def create(
        cls, clients: Iterable[Any], runs_dir: str, data_dir: str, packet_cap: int, output_cap: int
    ) -> "TurnBook":
        return cls(
            clients={c.id: c for c in clients}, runs_dir=runs_dir, data_dir=data_dir, packet_cap=packet_cap,
            output_cap=output_cap,
        )

    # ------------------------------------------------------------------ ids, paths, lookups

    def next_run_id(self, folder_names: Iterable[str] = ()) -> str:
        """R<n>: 1 + the highest in the records and among the folder names under records\\runs (5.1)."""
        return f"R{max([self.max_run, *(_num(n, 'R') for n in folder_names)]) + 1}"

    def next_proposal_id(self) -> str:
        return f"P{self.max_proposal + 1}"

    def run_dir(self, run_id: str) -> str:
        return os.path.join(self.runs_dir, run_id)

    def packet_path(self, run_id: str, k: int) -> str:
        return os.path.join(self.runs_dir, run_id, f"T{k}-packet.md")

    def output_path(self, run_id: str, k: int) -> str:
        return os.path.join(self.runs_dir, run_id, f"T{k}-output.md")

    def late_path(self, run_id: str, k: int, output_sha256: str) -> str:
        return os.path.join(self.runs_dir, run_id, "late", f"T{k}-{output_sha256[:8]}.md")

    def name(self, client_id: str) -> str:
        c = self.clients.get(client_id)
        return c.display_name if c is not None else client_id

    @property
    def live_turn(self) -> Turn | None:
        return self.run.live_turn if self.run is not None else None

    def find(self, turn_id: str) -> Turn | None:
        """A full turn id (R<n>-T<k>) of the open run -> the turn, or None."""
        ref = parse_turn_ref(turn_id) if isinstance(turn_id, str) else None
        if self.run is None or ref is None or ref[0] is None or f"R{ref[0]}" != self.run.id:
            return None
        return self.run.turns.get(ref[1])

    def resolve(self, ref: str) -> Turn | None:
        """A console <turn> (T<k> or R<n>-T<k>, R<n> = the open run) -> the turn, or None."""
        parsed = parse_turn_ref(ref)
        if self.run is None or parsed is None or (parsed[0] is not None and f"R{parsed[0]}" != self.run.id):
            return None
        return self.run.turns.get(parsed[1])

    # ------------------------------------------------------------------ memory-only counters and leases

    def count_fetch(self, client_id: str, wait_s: float | None = None) -> None:
        """Call on every get_turn before check_fetch: `fetches` counts the target's calls, refusals included."""
        t = self.live_turn
        if t is not None and t.client == client_id:
            t.handoff.fetches += 1
            t.handoff.wait_requested_s = wait_s

    def count_wait(self, client_id: str, waited_s: float) -> None:
        """Call after a get_turn that held (effective wait > 0): one wait of `waited_s` on the turn live now."""
        t = self.live_turn
        if t is not None and t.client == client_id and waited_s > 0:
            t.handoff.waits += 1
            t.handoff.waited_s += waited_s

    def lapse_leases(self, mono: float) -> None:
        """Lazily, at the next call: a lapsed lease returns the turn to offered; no payload left the hub (5.2)."""
        t = self.live_turn
        if t is not None and t.state == "leased" and t.lease_until is not None and t.lease_until <= mono:
            t.state, t.offer_id, t.lease_until, t.lease_lapsed = "offered", None, None, True

    def mark_broken(self, t: Turn) -> None:
        """Detection only (5.2): nothing is recorded; the hub logs it to the diag file."""
        t.state, t.offer_id, t.lease_until = "broken", None, None

    def packet_intact(self, t: Turn, read: Callable[[str], bytes | None]) -> str | None:
        """The frozen packet text when its file exists and matches packet_sha256, else None."""
        data = read(t.packet_file)
        if data is None or hashlib.sha256(data).hexdigest() != t.packet_sha256:
            return None
        return data.decode("utf-8")

    # ------------------------------------------------------------------ apply (live and replay)

    def commit(self, plan: Plan, rec: Mapping[str, Any], mono: float) -> None:
        """Apply a written record live, plus the plan's memory-only extras."""
        skipped = self.apply(rec, mono)
        assert skipped is None, skipped
        if plan.code is not None and self.live_turn is not None:
            self.live_turn.code = plan.code

    def fold(self, rec: Mapping[str, Any]) -> str | None:
        """Replay one record line (7.3): only the folded events; None when applied or ignored, else why it was
        skipped (the hub logs it to the diag file)."""
        if rec.get("v") != 1 or rec.get("event") not in FOLDED:
            return None
        if rec["event"] == "hub_resumed" and not rec.get("run_id"):
            return None
        return self.apply(rec, None)

    def apply(self, rec: Mapping[str, Any], mono: float | None) -> str | None:
        ev, ts = rec["event"], rec["ts"]
        if ev not in FOLDED and ev != "turn_leased":  # run_note, turn_submit_duplicate, turn_refused, routing
            return None
        run = self.run
        if ev == "run_proposed":
            self.proposal = Proposal(rec["proposal_id"], rec["client"], rec["handle"], rec["task"], ts)
            self.max_proposal = max(self.max_proposal, _num(rec["proposal_id"], "P"))
            return None
        if ev == "run_opened":
            self.max_run = max(self.max_run, _num(rec["run_id"], "R"))
            if run is not None:
                return self._skip(rec, f"{rec['run_id']} opened while {run.id} is open")
            self.run = Run(rec["run_id"], rec["task"], ts)
            if self.proposal is not None and self.proposal.id in (rec.get("proposal_id"), rec.get("dropped_proposal")):
                self.proposal = None
            return None
        if ev == "hub_resumed":
            if run is not None and rec.get("run_id") == run.id:
                run.pause_reasons.clear()
            return None
        if run is None or rec.get("run_id") != run.id:
            return self._skip(rec, f"{ev} for {rec.get('run_id')}, which is not the open run")
        if ev == "run_ended":
            self.run = None
            return None
        if ev == "turn_offered":
            k = rec["step"]
            if run.live_turn is not None or run.stop_pending is not None or k in run.turns:
                return self._skip(rec, f"{rec['turn_id']} offered while a turn is live, a stop is pending or it exists")
            t = Turn(
                run.id, k, rec["client"], rec["handle"], rec["role"], rec["note"], list(rec["inputs"]),
                rec["packet_file"], rec["packet_sha256"], rec["packet_chars"], ts, offered_mono=mono,
            )
            t.handoff.mode, t.handoff.wait_cap_s = rec["handoff"]["mode"], rec["handoff"]["wait_cap_s"]
            t.skill = rec.get("skill")  # absent in records written before skills reached packets
            run.turns[k] = t
            return None
        t = self.find(rec["turn_id"])
        if t is None:
            return self._skip(rec, f"{ev} for unknown turn {rec['turn_id']}")
        if ev == "turn_leased":
            if t.state != "offered":
                return self._skip(rec, f"{t.id} leased in state {t.state}")
            t.state, t.offer_id, t.handoff.mode = "leased", rec["offer_id"], "bounded_wait"
            t.lease_until = None if mono is None else mono + rec["lease_s"]
            t.lease_lapsed = False
        elif ev == "turn_claimed":
            if t.state not in ("offered", "leased") or rec["client"] != t.client:
                return self._skip(rec, f"{t.id} claimed in state {t.state}")
            t.state, t.nonce_sha256, t.claimed_ts, t.claimed_mono = "claimed", rec["nonce_sha256"], ts, mono
            t.offer_id, t.lease_until = None, None
            t.handoff.mode = rec["handoff"]["mode"]
        elif ev == "turn_submitted":
            if t.state != "claimed":
                return self._skip(rec, f"{t.id} submitted in state {t.state}")
            t.state, t.status, t.output, t.submitted_ts = "submitted", rec["status"], rec["output"], ts
            t.output_sha256, t.output_file = rec["output_sha256"], rec["output_file"]
            if rec["status"] == "blocked":
                reason = f"blocked by {self.name(t.client)}"
                if reason not in run.pause_reasons:
                    run.pause_reasons.append(reason)
        elif ev == "turn_recalled":
            if t.state not in LIVE:
                return self._skip(rec, f"{t.id} recalled in state {t.state}")
            t.prior_state, t.state, t.recalled_ts = t.state, "recalled", ts
            t.offer_id, t.lease_until = None, None
            if rec["needs_stop_confirm"]:
                run.stop_pending = t.id
        elif ev == "turn_stop_confirmed":
            if run.stop_pending != t.id:
                return self._skip(rec, f"stop of {t.id} confirmed, but no stop is pending for it")
            run.stop_pending = None
        return None

    def _skip(self, rec: Mapping[str, Any], why: str) -> str:
        if self.run is not None:
            self.run.suspect_lines += 1
            if rec.get("turn_id"):
                self.run.suspect_turns.add(rec["turn_id"])
        else:
            self.skipped += 1
        return why

    def restore(self, read: Callable[[str], bytes | None], code_mode: bool) -> None:
        """After the fold, at start (7.3): an open run comes back paused (hub restart), memory only; an offered
        turn whose packet file is missing or changed is broken; in code mode an offered turn gets a fresh code."""
        if self.run is None:
            return
        if "hub restart" not in self.run.pause_reasons:
            self.run.pause_reasons.append("hub restart")
        t = self.run.live_turn
        if t is not None and t.state == "offered":
            if self.packet_intact(t, read) is None:
                self.mark_broken(t)
            elif code_mode:
                t.code = mint_code()

    # ------------------------------------------------------------------ tool checks (4.3-4.6)

    def _handoff(self, t: Turn | None, **over: Any) -> dict[str, Any]:
        block = (t.handoff if t is not None else Handoff()).block()
        block.update(over)
        return block

    def _refuse(
        self, source: str, c: Any, reason: str, detail: str, *, turn: Turn | None = None, turn_id: str | None = None,
        warn: str | None = "frame", size: tuple[str | None, int | None] = (None, None), late: str | None = None,
        output: str | None = None, code_given: bool = False, kind: str = "turn",
    ) -> Refusal:
        """A tool refusal: the result text, the [WARN] line (the (a) frame, a special line, or None = record only)
        and its turn_refused fields."""
        if warn == "frame":
            what = {"submit_turn": "submit", "propose_run": "run proposal"}.get(source, "turn request")
            warn = f"[WARN] {c.display_name}: {what} refused ({reason}): {detail}"
        fields = {
            "source": source, "client": c.id, "reason": reason, "run_id": self.run.id if self.run else None,
            "turn_id": turn.id if turn is not None else turn_id, "detail": detail, "output_sha256": size[0],
            "output_chars": size[1], "late_file": late, "code_given": code_given, "handoff": self._handoff(turn),
        }
        files = [(late, normalise_str(output))] if late is not None and output is not None else []
        return Refusal(reason, detail, refused_text(reason, detail, kind), [warn] if warn else [], fields, files)

    def _waiting(self, source: str, c: Any, gate: Gate, code_given: bool = False) -> Refusal:
        detail = f"Waiting: {'; '.join(gate.waiting)}. Nothing proceeds single-model."
        r = self._refuse(source, c, "waiting", detail, code_given=code_given, kind="run" if source == "propose_run" else "turn")
        if gate.hints:
            r.lines = [r.lines[0] + f" To bring an app back: {'; '.join(gate.hints)}"]
        return r

    def _gates(
        self, source: str, c: Any, at: At, gate: Gate, read: Callable[[str], bytes | None], code_given: bool
    ) -> Refusal | str:
        """Rows 3-7 of 4.3 (shared by get_turn and claim_turn); the frozen packet text when they all pass."""
        if gate.waiting:
            return self._waiting(source, c, gate, code_given)
        run = self.run
        if run is None:
            return self._refuse(source, c, "no_run", "No run is open.", code_given=code_given)
        if run.pause_reasons:
            detail = f"Run {run.id} is paused ({', '.join(run.pause_reasons)})."
            return self._refuse(source, c, "run_paused", detail, code_given=code_given)
        t = run.live_turn
        nobody = "No turn is waiting for this app. The hub console shows whose turn it is."
        if t is None:
            warn = f"[WARN] {c.display_name} asked for a turn, but no turn is waiting. Decide first: next <client> <role>"
            return self._refuse(source, c, "not_yours", nobody, warn=warn, code_given=code_given)
        if t.client != c.id:
            target = self.clients[t.client]
            warn = f"[WARN] {c.display_name} asked for a turn, but {t.id} is for {target.display_name}: {target.turn_hint}"
            return self._refuse(source, c, "not_yours", nobody, warn=warn, code_given=code_given)
        packet = None
        if t.state in ("offered", "leased"):
            packet = self.packet_intact(t, read)
            if packet is None:
                self.mark_broken(t)
        if t.state == "broken":
            detail = f"The packet file for {t.id} is missing or changed since the operator decided the turn."
            warn = f"[WARN] {t.id} packet file changed or missing; type 'recall T{t.k}', then next"
            return self._refuse(source, c, "packet_changed", detail, turn=t, warn=warn, code_given=code_given)
        if t.state == "claimed":
            when = show_time(t.claimed_ts or at.ts, at.ts)
            warn = (
                f"[WARN] {c.display_name} asked for {t.id}, which was claimed at {when}. "
                f"If you did not expect that claim: recall T{t.k}"
            )
            detail = f"Turn {t.id} was claimed at {when}."
            return self._refuse(source, c, "already_claimed", detail, turn=t, warn=warn, code_given=code_given)
        assert packet is not None
        return packet

    def check_fetch(
        self, c: Any, wait_s: float | None, at: At, gate: Gate, read: Callable[[str], bytes | None], *,
        code: str | None = None, code_mode: bool = False, caller: Caller = Caller(),
    ) -> Refusal | Plan:
        """get_turn (4.3). Call count_fetch first. A Refusal whose reason is in WAITABLE means, at an effective
        wait > 0, 'wait and check again'. Effective wait 0 claims directly (turn_claimed); > 0 leases (turn_leased)."""
        self.lapse_leases(at.mono)
        given = bool(code)
        if wait_s is not None and (isinstance(wait_s, bool) or not math.isfinite(wait_s) or wait_s < 0):
            return self._refuse("get_turn", c, "bad_wait", "wait_s must be 0 or more seconds.", code_given=given)
        packet = self._gates("get_turn", c, at, gate, read, given)
        if isinstance(packet, Refusal):
            return packet
        t = self.live_turn
        assert t is not None
        if t.state == "leased":
            assert t.lease_until is not None
            until = (parse_ts(at.ts) + dt.timedelta(seconds=t.lease_until - at.mono)).isoformat()
            detail = f"Turn {t.id} is offered under a live lease until {show_time(until, at.ts)}."
            return self._refuse("get_turn", c, "already_offered", detail, turn=t, code_given=given)
        if code_mode and (not given or code.strip() != t.code):  # type: ignore[union-attr]
            reason = "code_missing" if not given else "code_wrong"
            detail = "This turn needs the code shown at the hub console."
            return self._refuse("get_turn", c, reason, detail, turn=t, code_given=given)
        if effective_wait(wait_s, c.turn_wait_max_s) > 0:
            offer = mint_offer()
            fields = {
                "run_id": t.run_id, "turn_id": t.id, "client": c.id, "offer_id": offer, "lease_s": c.turn_offer_lease_s,
                "prior_lease_lapsed": t.lease_lapsed, "handoff": self._handoff(t, mode="bounded_wait"),
            }
            return Plan("turn_leased", fields, offer_text(t.id, offer, c.turn_offer_lease_s))
        return self._claim(c, t, packet, "get_turn", at, caller, code_mode, given)

    def check_claim(
        self, c: Any, turn_id: str, offer: str, at: At, gate: Gate, read: Callable[[str], bytes | None], *,
        code_mode: bool = False, caller: Caller = Caller(),
    ) -> Refusal | Plan:
        """claim_turn (4.4): rows 3-7 of 4.3, then unknown_turn and offer_invalid. No wait parameter (D3)."""
        self.lapse_leases(at.mono)
        packet = self._gates("claim_turn", c, at, gate, read, False)
        if isinstance(packet, Refusal):
            return packet
        t = self.live_turn
        assert t is not None
        if self.find(turn_id) is None:
            return self._refuse("claim_turn", c, "unknown_turn", f"There is no turn {sanitize_line(str(turn_id))}.", turn_id=str(turn_id))
        if self.find(turn_id) is not t or t.state != "leased" or offer != t.offer_id:
            detail = f"Offer {sanitize_line(str(offer))} is not valid (lapsed, unknown or replaced)."
            return self._refuse("claim_turn", c, "offer_invalid", detail, turn=self.find(turn_id))
        return self._claim(c, t, packet, "claim_turn", at, caller, code_mode, False)

    def _claim(
        self, c: Any, t: Turn, packet: str, via: str, at: At, caller: Caller, code_mode: bool, given: bool
    ) -> Plan:
        nonce = mint_nonce()
        offered_for, suspect = elapsed(t.offered_ts, t.offered_mono, at)
        fields = {
            "run_id": t.run_id, "turn_id": t.id, "client": c.id, "handle": c.handle, "via": via,
            "nonce_sha256": nonce_hash(t.id, nonce), "offered_for_s": round(offered_for, 3), "era": caller.era,
            "session": caller.session, "client_info": caller.client_info, "code_required": code_mode,
            "code_given": given,
            "handoff": self._handoff(t, mode="operator_nudge" if via == "get_turn" else "bounded_wait"),
        }
        if suspect:
            fields["clock_suspect"] = True
        text = claim_text(t.id, nonce, t.role, self.output_cap, packet)
        return Plan("turn_claimed", fields, text, [f"[TURN] {c.display_name} claimed {t.id}."])

    def no_turn_after_wait(self, c: Any, waited_s: float) -> Refusal:
        """The end of a dormant wait with nothing claimable (4.3 row 10): record only."""
        detail = f"No turn for this app after {_secs(round(waited_s))}."
        return self._refuse("get_turn", c, "no_turn_after_wait", detail, turn=self.live_turn, warn=None)

    def check_submit(
        self, c: Any, turn_id: str, nonce: str, output: str, at: At, *, status: str | None = None,
        files_changed: str | None = None,
    ) -> Refusal | Plan:
        """submit_turn (4.5). Identity rows (turn, claimant, claimed, nonce) come before every row that stores
        content or changes state. Not gated by quorum, pause or registration."""
        osha, chars = sha256_text(output), len(output)
        t = self.find(turn_id)
        if t is None:
            detail = f"There is no turn {sanitize_line(str(turn_id))}."
            return self._refuse("submit_turn", c, "unknown_turn", detail, turn_id=str(turn_id), size=(osha, chars))
        if t.client != c.id:
            detail = f"Turn {t.id} was not claimed by this app."
            return self._refuse("submit_turn", c, "not_claimer", detail, turn=t, size=(osha, chars))
        if t.nonce_sha256 is None:
            detail = f"Turn {t.id} has not been claimed."
            return self._refuse("submit_turn", c, "not_claimed", detail, turn=t, size=(osha, chars))
        if not nonce_ok(t.id, str(nonce), t.nonce_sha256):
            detail = f"The nonce does not match the claim on {t.id}. Nothing was stored."
            return self._refuse("submit_turn", c, "bad_nonce", detail, turn=t, size=(osha, chars))
        if t.state == "recalled":
            late = self.late_path(t.run_id, t.k, osha)
            detail = (
                f"Turn {t.id} was recalled by the operator at {show_time(t.recalled_ts or at.ts, at.ts)}. This output "
                f"was kept for the operator as a late copy and not used. Files changed during the turn are still on disk."
            )
            warn = (
                f"[WARN] Late output for recalled {t.id} from {c.display_name} kept at {late}. "
                f"Its file edits, if any, remain on disk."
            )
            return self._refuse(
                "submit_turn", c, "recalled", detail, turn=t, warn=warn, size=(osha, chars), late=late, output=output
            )
        if t.state == "submitted":
            when = show_time(t.submitted_ts or at.ts, at.ts)
            if osha == t.output_sha256:
                fields = {
                    "run_id": t.run_id, "turn_id": t.id, "client": c.id, "output_sha256": osha,
                    "handoff": self._handoff(t),
                }
                return Plan("turn_submit_duplicate", fields, duplicate_text(t.id, when))
            late = self.late_path(t.run_id, t.k, osha)
            detail = (
                f"Turn {t.id} already has a received output ({when}). This one was kept for the operator as a late "
                f"copy and not used."
            )
            r = self._refuse(
                "submit_turn", c, "already_submitted", detail, turn=t, size=(osha, chars), late=late, output=output
            )
            r.lines = [r.lines[0] + f" Kept at {late}."]
            return r
        status = "done" if status is None else status
        if status not in STATUSES:
            detail = "status must be done, blocked or declined. The turn is still claimed."
            return self._refuse("submit_turn", c, "bad_status", detail, turn=t, warn=None)
        if chars > self.output_cap:
            detail = (
                f"The output is {chars} characters; the limit is {self.output_cap}. The turn is still claimed; "
                f"the same turn_id and nonce stay valid."
            )
            return self._refuse("submit_turn", c, "over_cap", detail, turn=t, warn=None, size=(osha, None))
        claimed_for, suspect = elapsed(t.claimed_ts or at.ts, t.claimed_mono, at)
        out_file = self.output_path(t.run_id, t.k)
        fields = {
            "run_id": t.run_id, "turn_id": t.id, "client": c.id, "handle": c.handle, "status": status,
            "output": output, "output_sha256": osha, "output_chars": chars, "output_file": out_file,
            "files_changed": files_changed, "claimed_for_s": round(claimed_for, 3), "handoff": self._handoff(t),
        }
        if suspect:
            fields["clock_suspect"] = True
        line = summary(output)
        if status == "declined":
            lines = [f"[TURN] {c.display_name} declined {t.id}: {line}", saved_line(out_file), DECIDE_LINE]
        else:
            lines = [f"[TURN] {c.display_name} submitted {t.id} ({status}, {chars} characters): {line}", saved_line(out_file)]
            lines.append(
                DECIDE_LINE if status == "done"
                else f"[RUN] {t.run_id} paused: {c.display_name} reported blocked ({line}). Type 'resume' when it can continue."
            )
        return Plan(
            "turn_submitted", fields, received_text(t.id, chars, status), lines, views=[(out_file, normalise_str(output))]
        )

    def check_propose(self, c: Any, task: str, at: At, gate: Gate) -> Refusal | Plan:
        """propose_run (4.6). A newer proposal replaces a pending one (OC-S31)."""
        if gate.waiting:
            return self._waiting("propose_run", c, gate)
        if self.run is not None:
            return self._refuse("propose_run", c, "run_open", f"Run {self.run.id} is open.", kind="run")
        if not task.strip():
            return self._refuse("propose_run", c, "empty", "The task is empty.", warn=None, kind="run")
        if len(task) > self.packet_cap:
            detail = (
                f"The task is {len(task)} characters; the limit is {self.packet_cap}. "
                f"Larger material can be passed as file paths."
            )
            return self._refuse("propose_run", c, "over_cap", detail, warn=None, kind="run")
        pid = self.next_proposal_id()
        fields = {
            "proposal_id": pid, "client": c.id, "handle": c.handle, "task": task, "task_sha256": sha256_text(task),
            "task_chars": len(task),
        }
        line = f"[RUN] {c.display_name} proposes a run ({pid}): {sanitize_line(task)}. Type 'run' to open it."
        return Plan("run_proposed", fields, proposed_text(pid), [line])

    # ------------------------------------------------------------------ console checks (5.8)

    @staticmethod
    def _say(reason: str, line: str, result: str | None = None) -> Refusal:
        return Refusal(reason, lines=[line], result=result or f"refused:{reason}")

    def check_run(
        self, task: str | None, at: At, *, task_source: str = "console", task_file: str | None = None,
        folder_names: Iterable[str] = (),
    ) -> Refusal | Plan:
        """run <task> | run @<path> (task_source 'file', the text from task_from_file) | run (task None: accepts
        the pending proposal). Not quorum-gated (OC-S8 sub-choice). The hub creates plan.mkdir exclusively."""
        if self.run is not None:
            return self._say("run_open", f"[INFO] Run {self.run.id} is open. Type 'end' first.")
        proposal = None
        if task is None:
            if self.proposal is None:
                line = "Usage: run <task> | run @<path> | run (accepts a pending proposal). No proposal is pending."
                return self._say("usage", line, "usage")
            proposal, task, task_source = self.proposal, self.proposal.task, "proposal"
        if len(task) > self.packet_cap:
            return self._say("over_cap", (
                f"[ERROR] The task is {len(task)} characters, over the limit of {self.packet_cap} "
                f"([turns] packet_cap_chars). Point to material by file path instead."
            ))
        rid = self.next_run_id(folder_names)
        dropped = self.proposal.id if self.proposal is not None and proposal is None else None
        fields = {
            "run_id": rid, "task": task, "task_sha256": sha256_text(task), "task_chars": len(task),
            "task_source": task_source, "task_file": task_file, "proposal_id": proposal.id if proposal else None,
            "dropped_proposal": dropped,
        }
        if proposal is not None:
            first = f"[RUN] {rid} opened from {proposal.id} (proposed by {self.name(proposal.client)}): {sanitize_line(task)}"
        else:
            first = f"[RUN] {rid} opened: {sanitize_line(task)}"
        lines = [first, f"[RUN] Decide the first turn: {DECIDE}"]
        view = (os.path.join(self.run_dir(rid), "task.md"), normalise_str(task))
        return Plan("run_opened", fields, lines=lines, views=[view], mkdir=self.run_dir(rid))

    def _client(self, word: str) -> Any:
        want = word.casefold()
        return next(
            (c for c in self.clients.values() if want in (c.id.casefold(), c.handle.casefold(), c.display_name.casefold())),
            None,
        )

    def _live_line(self, t: Turn) -> str:
        return f"[INFO] {t.id} is still live ({t.state}, {self.name(t.client)}). Wait for its submit, or type 'recall T{t.k}'."

    def _stop_line(self) -> str:
        assert self.run is not None and self.run.stop_pending is not None
        t = self.find(self.run.stop_pending)
        name = self.name(t.client) if t is not None else "the app"
        return f"[INFO] {self.run.stop_pending} was recalled while claimed. Press Stop in {name}, then type: stopped"

    def check_next(
        self, client: str | None, role: str | None, picks: Sequence[str], note: str | None, at: At, gate: Gate,
        read: Callable[[str], bytes | None], *, code_mode: bool = False, skill: tuple[str, str, str] | None = None,
    ) -> Refusal | Plan:
        """next <client> <role> [+T<k> ...|+none] [-- <note>]. `picks` are the '+' words as typed (empty = the
        default selection, the latest done output); `read` returns a file's bytes or None when it is missing.
        `skill` is (name, sha256, text) of the loaded skill whose name is the role, or None: the hub resolves it and
        the text comes from the skill adapter, so the engine never reads a skill's declared fields.
        The hub writes plan.files[0] (the packet, replacing a crash orphan), then turn_offered, then plan.after
        (routing_decision)."""
        refs = [p[1:] for p in picks]
        if (
            not client or not role or not ROLE_RE.fullmatch(role)
            or any(not p.startswith("+") or (r.casefold() != "none" and parse_turn_ref(r) is None) for p, r in zip(picks, refs))
        ):
            return self._say("usage", NEXT_USAGE, "usage")
        c = self._client(client)
        if c is None:
            return self._say("unknown_client", f"Unknown client '{client}'. Clients: {', '.join(self.clients)}.")
        run = self.run
        if run is None:
            return self._say("no_run", "[INFO] No run is open. Type 'run <task>' first.")
        if run.stop_pending is not None:
            return self._say("stop_pending", self._stop_line())
        if run.live_turn is not None:
            return self._say("turn_live", self._live_line(run.live_turn))
        chosen: list[Turn] = []
        plus_t = [r for r in refs if r.casefold() != "none"]
        if plus_t and len(plus_t) != len(refs):
            return self._say("bad_input", f"[INFO] Cannot pass on {plus_t[0]}: +none excludes +T.")
        for r in plus_t:
            t = self.resolve(r)
            why = (
                "no such turn" if t is None or t.state not in ("submitted", "recalled")
                else "it was recalled" if t.state == "recalled"
                else "its record lines are inconsistent (see the diag log)" if t.id in run.suspect_turns
                else None
            )
            if why:
                return self._say("bad_input", f"[INFO] Cannot pass on {r}: {why}.")
            if t not in chosen:
                chosen.append(t)
        if not picks:
            done = [t for t in run.turns_in("submitted") if t.status == "done"]
            chosen = done[-1:]
        chosen.sort(key=lambda t: t.k)
        if run.pause_reasons:
            return self._say("run_paused", f"[INFO] Run {run.id} is paused ({', '.join(run.pause_reasons)}). Type 'resume' first.")
        if gate.waiting:
            line = f"[WARN] next not applied: Waiting: {'; '.join(gate.waiting)}. Nothing proceeds single-model."
            if gate.hints:
                line += f" To bring an app back: {'; '.join(gate.hints)}"
            return self._say("waiting", line)
        notes: list[str] = []
        inputs: list[dict[str, Any]] = []
        texts: list[tuple[Turn, str]] = []
        for t in chosen:
            assert t.output is not None and t.output_file is not None
            used, edited = self._current_output(t, read)
            if used is None:
                return self._say("not_utf8", f"[ERROR] {t.output_file} is not UTF-8; save it as UTF-8 or restore it.")
            if edited is False:
                notes.append(f"[INFO] {t.id} output file missing; using the recorded text.")
            elif edited:
                notes.append(f"[TURN] Using your edited {t.id} output.")
            submitted = normalise_str(t.output)
            inputs.append({
                "turn_id": t.id, "submitted_sha256": sha256_text(submitted), "used_sha256": sha256_text(used),
                "edited_text": used if edited else None,
            })
            texts.append((t, used))
        k = max(run.turns, default=0) + 1
        tid = f"{run.id}-T{k}"
        note = note if note else None
        packet = build_packet(
            run.id, k, role, run.task, note, [(t.k, t.role, text) for t, text in texts],
            skill=None if skill is None else (skill[0], skill[2]),
        )
        if self.data_dir and names_data_folder(packet, self.data_dir):
            return self._say("data_folder", (
                f"[ERROR] The packet names the hub data folder ({self.data_dir}). Packets never point models into it; "
                f"copy the material to a work folder and give that path."
            ))
        total = len(claim_text(tid, "0" * NONCE_HEX, role, self.output_cap, packet))
        if total > self.packet_cap:
            biggest = max(texts, key=lambda x: len(x[1]), default=None)
            edit = f"edit {biggest[0].output_file}, " if biggest is not None else ""
            return self._say("over_cap", (
                f"[ERROR] The packet would be {total} characters, over the limit of {self.packet_cap} "
                f"([turns] packet_cap_chars). Select fewer outputs (+T<n>), {edit}shorten the note, or point to "
                f"material by file path."
            ))
        pfile = self.packet_path(run.id, k)
        handoff = Handoff(
            mode="bounded_wait" if c.turn_wait_max_s > 0 else "operator_nudge", wait_cap_s=c.turn_wait_max_s
        ).block()
        fields = {
            "run_id": run.id, "turn_id": tid, "step": k, "client": c.id, "handle": c.handle, "role": role,
            "note": note, "inputs": inputs, "packet_file": pfile, "packet_sha256": sha256_text(packet),
            "packet_chars": len(packet), "code_required": code_mode, "handoff": handoff,
            "skill": None if skill is None else skill[0],
        }
        routing = {
            "run_id": run.id, "step": k, "round": None, "skill": {"name": skill[0] if skill else None, "sha256": skill[1] if skill else None},
            "model": [c.handle],
            "role": role,
            "alternatives": [
                {"skill": None, "model": [o.handle], "why_not": None} for o in self.clients.values() if o.id != c.id
            ],
            "reason": note, "decided_by": "operator", "bounds_ref": None, "turn_id": tid, "handoff": handoff,
        }
        code = mint_code() if code_mode else None
        parts = "" if not texts else " (" + ", ".join(["task"] + (["note"] if note else []) + [f"T{t.k}" for t, _ in texts]) + ")"
        if skill is not None:
            notes.append(f"[TURN] Skill {skill[0]} goes with this turn; its instructions are in the packet.")
        lines = notes + [
            f"[TURN] {tid} ({role}) for {c.display_name}. Packet {len(packet)} characters{parts}: {pfile}",
            f"[TURN] Next: {c.turn_hint}" + (f" {code}" if code else ""),
        ]
        return Plan("turn_offered", fields, lines=lines, files=[(pfile, packet)], after=[("routing_decision", routing)], code=code)

    def _current_output(self, t: Turn, read: Callable[[str], bytes | None]) -> tuple[str | None, bool | None]:
        """(text to use, edited?) for a submitted turn's output file (9.1). edited is None when the file matches,
        False when it is missing (the recorded text is used), True when it was edited; text None = not UTF-8."""
        assert t.output is not None and t.output_file is not None
        submitted = normalise_str(t.output)
        data = read(t.output_file)
        if data is None:
            return submitted, False
        try:
            current = normalise(data)
        except UnicodeDecodeError:
            return None, None
        return (current, True) if sha256_text(current) != sha256_text(submitted) else (submitted, None)

    def check_recall(self, ref: str | None, at: At) -> Refusal | Plan:
        """recall [<turn>] (5.2). A claimed turn needs 'stopped' before next and end; nothing else does."""
        run = self.run
        live = run.live_turn if run is not None else None
        if ref is None or not ref.strip():
            if live is None:
                return self._say("noop", "[INFO] No turn is live.", "noop")
            line = f"Usage: recall <turn>. The live turn is {live.id} ({live.state}, {self.name(live.client)})."
            return self._say("usage", line, "usage")
        if run is None:
            return self._say("noop", "[INFO] No run is open.", "noop")
        t = self.resolve(ref)
        if t is None:
            return self._say("unknown_turn", f"[INFO] There is no turn {ref.strip()} in {run.id}.")
        if not t.live:
            return self._say("noop", f"[INFO] {t.id} is already {t.state}; nothing to recall.", "noop")
        name = self.name(t.client)
        claimed = t.state == "claimed"
        age, suspect = elapsed(t.claimed_ts or t.offered_ts, t.claimed_mono, at) if claimed else elapsed(
            t.offered_ts, t.offered_mono, at
        )
        fields = {
            "run_id": run.id, "turn_id": t.id, "client": t.client, "prior_state": t.state, "age_s": round(age, 3),
            "needs_stop_confirm": claimed, "handoff": self._handoff(t),
        }
        if suspect:
            fields["clock_suspect"] = True
        if claimed:
            line = (
                f"[TURN] {t.id} recalled (claimed by {name} {ago(age)} ago). A late submit will be refused and kept "
                f"aside. Press Stop in {name}, then type: stopped"
            )
        elif t.state == "broken":
            line = f"[TURN] {t.id} recalled (packet file changed or missing). Nothing was delivered."
        else:
            line = f"[TURN] {t.id} recalled (offered to {name} {ago(age)} ago, not claimed). Nothing was delivered."
        return Plan("turn_recalled", fields, lines=[line])

    def check_stopped(self, at: At) -> Refusal | Plan:
        run = self.run
        if run is None or run.stop_pending is None:
            return self._say("noop", "[INFO] No recalled turn is waiting for 'stopped'.", "noop")
        t = self.find(run.stop_pending)
        assert t is not None
        fields = {"run_id": run.id, "turn_id": t.id, "client": t.client, "by": "operator", "handoff": self._handoff(t)}
        line = f"[TURN] Stop of {t.id} confirmed by the operator. Decide: {DECIDE} | end [outcome]"
        return Plan("turn_stop_confirmed", fields, lines=[line])

    def check_end(self, outcome: str | None, at: At, read: Callable[[str], bytes | None]) -> Refusal | Plan:
        """end [<outcome>]: refused while a turn is live or a stop is pending; final edits are recorded (8.2)."""
        run = self.run
        if run is None:
            return self._say("noop", "[INFO] No run is open.", "noop")
        if run.stop_pending is not None:
            return self._say("stop_pending", self._stop_line())
        live = run.live_turn
        if live is not None:
            return self._say("turn_live", f"[INFO] {live.id} is still live ({live.state}). Type 'recall T{live.k}' first.")
        submitted = run.turns_in("submitted")
        order = list(self.clients)
        by_client = {cid: sum(1 for t in submitted if t.client == cid) for cid in order}
        done_by = {cid: sum(1 for t in submitted if t.client == cid and t.status == "done") for cid in order}
        by_client = {k: v for k, v in by_client.items() if v}
        done_by = {k: v for k, v in done_by.items() if v}
        two_model = len(done_by) >= 2
        open_s, suspect = span(run.opened_ts, at.ts)
        totals: dict[str, dict[str, Any]] = {}
        for t in run.turns.values():
            tot = totals.setdefault(t.client, {"mode": t.handoff.mode, "fetches": 0, "waits": 0, "waited_s": 0.0})
            if t.state in ("claimed", "submitted") or t.prior_state == "claimed":
                tot["mode"] = t.handoff.mode if tot["mode"] == t.handoff.mode else "mixed"
            tot["fetches"] += t.handoff.fetches
            tot["waits"] += t.handoff.waits
            tot["waited_s"] = round(tot["waited_s"] + t.handoff.waited_s, 3)
        final_edits, lines = [], []
        for t in submitted:
            used = None
            for later in run.turns.values():
                used = next((i["used_sha256"] for i in later.inputs if i["turn_id"] == t.id), used)
            data = read(t.output_file) if t.output_file else None
            try:
                current = normalise(data) if data is not None else None
            except UnicodeDecodeError:
                current = None
            baseline = used or sha256_text(normalise_str(t.output or ""))
            if current is not None and sha256_text(current) != baseline:
                final_edits.append({"turn_id": t.id, "used_sha256": sha256_text(current), "edited_text": current})
                lines.append(f"[RUN] Your edit of {t.id} output is recorded.")
        outcome = outcome.strip() if outcome and outcome.strip() else None
        n = len(submitted)
        counts = ", ".join(f"{self.name(cid)} {v}" for cid, v in by_client.items())
        head = f"[RUN] {run.id} ended after {n} turn{'' if n == 1 else 's'}"
        if two_model:
            end = f"{head} ({counts})."
        elif len(by_client) == 1:
            end = f"{head}, all by {self.name(next(iter(by_client)))}: not a two-model run."
        else:
            end = f"{head}{f' ({counts})' if counts else ''}: not a two-model run."
        # Section 3 and 13.3 G14 step 15: ' Outcome: ...' ends the two-model line; the single-model label stands alone
        # (the outcome is in run_ended either way).
        lines.append(end + (f" Outcome: {outcome}" if outcome and two_model else ""))
        fields = {
            "run_id": run.id, "outcome": outcome, "turns_submitted": n, "turns_recalled": len(run.turns_in("recalled")),
            "turns_by_client": by_client, "turns_done_by_client": done_by,
            "handles_used": [self.clients[cid].handle for cid in by_client if cid in self.clients],
            "two_model": two_model, "open_s": round(open_s, 3), "clock_suspect": suspect, "handoff_totals": totals,
            "final_edits": final_edits,
        }
        return Plan("run_ended", fields, lines=lines)

    def check_note(self, rest: str, at: At) -> Refusal | Plan:
        """note [<turn>] <text>: the text is the rest of the line, verbatim."""
        run = self.run
        if run is None:
            return self._say("no_run", "[INFO] No run is open; nothing to attach the note to.")
        text, turn = rest.strip(), None
        words = text.split(None, 1)
        if words and parse_turn_ref(words[0]) is not None:
            turn = self.resolve(words[0])
            if turn is None:
                return self._say("unknown_turn", f"[INFO] There is no turn {words[0]} in {run.id}.")
            text = words[1] if len(words) > 1 else ""
        if not text:
            return self._say("usage", "Usage: note [<turn>] <text>", "usage")
        fields = {"run_id": run.id, "text": text, "turn_id": turn.id if turn else None}
        return Plan("run_note", fields, lines=[f"[RUN] Note recorded for {turn.id if turn else run.id}."])

    def check_resume(self, by: str = "operator") -> Plan | None:
        """The run part of resume (5.3): None when no run pause reason is set. The hub writes one hub_resumed
        (with the hub flag, if it was set, printing '[INFO] Hub resumed.' first) and commits this plan."""
        run = self.run
        if run is None or not run.pause_reasons:
            return None
        cleared = list(run.pause_reasons)
        fields = {"by": by, "run_id": run.id, "cleared": cleared}
        return Plan("hub_resumed", fields, lines=[f"[INFO] Run {run.id} resumed (cleared: {', '.join(cleared)})."])

    # ------------------------------------------------------------------ reminders and status (5.7, 5.8)

    def reminders(
        self, at: At, offer_s: float | None, claim_s: float | None, hub_paused: bool, floor_mono: float
    ) -> list[str]:
        """Transition-only lines, once per turn. Ages count from the latest of the event, the hub start and the
        last resume (floor_mono); never while the run is paused or the hub flag is set."""
        run = self.run
        t = run.live_turn if run is not None else None
        if t is None or run is None or run.pause_reasons or hub_paused:
            return []
        name, lines = self.name(t.client), []
        if t.state in ("offered", "leased") and offer_s is not None and "offer" not in t.reminded:
            age = at.mono - max(t.offered_mono if t.offered_mono is not None else floor_mono, floor_mono)
            if age >= offer_s:
                t.reminded.add("offer")
                hint = self.clients[t.client].turn_hint + (f" {t.code}" if t.code else "")
                lines.append(f"[TURN] {t.id} offered to {name} {ago(age)} ago and not claimed. Next: {hint}")
        if t.state == "claimed" and claim_s is not None and "claim" not in t.reminded:
            age = at.mono - max(t.claimed_mono if t.claimed_mono is not None else floor_mono, floor_mono)
            if age >= claim_s:
                t.reminded.add("claim")
                lines.append(
                    f"[TURN] {t.id} claimed by {name} {ago(age)} ago, no submit. If the app hit its usage limit: "
                    f"pause. If that conversation is gone: recall T{t.k}"
                )
        return lines

    def status_lines(self, at: At, code_mode: bool = False) -> list[str]:
        """The status block of 5.8, inserted before the quorum line; empty with no run, proposal or stop."""
        out: list[str] = []
        run = self.run
        if run is not None:
            paused = f", PAUSED ({', '.join(run.pause_reasons)})" if run.pause_reasons else ""
            n = len(run.turns_in("submitted"))
            out.append(f"Run {run.id}: open{paused}; {n} turns submitted; task: {sanitize_line(run.task)}")
            t = run.live_turn
            if t is not None and t.state != "broken":
                since_ts, since_mono = (t.claimed_ts, t.claimed_mono) if t.state == "claimed" else (t.offered_ts, t.offered_mono)
                age, _ = elapsed(since_ts or t.offered_ts, since_mono, at)
                hint = self.clients[t.client].turn_hint if t.client in self.clients else ""
                code = f" {t.code}" if code_mode and t.code else ""
                out.append(
                    f"Turn {t.id} ({t.role}) for {self.name(t.client)}: {t.state.upper()} at "
                    f"{show_time(since_ts or t.offered_ts, at.ts)} ({ago(age)}); Next: {hint}{code}"
                )
        if self.proposal is not None:
            out.append(f"Proposal {self.proposal.id} from {self.name(self.proposal.client)}: {sanitize_line(self.proposal.task)}")
        if run is not None:
            if run.stop_pending is not None:
                t = self.find(run.stop_pending)
                out.append(f"Stop confirmation pending: {run.stop_pending} ({self.name(t.client) if t else '-'})")
            if run.suspect_lines:
                out.append(f"Run {run.id}: SUSPECT, {run.suspect_lines} inconsistent record lines skipped; see the diag log")
            t = run.live_turn
            if t is not None and t.state == "broken":
                out.append(f"Turn {t.id}: BROKEN, packet file changed or missing; type 'recall T{t.k}', then next")
        return out


# ------------------------------------------------------------------------------------------ packets and helpers


def normalise_str(text: str) -> str:
    """Text as hub-written files hold it (7.2): no BOM, \\n line ends."""
    return text.removeprefix("﻿").replace("\r\n", "\n")


def effective_wait(wait_s: float | None, cap: float) -> float:
    """4.7: min(request, this app's turn_wait_max_s); an omitted wait_s means the cap."""
    return cap if wait_s is None else min(wait_s, cap)


def build_packet(
    run_id: str, k: int, role: str, task: str, note: str | None, earlier: Sequence[tuple[int, str, str]],
    skill: tuple[str, str] | None = None,
) -> str:
    """The work packet of 9.1: data only, roles only (no handles or model names). earlier = (k, role, text),
    oldest first. skill = (name, text as the skill adapter renders it), placed right after the role."""
    parts = [f"RCO work packet: run {run_id}, turn T{k}.\nRole: {role}"]
    if skill is not None:
        name, text = skill
        parts.append(f"Skill: {name}" + (f"\n{normalise_str(text)}" if text.strip() else ""))
    parts.append(f"Task:\n{normalise_str(task)}")
    if note:
        parts.append(f"Operator note:\n{normalise_str(note)}")
    for ek, erole, text in earlier:
        parts.append(f"Earlier turn T{ek} (role: {erole}), as passed on by the operator:\n{normalise_str(text)}")
    return "\n\n".join(parts)


def task_from_file(path: str, data: bytes | None, error: str | None = None) -> str | Refusal:
    """run @<path>: the file's text (UTF-8, BOM stripped), or the refusal. data None and no error = missing."""
    why = None
    if error is not None:
        why = f"unreadable: {error}"
    elif data is None:
        why = "missing"
    else:
        try:
            text = normalise(data)
        except UnicodeDecodeError:
            why = "not UTF-8"
        else:
            if not text.strip():
                why = "empty"
    if why is not None:
        return Refusal("task_file", lines=[f"[ERROR] Cannot use {path}: {why}."], result="refused:task_file")
    return text


def run_folder_exists(path: str) -> Refusal:
    """The exclusive create of a run folder failed (7.2)."""
    line = f"[ERROR] {path} already exists; the hub never reuses a run folder. Move it aside, then retype."
    return Refusal("run_folder_exists", lines=[line], result="refused:run_folder_exists")


def io_failure(path: str, error: str) -> Refusal:
    """A file write for next failed before the commit; the hub also records console_failure(...)."""
    line = f"[ERROR] Cannot write {path}: {error}. Nothing was offered."
    return Refusal("io", lines=[line], result="refused:io")


def console_failure(reason: str, detail: str, run_id: str | None = None, turn_id: str | None = None) -> dict[str, Any]:
    """turn_refused fields for a console action that failed after its check (file I/O, strict record; 8.2)."""
    return {
        "source": "console", "client": None, "reason": reason, "run_id": run_id, "turn_id": turn_id, "detail": detail,
        "output_sha256": None, "output_chars": None, "late_file": None, "code_given": False,
        "handoff": Handoff().block(),
    }


_HANDED = "The hub cannot write its record, so nothing was handed out."
_KEPT = "The turn is still claimed; the same turn_id and nonce stay valid."


def no_record(source: str) -> Refusal:
    """The commit write failed (4.3 note (b)): not recordable; the hub's one-time [ERROR] is the console line."""
    kind = "run" if source == "propose_run" else "turn"
    detail = f"Your output was not received. {_KEPT}" if source == "submit_turn" else _HANDED
    return Refusal("no_record", detail, refused_text("no_record", detail, kind))


def internal(source: str) -> Refusal:
    """A handler caught an exception (SchemaError included): the outcome line stays in the contract."""
    kind = "run" if source == "propose_run" else "turn"
    if source == "submit_turn":
        detail = f"Internal hub error; your output was not received. {_KEPT}"
    else:
        detail = "Internal hub error; nothing was handed out."
    line = f"[ERROR] internal: {source} failed (details in the diag log)."
    return Refusal("internal", detail, refused_text("internal", detail, kind), [line])


def replay(
    records: Iterable[Mapping[str, Any]], book: TurnBook, read: Callable[[str], bytes | None], code_mode: bool
) -> list[str]:
    """7.3: fold every record in file order into an empty `book`, then restore(). Returns the skip reasons for
    the diag log. Replay never writes files."""
    skipped = []
    for rec in records:
        why = book.fold(rec)
        if why is not None:
            skipped.append(why)
    book.restore(read, code_mode)
    return skipped
