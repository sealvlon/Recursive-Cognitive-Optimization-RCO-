"""Operator console (design section 7; Stage 1 design 5.8).

One output sink; every hub line is written under one lock as "\\r" + clear line + text + "\\n" + prompt +
pending input, so a hub line never destroys typed text and the final screen equals the scene. Keys come
from an injectable key source (msvcrt.getwch on a real console, a line reader when stdin is not a
console, scripted keys in tests). The command dispatcher is pure: (snapshot, line) -> Result.

Stage 1: the turn verbs (run, next, recall, stopped, end, note) are parsed from the raw line, because a task, a
note, an outcome or a path with spaces is the rest of the line; Result.turn carries them to the hub, which asks
the turn engine (rco.turns) for the true outcome and the catalogue lines.
"""

import collections
import os
import shutil
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

from rco.turns import sanitize_line  # one implementation for model and file text on console lines (5.8)

PROMPT = "> "
_GREEN, _RESET = "\x1b[32m", "\x1b[0m"
KeySource = Callable[[], str]  # returns one character; "" means end of input


class Console:
    def __init__(self, out: TextIO, *, vt: bool = False, width: int = 80) -> None:
        self._out = out
        self._lock = threading.RLock()
        self._buf = ""
        self._prompt_on = False
        self.colorable = False  # a VT console with NO_COLOR unset
        self.color = False  # green [INFO] tag; the visible text never changes
        self.tap: Callable[[list[str]], None] | None = None  # sees every printed line (the operator panel's log)
        # Without VT processing (legacy conhost, or not a console) the line is blanked with spaces.
        self._clear = "\r\x1b[2K" if vt else "\r" + " " * max(width - 1, 1) + "\r"

    def _paint(self, text: str) -> str:
        if self.color and text.startswith("[INFO]"):
            return _GREEN + "[INFO]" + _RESET + text[6:]
        return text

    def _write(self, s: str) -> None:
        try:
            self._out.write(s)
            self._out.flush()
        except (OSError, ValueError):
            pass  # a closed or broken console must never take the hub down

    def lines(self, texts: list[str]) -> None:
        with self._lock:
            body = "".join(self._paint(t) + "\n" for t in texts)
            if self._prompt_on:
                self._write(self._clear + body + PROMPT + self._buf)
            else:
                self._write(body)
            if self.tap is not None:
                self.tap(list(texts))

    def line(self, text: str) -> None:
        self.lines([text])

    def show_prompt(self) -> None:
        with self._lock:
            if not self._prompt_on:
                self._prompt_on = True
                self._write(PROMPT + self._buf)

    def close_prompt(self) -> None:
        """Blank the prompt line; later lines print plainly (used for the stop lines)."""
        with self._lock:
            if self._prompt_on:
                self._prompt_on = False
                self._write(self._clear)

    def feed(self, ch: str) -> str | None:
        """Line editor: consume one key; return the finished line on Enter."""
        with self._lock:
            if ch in ("\r", "\n"):
                line, self._buf = self._buf, ""
                if self._prompt_on:
                    self._write("\n" + PROMPT)
                return line
            if ch in ("\x08", "\x7f"):
                if self._buf:
                    self._buf = self._buf[:-1]
                    if self._prompt_on:
                        self._write("\b \b")
            elif ch == "\x1b":
                self._buf = ""
                if self._prompt_on:
                    self._write(self._clear + PROMPT)
            elif ch.isprintable():
                self._buf += ch
                if self._prompt_on:
                    self._write(ch)
            return None


def start_key_thread(
    console: Console,
    keys: KeySource,
    on_line: Callable[[str], None],
    on_interrupt: Callable[[], None],
    special_pending: Callable[[], bool] | None = None,
) -> threading.Thread:
    """special_pending: None for sources that deliver typed text only (a line reader, scripted keys). For
    msvcrt.getwch it is msvcrt.kbhit: a special key (arrows, F-keys) arrives as "\\x00" or "\\xe0" with its
    second code already queued, while a typed U+00E0 stands alone."""

    def run() -> None:
        while True:
            try:
                ch = keys()
            except (EOFError, OSError, ValueError):
                return
            if not ch:
                return
            if ch == "\x03":  # Ctrl+C read as a key (when it is not delivered as SIGINT)
                on_interrupt()
            elif ch in ("\x00", "\xe0") and special_pending is not None and special_pending():
                keys()  # a special key: drop its second code too
            else:
                line = console.feed(ch)
                if line is not None:
                    on_line(line)

    t = threading.Thread(target=run, name="rco-keys", daemon=True)  # never holds up exit
    t.start()
    return t


