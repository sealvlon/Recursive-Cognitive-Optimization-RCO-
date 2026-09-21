"""rco.toml loader (design 9.1). Precedence: defaults < rco.toml < CLI (--config, --port).

Unknown keys, bad types, port 0 and any host but 127.0.0.1 stop the hub, naming the key. There is no
configuration through environment variables; %VAR% inside path values is expanded. Stage 1 adds [turns]
(Stage 1 design 6.2): hub-wide caps and timings, none of which relaxes quorum.
"""

import argparse
import hashlib
import math
import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
POLICIES = ("idempotent_line", "idempotent_quiet", "refuse_until_release", "line_after_stale")
EXTRA_COMMANDS = ("skills", "release")
PACKET_MARGIN_CHARS = 2000  # room a packet keeps for its header, the task and the note beside a full-size output


class ConfigError(Exception):
    """Invalid configuration or registry; the message names the file and the key."""


@dataclass(frozen=True)
class Config:
    path: Path
    sha256: str
    host: str
    port: int
    registry: Path
    data_dir: Path
    skills_dir: Path
    adapter: str
    poll_interval_s: float
    settle_ms: int
    scan_deadline_ms: int
    staleness_s: float | None
    missing_report_s: float | None
    session_idle_timeout: float | None  # as passed to the SDK: None = never expire
    re_register_policy: str
    name_mismatch: str
    on_write_failure: str
    extra_commands: tuple[str, ...]
    quiet_until_ready: bool
    color: str
    level: str
    diag_level: str
    diag_max_mb: float
    diag_backups: int
    packet_cap_chars: int
    output_cap_chars: int
    entry_code: str
    offer_reminder_s: float | None  # None = off
    claim_reminder_s: float | None  # None = off
    wait_progress_s: float
    # Operator panel: a local web UI over the same console commands (rco.panel). 127.0.0.1 only.
    panel_enabled: bool
    panel_port: int
    panel_open_browser: bool


def read_toml(path: Path, error: type[Exception] = ConfigError) -> tuple[dict[str, Any], str]:
    """Parse a UTF-8 TOML file, tolerating the BOM that PowerShell 5.1 writes (tomllib rejects it)."""
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise error(f"{path}: cannot read: {e.strerror or e}") from None
    try:
        text = raw.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
        doc = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise error(f"{path}: not valid UTF-8 TOML: {e}") from None
    return doc, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- value checks (raise ValueError(message))


def _integer(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"must be an integer, got {v!r}")
    return v


def _number(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError(f"must be a number, got {v!r}")
    if not math.isfinite(v):
        raise ValueError(f"must be finite, got {v!r}")
    return v


def _positive(v: Any) -> float:
    if _number(v) <= 0:
        raise ValueError(f"must be greater than 0, got {v!r}")
    return v


def _positive_int(v: Any) -> int:
    if _integer(v) <= 0:
        raise ValueError(f"must be greater than 0, got {v!r}")
    return v


def _count(v: Any) -> int:
    if _integer(v) < 0:
        raise ValueError(f"must be 0 or more, got {v!r}")
    return v


def _positive_or_off(v: Any) -> float | None:
    return None if v == "off" else _positive(v)


def _idle_timeout(v: Any) -> float | None:
    # mcp 2.2.0 raises on 0; only None disables expiry, so "never" (and 0 as an alias) map to None.
    if v == "never" or (not isinstance(v, bool) and v == 0):
        return None
    if isinstance(v, str):
        raise ValueError(f'must be a number of seconds or "never", got {v!r}')
    return _positive(v)


def _port(v: Any) -> int:
    if _integer(v) == 0:
        raise ValueError("0 is refused: the hub needs a fixed port that the apps can be configured with")
    if not 1 <= v <= 65535:
        raise ValueError(f"must be 1..65535, got {v}")
    return v


def _host(v: Any) -> str:
    if v != HOST:
        raise ValueError(f'must be "{HOST}": the hub binds loopback only, got {v!r}')
    return v


def _text(v: Any) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"must be a non-empty string, got {v!r}")
    return v


def _module(v: Any) -> str:
    if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", v):
        raise ValueError(f"must be a Python module name, got {v!r}")
    return v


def _module_required(v: Any) -> str:
    # No default: the skill-format adapter is named outside the core (design section 14), in rco.toml.
    if v is None:
        raise ValueError('required: name the skill-format adapter module (rco.toml sets it)')
    return _module(v)


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"must be true or false, got {v!r}")
    return v


def _choice(*options: str) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        if v not in options:
            raise ValueError(f"must be one of {', '.join(options)}; got {v!r}")
        return v

    return check


def _extra_commands(v: Any) -> tuple[str, ...]:
    if not isinstance(v, list) or any(c not in EXTRA_COMMANDS for c in v):
        raise ValueError(f"must be a list drawn from {', '.join(EXTRA_COMMANDS)}; got {v!r}")
    return tuple(v)


