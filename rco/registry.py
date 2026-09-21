"""registry.toml loader and renderer (design section 4; the turn fields: Stage 1 design 6.1).

Every scene string and every per-client string is data in the registry file; the core renders templates
and never branches on a client. A third client is one more [[client]] block.
"""

import math
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rco.config import ConfigError, read_toml

# field -> (kind, placeholders allowed in it)
SHARED: dict[str, tuple[str, tuple[str, ...]]] = {
    "console_started": ("str", ()),
    "console_waiting": ("str", ()),
    "console_ready": ("list", ()),
    "console_connected": ("str", ("display_name", "handle")),
    "check_prefix": ("str", ()),
    "checks": ("list", ("skill_count",)),
    "result_layout": ("list", ("init_line", "checks", "footer")),
}
CLIENT: dict[str, str] = {
    "id": "str",
    "display_name": "str",
    "handle": "str",
    "auth_env": "str",
    "invocation_names": "list",
    "client_info_hints": "list",
    "invocation_hint": "str",
    "reconnect_hint": "str",
    "init_line": "str",
    "footer": "str",
    # Turns (Stage 1 design 6.1): what the operator types in the app and where, how long a fetch may hold, how long
    # an offer lasts. Every per-client turn difference is one of these; the core compares ids, numbers and hashes.
    "turn_command": "str",
    "turn_hint": "str",
    "turn_wait_max_s": "num",
    "turn_offer_lease_s": "num",
}
UNIQUE = ("id", "display_name", "handle", "auth_env", "invocation_names", "turn_command")  # never a num: casefolded
POSITIVE = ("turn_offer_lease_s",)  # num fields that must be > 0; the other num fields may be 0


@dataclass(frozen=True)
class Client:
    id: str
    display_name: str
    handle: str
    auth_env: str
    invocation_names: tuple[str, ...]
    client_info_hints: tuple[str, ...]
    invocation_hint: str
    reconnect_hint: str
    init_line: str
    footer: str
    turn_command: str
    turn_hint: str
    turn_wait_max_s: float
    turn_offer_lease_s: float


@dataclass(frozen=True)
class Registry:
    path: Path
    sha256: str
    shared: dict[str, Any]
    clients: tuple[Client, ...]

    def get(self, client_id: str) -> Client:
        return next(c for c in self.clients if c.id == client_id)

    def startup_lines(self) -> list[str]:
        return [self.shared["console_started"], self.shared["console_waiting"]]

    def ready_lines(self) -> list[str]:
        return list(self.shared["console_ready"])

    def connected_line(self, c: Client) -> str:
        return self.shared["console_connected"].format(display_name=c.display_name, handle=c.handle)

    def payload(self, c: Client, skill_count: int) -> str:
        prefix = self.shared["check_prefix"]
        checks = [prefix + t.format(skill_count=skill_count) for t in self.shared["checks"]]
        lines: list[str] = []
        for t in self.shared["result_layout"]:
            if t == "{checks}":
                lines.extend(checks)
            else:
                lines.append(t.format(init_line=c.init_line, footer=c.footer))
        return "\n".join(lines)

    def accepts_name(self, c: Client, name: str) -> bool:
        return name.strip().casefold() in {n.casefold() for n in c.invocation_names}


def _placeholders(template: str) -> list[str]:
    names = []
    for _, name, spec, conv in string.Formatter().parse(template):
        if name is None:
            continue
        if spec or conv:
            raise ValueError(f"format specs and conversions are not allowed in {template!r}")
        names.append(name)
    return names


def _check(value: Any, kind: str, allowed: tuple[str, ...], non_empty: bool, positive: bool = False) -> None:
    if kind == "num":
        if isinstance(value, bool) or not isinstance(value, int | float):  # bool is an int to Python
            raise ValueError(f"must be a number, got {value!r}")
        if not math.isfinite(value):
            raise ValueError(f"must be finite, got {value!r}")
        if positive and value <= 0:
            raise ValueError(f"must be greater than 0, got {value!r}")
        if value < 0:
            raise ValueError(f"must be 0 or more, got {value!r}")
        return
    if kind == "str":
        if not isinstance(value, str):
            raise ValueError("must be a string")
        items = [value]
    else:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("must be a list of strings")
        items = value
    if non_empty and (not items or any(not s.strip() for s in items)):
        raise ValueError("must not be empty")
    for s in items:
        for name in _placeholders(s):
            if name not in allowed:
                ok = ", ".join("{%s}" % a for a in allowed) or "none"
                raise ValueError(f"placeholder {{{name}}} is not allowed here (allowed: {ok})")


def load_registry(path: Path) -> Registry:
    path = Path(path).resolve()
    doc, sha = read_toml(path)

    def fail(where: str, msg: str) -> ConfigError:
        return ConfigError(f"{path}: {where}: {msg}")

    for key in doc:
        if key not in ("schema", "shared", "client"):
            raise fail(f"top-level key '{key}'", "unknown key")
    if doc.get("schema") != 1:
        raise fail("schema", "must be 1")
    shared = doc.get("shared")
    if not isinstance(shared, dict):
        raise fail("[shared]", "missing table")
    for key in shared:
        if key not in SHARED:
            raise fail(f"[shared] field '{key}'", "unknown field")
    for key, (kind, allowed) in SHARED.items():
        if key not in shared:
            raise fail(f"[shared] field '{key}'", "missing")
        try:
            _check(shared[key], kind, allowed, non_empty=key not in ("console_ready", "result_layout"))
        except ValueError as e:
            raise fail(f"[shared] field '{key}'", str(e)) from None
    for line in shared["result_layout"]:
        if "checks" in _placeholders(line) and line != "{checks}":
            raise fail("[shared] field 'result_layout'", "{checks} must stand alone on its line")

    entries = doc.get("client")
    if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
        raise fail("[[client]]", "at least one [[client]] entry is required")
    clients = []
    seen: dict[str, dict[str, str]] = {f: {} for f in UNIQUE}
    for i, entry in enumerate(entries, 1):
        where = f"[[client]] #{i}" + (f" (id '{entry['id']}')" if isinstance(entry.get("id"), str) else "")
        for key in entry:
            if key not in CLIENT:
                raise fail(f"{where} field '{key}'", "unknown field")
        for key, kind in CLIENT.items():
            if key not in entry:
                raise fail(f"{where} field '{key}'", "missing")
            try:
                _check(entry[key], kind, (), non_empty=key != "client_info_hints", positive=key in POSITIVE)
            except ValueError as e:
                raise fail(f"{where} field '{key}'", str(e)) from None
        command = entry["turn_command"]
        if any(ch.isspace() for ch in command):
            raise fail(f"{where} field 'turn_command'", "must not contain whitespace")
        if not entry["turn_hint"].endswith(command):  # the Next line appends the entry code to the hint
            raise fail(f"{where} field 'turn_hint'", f"must end with turn_command '{command}' (a code may follow it)")
        for key in UNIQUE:
            for v in entry[key] if isinstance(entry[key], list) else [entry[key]]:
                other = seen[key].get(v.casefold())
                if other is not None:
                    raise fail(f"{where} field '{key}'", f"duplicate value '{v}' (also in {other})")
                seen[key][v.casefold()] = where
        clients.append(Client(**{k: tuple(v) if isinstance(v, list) else v for k, v in entry.items()}))
    return Registry(path=path, sha256=sha, shared=dict(shared), clients=tuple(clients))