def line_keys(stream: TextIO) -> KeySource:
    """Key source over a line-oriented stream (stdin piped or redirected: msvcrt needs a real console)."""
    pending: collections.deque[str] = collections.deque()

    def next_key() -> str:
        if not pending:
            line = stream.readline()
            if not line:
                return ""
            pending.extend(line.rstrip("\r\n"))
            pending.append("\r")
        return pending.popleft()

    return next_key


def default_keys() -> tuple[KeySource, Callable[[], bool] | None] | None:
    """(key source, special-key test): msvcrt on a real console, else a line reader (typed text only)."""
    stdin = sys.stdin
    if stdin is None:
        return None
    try:
        tty = stdin.isatty()
    except (OSError, ValueError):
        tty = False
    if tty and os.name == "nt":
        import msvcrt

        return msvcrt.getwch, msvcrt.kbhit
    return line_keys(stdin), None


def _enable_vt(stream: TextIO) -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.restype = wintypes.HANDLE
        handle = k32.GetStdHandle(-11 if stream is sys.stdout else -12)
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (OSError, AttributeError):
        return False


def open_console(stream: TextIO, env: Any) -> Console:
    try:
        tty = stream.isatty()
    except (OSError, ValueError):
        tty = False
    if not tty:
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass
    vt = tty and _enable_vt(stream)
    width = shutil.get_terminal_size((80, 24)).columns  # GetConsoleScreenBufferInfo on a console
    console = Console(stream, vt=vt, width=width)
    console.colorable = console.color = vt and "NO_COLOR" not in env
    return console


# ---------------------------------------------------------------------------------------------- dispatcher


@dataclass
class Result:
    command: str
    args: list[str]
    text: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)  # "pause", "resume", "stop", "release:<client id>"
    result: str = "ok"
    # Stage 1: the parsed turn verb, {"verb": ..., ...}; the hub asks the engine and records the true result.
    # pause also carries {"verb": "pause", "reason": ...} for hub_paused.
    turn: dict[str, Any] | None = None


TURN_VERBS = ("run", "next", "recall", "stopped", "end", "note")
HELP = [
    ("status", "clients, streams, quorum, hub state and the open run"),
    ("pause", "pause [reason]: hold turn requests, proposals and next until resume"),
    ("resume", "clear the hub pause and a paused run's reasons"),
    ("stop", "stop the hub (open streams get at most 3 s); Ctrl+C and Ctrl+Break do the same"),
    ("help", "this list"),
]
HELP_EXTRA = {
    "skills": "the skill catalog and rejections",
    "release": "release <client>: clear a registration",
    "panel": "print the operator panel address and open it in the browser",
}
HELP_TURNS = [  # appended after the extras (Stage 1 design 12.2 point 5)
    ("run", "run <task> | run @<path> | run: open a run (bare: accept the pending proposal)"),
    ("next", "next <client> <role> [+T<k>...|+none] [-- note]: decide the next turn"),
    ("recall", "recall <turn>: take back the live turn"),
    ("stopped", "confirm you pressed Stop in the app of a recalled claimed turn"),
    ("end", "end [outcome]: end the open run"),
    ("note", "note [<turn>] <text>: record a note on the run or one turn"),
]


def _rest(line: str, typed: str) -> str:
    """The raw text after the verb, outer whitespace trimmed (line.split() would break paths and notes)."""
    return line.lstrip()[len(typed):].strip()


def _turn(cmd: str, args: list[str], rest: str) -> Result:
    """The 5.8 grammar of the turn verbs, as far as it needs no state; the engine checks the rest."""
    turn: dict[str, Any] = {"verb": cmd}
    if cmd == "run":
        if rest.startswith("@"):
            path = rest[1:].strip()
            if not path:
                usage = "Usage: run <task> | run @<path> | run (accepts a pending proposal)."
                return Result(cmd, args, [usage], result="usage")
            turn["file"] = path
        else:
            turn["task"] = rest or None
    elif cmd == "next":
        head, sep, note = (rest + " ").partition(" -- ")  # the first ' -- ' splits; the note is the rest, verbatim
        words = head.split()
        turn.update(
            client=words[0] if words else None, role=words[1] if len(words) > 1 else None, picks=words[2:],
            note=note.strip() if sep and note.strip() else None,
        )
    elif cmd in ("recall", "end"):
        turn["ref" if cmd == "recall" else "outcome"] = rest or None
    elif cmd == "note":
        turn["rest"] = rest
    return Result(cmd, args, turn=turn)