# section -> key -> (default, check). Keys are unique across sections and name the Config fields.
SPEC: dict[str, dict[str, tuple[Any, Callable[[Any], Any]]]] = {
    "hub": {"host": (HOST, _host), "port": (8799, _port), "registry": ("registry.toml", _text)},
    # OC-1 revised 2026-09-12: not under AppData, which an app's package container silently redirects.
    "paths": {"data_dir": ("%USERPROFILE%\\.rco\\data", _text), "skills_dir": ("skills", _text)},
    "skills": {
        "adapter": (None, _module_required),
        "poll_interval_s": (2, _positive),
        "settle_ms": (500, _count),
        "scan_deadline_ms": (1000, _positive_int),
    },
    "presence": {"staleness_s": (1800, _positive_or_off), "missing_report_s": (1800, _positive_or_off)},
    "transport": {"session_idle_timeout_s": (1800, _idle_timeout)},
    "registration": {
        "re_register_policy": ("idempotent_line", _choice(*POLICIES)),
        "name_mismatch": ("reject", _choice("reject", "warn", "ignore")),
    },
    "records": {"on_write_failure": ("fail_action", _choice("fail_action", "continue", "stop_hub"))},
    "console": {
        "extra_commands": ([], _extra_commands),
        "quiet_until_ready": (True, _bool),
        "color": ("auto", _choice("auto", "never")),
        "level": ("info", _choice("info", "warn")),
    },
    "logs": {
        "diag_level": ("INFO", _choice("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")),
        "diag_max_mb": (5, _positive),
        "diag_backups": (3, _count),
    },
    # Stage 1 design 6.2. The caps are hub-wide, so both apps get the same packets (OC-S13: approximate, re-set
    # after G10); the entry code is D3's 3b, off until decided after G11 (OC-S5); reminders are OC-S22.
    "turns": {
        "packet_cap_chars": (24000, _positive_int),
        "output_cap_chars": (12000, _positive_int),
        "entry_code": ("off", _choice("off", "on")),
        "offer_reminder_s": (900, _positive_or_off),
        "claim_reminder_s": (3600, _positive_or_off),
        "wait_progress_s": (10, _positive),
    },
    # The operator panel (rco.panel): off by default so every test config keeps the console-only behaviour;
    # rco.toml turns it on. The page and its API listen on 127.0.0.1 only, on their own port.
    "panel": {"enabled": (False, _bool), "ui_port": (8798, _port), "open_browser": (True, _bool)},
}
_PATH_KEYS = {"registry": "hub", "data_dir": "paths", "skills_dir": "paths"}
_RENAMED = {"session_idle_timeout_s": "session_idle_timeout", "enabled": "panel_enabled", "ui_port": "panel_port",
            "open_browser": "panel_open_browser"}  # TOML key -> Config field


def expand_vars(text: str, env: Mapping[str, str]) -> str:
    """Expand %VAR% from `env` (case-insensitive, as on Windows); an unset variable is an error."""
    upper = {k.upper(): v for k, v in env.items()}

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name.upper() not in upper:
            raise ValueError(f"environment variable %{name}% is not set")
        return upper[name.upper()]

    return re.sub(r"%([^%\\/:]+)%", sub, text)


def load_config(path: Path, env: Mapping[str, str], port_override: int | None = None) -> Config:
    path = Path(path).resolve()
    doc, sha = read_toml(path)
    for section, table in doc.items():
        if section not in SPEC:
            raise ConfigError(f"{path}: [{section}]: unknown section")
        if not isinstance(table, dict):
            raise ConfigError(f"{path}: {section}: must be a [table]")
    values: dict[str, Any] = {}
    for section, keys in SPEC.items():
        table = doc.get(section, {})
        for key in table:
            if key not in keys:
                raise ConfigError(f"{path}: [{section}] {key}: unknown key")
        for key, (default, check) in keys.items():
            try:
                values[key] = check(table.get(key, default))
            except ValueError as e:
                raise ConfigError(f"{path}: [{section}] {key}: {e}") from None
    # A maximum-size output must still fit in a later packet, beside its header, the task and the note.
    room = values["packet_cap_chars"] - PACKET_MARGIN_CHARS
    if values["output_cap_chars"] > room:
        raise ConfigError(
            f"{path}: [turns] output_cap_chars: {values['output_cap_chars']} is over [turns] packet_cap_chars "
            f"- {PACKET_MARGIN_CHARS} ({room}); a full-size output must fit in a later packet"
        )
    if port_override is not None:
        try:
            values["port"] = _port(port_override)
        except ValueError as e:
            raise ConfigError(f"--port: {e}") from None
    if values["enabled"] and values["ui_port"] == values["port"]:
        raise ConfigError(f"{path}: [panel] ui_port: must differ from the hub port ({values['port']})")
    for key, section in _PATH_KEYS.items():
        try:
            p = Path(expand_vars(values[key], env))
        except ValueError as e:
            raise ConfigError(f"{path}: [{section}] {key}: {e}") from None
        p = p if p.is_absolute() else path.parent / p
        # data_dir stays as given (absolute and normalised, links not followed): the hub compares it with its real
        # path to detect package-container redirection (hub.check_redirection) and records both.
        values[key] = Path(os.path.abspath(p)) if key == "data_dir" else p.resolve()
    for key, name in _RENAMED.items():
        values[name] = values.pop(key)
    return Config(path=path, sha256=sha, **values)


def parse_args(argv: list[str] | None, default_config: Path) -> tuple[Path, int | None]:
    ap = argparse.ArgumentParser(
        prog="mcp_server.py", description="RCO hub (Milestone 1): MCP at http://127.0.0.1:<port>/mcp"
    )
    ap.add_argument("--config", type=Path, default=default_config, help=f"config file (default {default_config})")
    ap.add_argument("--port", type=int, help="override [hub] port")
    ns = ap.parse_args(argv)
    return ns.config, ns.port