def dispatch(snap: dict[str, Any], line: str, extra: set[str]) -> Result | None:
    words = line.split()
    if not words:
        return None
    typed, args = words[0], words[1:]
    cmd = typed.lower()
    if cmd == "help":
        rows = HELP + [(c, HELP_EXTRA[c]) for c in sorted(extra)] + HELP_TURNS
        return Result(cmd, args, ["Commands:"] + [f"  {c:<8} {d}" for c, d in rows])
    if cmd == "status":
        return Result(cmd, args, status_lines(snap))
    if cmd == "pause":
        if snap["paused"]:
            return Result(cmd, args, ["[INFO] Hub is already paused."], result="noop")
        reason = _rest(line, typed) or None
        return Result(cmd, args, ["[INFO] Hub paused by operator."], ["pause"], turn={"verb": "pause", "reason": reason})
    if cmd == "resume":
        run = snap.get("run")
        if not snap["paused"] and not (run and run["pause_reasons"]):
            return Result(cmd, args, ["[INFO] Hub is not paused."], result="noop")
        # The run part ('[INFO] Run R1 resumed (cleared: ...)') comes from the engine; the hub appends it.
        return Result(cmd, args, ["[INFO] Hub resumed."] if snap["paused"] else [], ["resume"])
    if cmd == "stop":
        return Result(cmd, args, [], ["stop"])
    if cmd == "skills" and "skills" in extra:
        return Result(cmd, args, skills_lines(snap["skills"]))
    if cmd == "release" and "release" in extra:
        return _release(snap, args)
    if cmd == "panel" and "panel" in extra:
        return Result(cmd, args, [], ["panel"])
    if cmd in TURN_VERBS:
        return _turn(cmd, args, _rest(line, typed))
    for command, hint in snap.get("turn_commands", ()):
        if typed.casefold() == command.casefold():  # typed into the hub by mistake (5.8, section 11)
            return Result(cmd, args, [f"[INFO] '{sanitize_line(typed)}' is typed in the app, not here: {hint}"], result="unknown")
    return Result(cmd, args, [f"Unknown command '{typed}'. Type 'help'."], result="unknown")


def _release(snap: dict[str, Any], args: list[str]) -> Result:
    if len(args) != 1:
        return Result("release", args, ["Usage: release <client>  (id, handle or name)"], result="usage")
    want = args[0].casefold()
    for c in snap["clients"]:
        if want in (c["id"].casefold(), c["handle"].casefold(), c["display_name"].casefold()):
            if not c["registered"]:
                return Result("release", args, [f"[INFO] {c['display_name']} is not registered."], result="noop")
            text = [f"[INFO] Registration of {c['display_name']} ({c['handle']}) released."]
            return Result("release", args, text, [f"release:{c['id']}"])
    ids = ", ".join(c["id"] for c in snap["clients"])
    return Result("release", args, [f"Unknown client '{args[0]}'. Clients: {ids}."], result="unknown_client")


def _age(seconds: float | None) -> str:
    return "never" if seconds is None else f"{seconds:.0f} s ago"


def status_lines(snap: dict[str, Any]) -> list[str]:
    sk = snap["skills"]
    out = [
        f"Hub: {'PAUSED' if snap['paused'] else 'running'}, uptime {snap['uptime_s']:.0f} s, {snap['url']}",
        f"Data dir: {snap['data_dir']}; records {'OK' if snap['records_healthy'] else 'FAILING'}; "
        f"skills {sk['count']} (rejected {len(sk['rejected'])})",
    ]
    for c in snap["clients"]:
        if c["registered"]:
            reg = f"registered ({c['registered_at']}, count {c['register_count']})"
        else:
            reg = f"NOT REGISTERED (transport seen: {'yes' if c['transport_seen'] else 'no'})"
        flags = "".join(f"; {f}" for f in ("STALE", "MISSING") if c[f.lower()])
        out.append(
            f"{c['display_name']} ({c['handle']}): {reg}; open streams {c['open_requests']}; "
            f"last seen {_age(c['last_seen_age_s'])}; era {c['era'] or '-'}; clientInfo {c['client_info'] or '-'}{flags}"
        )
    out += snap.get("turn_status", [])  # Stage 1 5.8: run, turn, proposal, stop, SUSPECT, BROKEN; [] = M1 output
    q = snap["quorum"]  # the quorum line, exactly as design section 7 gives it; it stays last
    if q["ok"]:
        out.append(f"READY {q['present']}/{q['total']}")
    else:
        out.append(f"WAITING: {'; '.join(q['waiting'])}. Nothing proceeds single-model.")
    return out


def skills_lines(sk: dict[str, Any]) -> list[str]:
    out = [f"Skills: {sk['count']} (rejected {len(sk['rejected'])}), adapter {sk['adapter']}"]
    out += [f"  {s['name']} ({s['file']}) sha256 {s['sha256'][:12]}" for s in sk["skills"]]
    out += [f"  rejected: {r['file']}: {r['reason']}" for r in sk["rejected"]]
    return out
