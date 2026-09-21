"""RCO hub (Milestone 1, Stage 1): loop-owned state, the six fixed tools, preflight, app assembly, uvicorn, shutdown.

Startup (design section 1): config -> registry -> tokens -> preflight (package identity, data-dir redirection
guard, data-dir canary + redirection probe, port listener table) -> exclusive bind -> record server_start -> replay
of the run and turn events (Stage 1 design 7.3; silent) -> initial skills scan (notes held back) -> own
SIGINT/SIGBREAK handlers -> uvicorn. The startup lines print once uvicorn serves the pre-bound socket. Effect order
on every state change: validate -> record -> console line -> tool result. Turn and run changes (Stage 1 design 5.6):
engine check -> stage files (atomic) -> strict record (the commit point) -> apply -> views -> console -> result.
"""

import asyncio
import collections
import contextlib
import ctypes
import importlib.metadata
import logging
import logging.handlers
import math
import os
import platform
import re
import signal
import socket
import struct
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TextIO

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import Field
from sse_starlette.sse import AppStatus

from rco import console as con
from rco import turns
from rco.auth import BearerAuth
from rco.config import HOST, Config, ConfigError, load_config, parse_args
from rco.panel import Panel
from rco.records import Records, RecordReadError, RecordWriteError, SchemaError, read_events, utc_now
from rco.registry import Client, Registry, load_registry
from rco.skills import SkillCatalog
from rco.turns import At, Caller, Gate, Plan, Refusal, TurnBook

log = logging.getLogger("rco.hub")

TOOLS = ("claim_turn", "get_turn", "list_skills", "propose_run", "register", "submit_turn")  # sorted, as _serve compares
MIN_TOKEN_LEN = 32
WARN_REPEAT_S = 10  # an identical turn [WARN] line for the same client within 10 s is recorded, not printed (4.3 (a))
MAX_WAIT_PASS_S = 1.0  # the dormant wait re-checks hub.mono at least once per real second (4.7; FakeClock)
NO_PACKAGE = 15700  # APPMODEL_ERROR_NO_PACKAGE from GetCurrentPackageFullName
CLOSE_EVENTS = (2, 5, 6)  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
REGISTER_DESCRIPTION = (
    "Registers this app with the local RCO hub. Call only when the user explicitly typed the middleware "
    "registration command; never from background, title or summary tasks. Relay the returned text verbatim."
)
LIST_SKILLS_DESCRIPTION = (
    "Lists the skills the local RCO hub has discovered, as JSON data (the same for every app). "
    "Call it when the user asks which skills are available."
)
# Stage 1 design 4.1 (exact). Client-neutral, and true again when Stage 2 turns waits on.
GET_TURN_DESCRIPTION = (
    "Fetches this app's current turn in an RCO run from the local hub. Call only while carrying out the RCO turn "
    "skill the user invoked; never from background, title or summary tasks. The first line of the result states the "
    "outcome."
)
CLAIM_TURN_DESCRIPTION = (
    "Claims an RCO turn that get_turn offered to this app and returns its work packet. Call only while carrying out "
    "the RCO turn skill the user invoked, with the turn_id and offer from that get_turn result."
)
SUBMIT_TURN_DESCRIPTION = (
    "Submits the result of the RCO turn this app claimed, with the turn_id and nonce from the claim result. Call only "
    "while carrying out the RCO turn skill the user invoked; never from background, title or summary tasks."
)
PROPOSE_RUN_DESCRIPTION = (
    "Proposes a new RCO run to the operator at the hub console; nothing starts until the operator opens it. Call only "
    "while carrying out the RCO turn skill the user invoked with a run request; never from background, title or "
    "summary tasks."
)
PARAM = {  # the parameter descriptions of 4.1 (exact)
    "wait_s": "Seconds the hub may hold this call waiting for a turn. Omit it: the hub then uses this app's limit, "
    "and never holds longer than that limit.",
    "code": "The code the user typed after the turn command, if any.",
    "turn_id": "The turn_id from the get_turn or claim result.",
    "offer": "The offer value from the get_turn result.",
    "nonce": "The nonce from the claim result, exactly.",
    "status": "done, blocked or declined. Omit for done. Give the reason in output when blocked or declined.",
    "output": "Your result, starting with a one-line summary. Stay within the output limit in the claim result; put "
    "larger material in files and give their full paths.",
    "files_changed": "Full paths of the files you created or changed during this turn, one per line. Omit it if "
    "there are none.",
    "task": "The run's task as the user gave it.",
}


class StartupFailure(Exception):
    """The hub cannot start; the message is printed as-is and the process exits with code 2."""


# ------------------------------------------------------------------------------------------ outside world


def read_hkcu_env(name: str) -> str | None:
    """Read (never write) a user environment variable from HKCU\\Environment."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as k:
            value, kind = winreg.QueryValueEx(k, name)
    except (ImportError, OSError):
        return None
    if not isinstance(value, str):
        return None
    return os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else value


def package_identity_status() -> int:
    if os.name != "nt":
        return NO_PACKAGE
    n = ctypes.c_uint32(0)
    return ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(n), None)


def parse_tcp_table(buf: bytes, family: int, port: int) -> list[tuple[str, int]]:
    """(address, pid) of each row on `port` in a GetExtendedTcpTable(TCP_TABLE_OWNER_PID_LISTENER) buffer."""
    (count,) = struct.unpack_from("<I", buf, 0)
    found = []
    for i in range(count):
        if family == socket.AF_INET:  # MIB_TCPROW_OWNER_PID: state, local addr, local port, remote addr, port, pid
            _, addr, lport, _, _, pid = struct.unpack_from("<6I", buf, 4 + 24 * i)
            if socket.ntohs(lport & 0xFFFF) == port:
                found.append((socket.inet_ntoa(struct.pack("<I", addr)), pid))
        else:  # MIB_TCP6ROW_OWNER_PID: local addr[16], scope, local port, remote addr[16], scope, port, state, pid
            off = 4 + 56 * i
            (lport,) = struct.unpack_from("<I", buf, off + 20)
            (pid,) = struct.unpack_from("<I", buf, off + 52)
            if socket.ntohs(lport & 0xFFFF) == port:
                found.append((socket.inet_ntop(socket.AF_INET6, bytes(buf[off : off + 16])), pid))
    return found


def tcp_listeners(port: int) -> list[tuple[str, int]]:
    """(address, pid) of every IPv4 and IPv6 LISTEN socket on `port`, read from the TCP tables (read-only).
    IPv6 matters: a dual-stack [::] listener also takes IPv4 loopback traffic, like a 0.0.0.0 one."""
    if os.name != "nt":
        return []
    get_table = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    found = []
    for family in (socket.AF_INET, socket.AF_INET6):
        size = ctypes.c_ulong(0)
        for _ in range(5):  # the table can grow between the size query and the read
            buf = ctypes.create_string_buffer(max(size.value, 4))
            rc = get_table(buf, ctypes.byref(size), False, family, 3, 0)  # 3 = TCP_TABLE_OWNER_PID_LISTENER
            if rc == 0:
                found += parse_tcp_table(buf.raw, family, port)
                break
            if rc != 122:  # ERROR_INSUFFICIENT_BUFFER
                break
    return found


@dataclass
class World:
    """Everything the hub touches outside itself. Production uses these defaults; tests inject fakes."""

    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    read_user_env: Callable[[str], str | None] = read_hkcu_env
    out: TextIO | None = None  # console sink; None = sys.stdout
    keys: con.KeySource | None = None  # None = msvcrt on a real console, else a stdin line reader
    package_identity: Callable[[], int] = package_identity_status
    listeners: Callable[[int], list[tuple[str, int]]] = tcp_listeners
    monotonic: Callable[[], float] = time.monotonic
    sock: socket.socket | None = None  # TEST-ONLY pre-bound socket; production binds its own (port 0 stays refused)
    signals: bool = True
    realpath: Callable[[str], str] = os.path.realpath  # where a path really lives (links, container redirection)
    panel_sock: socket.socket | None = None  # TEST-ONLY pre-bound socket for the operator panel
    browser: Callable[[str], None] = lambda url: threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()


# ------------------------------------------------------------------------------------------ preflight


def load_tokens(registry: Registry, world: World) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """OC-17: every client needs its own token of at least 32 characters; process env first, then HKCU."""
    env = {k.upper(): v for k, v in world.env.items()}
    tokens: dict[str, str] = {}
    sources: dict[str, dict[str, str]] = {}
    for c in registry.clients:
        where = f'{registry.path} [[client]] id "{c.id}", auth_env'
        value, source = (env.get(c.auth_env.upper()) or "").strip(), "process-env"
        if not value:
            value, source = (world.read_user_env(c.auth_env) or "").strip(), "hkcu"
        if not value:
            raise StartupFailure(
                f"[ERROR] No token for {c.display_name}: the environment variable {c.auth_env} is not set ({where}).\n"
                f"        Set it at Windows user level, then restart the hub and the app."
            )
        if len(value) < MIN_TOKEN_LEN:
            raise StartupFailure(
                f"[ERROR] The token in {c.auth_env} for {c.display_name} is too short: {len(value)} characters, "
                f"at least {MIN_TOKEN_LEN} needed ({where})."
            )
        for other, tok in tokens.items():
            if tok == value:
                raise StartupFailure(
                    f"[ERROR] {registry.get(other).auth_env} and {c.auth_env} hold the same token: each app needs "
                    f"its own ({where})."
                )
        tokens[c.id] = value
        sources[c.id] = {"env_var": c.auth_env, "source": source}  # the source only, never the value
    return tokens, sources


def identity_name(status: int) -> str:
    return {NO_PACKAGE: "none", 122: "packaged"}.get(status, f"unknown ({status})")


def check_package_identity(identity: str, data_dir: Path, env: Mapping[str, str]) -> None:
    upper = {k.upper(): v for k, v in env.items()}
    roots = [Path(os.path.abspath(upper[k])) for k in ("LOCALAPPDATA", "APPDATA") if upper.get(k)]
    if identity != "packaged" or not any(data_dir == r or r in data_dir.parents for r in roots):
        return
    venv = Path(upper.get("USERPROFILE", "%USERPROFILE%")) / ".rco" / "venv" / "Scripts" / "python.exe"
    raise StartupFailure(
        "[ERROR] This Python runs with a package identity (the WindowsApps alias or a Store install), so Windows\n"
        f"        redirects its writes under %LOCALAPPDATA% and {data_dir} would not be where you look for it.\n"
        f'        Launch the hub from the project folder with the venv interpreter: & "{venv}" mcp_server.py'
    )


# An app package's private storage: ...\Packages\<package family name>\LocalCache\...
PACKAGE_CACHE = re.compile(r"\\Packages\\([^\\]+)\\LocalCache(?:\\|$)", re.IGNORECASE)


def real_data_dir(data_dir: Path, realpath: Callable[[str], str]) -> str:
    """Where `data_dir` really lives, without creating anything: the real path of its nearest existing
    ancestor (the folder itself once it exists), joined with the part that does not exist yet."""
    probe, tail = os.path.abspath(data_dir), []
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        tail.append(os.path.basename(probe))
        probe = parent
    return os.path.join(realpath(probe), *reversed(tail))


def probe_real_dir(data_dir: Path, realpath: Callable[[str], str]) -> str:
    """Where a file newly created in the (existing) `data_dir` really lands. In a package container a folder that
    already exists, such as the parent of a fresh data folder, looks real while whatever is created in it is
    redirected, so only a new file shows it (real_data_dir cannot). The probe is removed at once."""
    probe = data_dir / f".rco-probe-{uuid.uuid4().hex}"
    probe.touch(exist_ok=False)
    try:
        return os.path.dirname(realpath(str(probe)))
    finally:
        probe.unlink(missing_ok=True)


def check_redirection(data_dir: Path, config_path: Path, real: str) -> None:
    """OC-1 revised: a process inside an app's package container (e.g. an agent's shell in a desktop app) has
    its AppData writes redirected into that package's LocalCache, which the operator's own shells never see,
    and GetCurrentPackageFullName does not reveal it (the package-identity guard misses it). `real` is where
    data_dir really is (real_data_dir before the canary, probe_real_dir after it). Refused when that lies in an
    app package's LocalCache and the configured path does not. Any other difference (a junction or symlink the
    operator made, a long name for an 8.3 one, a \\\\?\\ prefix) is allowed; server_start records data_dir_realpath."""
    given = os.path.abspath(data_dir)
    if os.path.normcase(os.path.abspath(real)) == os.path.normcase(given):
        return
    package = PACKAGE_CACHE.search(real.replace("/", "\\"))
    if package is None or PACKAGE_CACHE.search(given):  # not package storage, or configured there on purpose
        return
    raise StartupFailure(
        f"[ERROR] The data folder {given} is redirected to {real} (an app package's private storage).\n"
        f"        Likely cause: the hub was started inside an app's package container ({package.group(1)}), e.g. by an "
        f"agent working in that desktop app; files written there are invisible to your own shells.\n"
        f"        Fix: start the hub from a Windows PowerShell window opened from the Start menu, or set "
        f"[paths] data_dir outside AppData in {config_path}, then restart."
    )


def canary_creates(data_dir: Path) -> list[Path]:
    """What write_canary is about to create (nothing listed exists yet), deepest first."""
    missing, p = [], data_dir
    while not p.exists() and p.parent != p:
        missing.append(p)
        p = p.parent
    return [x for x in (data_dir / ".rco-canary", data_dir / "records", data_dir / "logs") if not x.exists()] + missing


def undo_created(paths: list[Path]) -> None:
    """Remove what canary_creates listed: the canary file and folders that are still empty, never anything else."""
    for p in paths:
        with contextlib.suppress(OSError):
            if p.is_dir():
                p.rmdir()
            else:
                p.unlink()


def write_canary(data_dir: Path) -> None:
    """Proves writability only (a virtualized process would see its own redirected file)."""
    for sub in ("records", "logs"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    canary = data_dir / ".rco-canary"
    token = uuid.uuid4().hex
    canary.write_text(token, encoding="utf-8")
    if canary.read_text(encoding="utf-8") != token:
        raise OSError(f"read-back of {canary} did not match")


def write_atomic(path: str, text: str, make_parent: bool = False) -> None:
    """Stage 1 design 7.2: <name>.tmp in the same folder, flushed, fsynced, then os.replace. UTF-8, no BOM, and the
    engine's \\n line ends, written as bytes like the record file. Only the late-copy folder is created here: a
    run folder is created exclusively at `run`, never implied by a later write."""
    if make_parent:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(text.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_file(path: str) -> bytes | None:
    """File bytes for the engine; None when the file is missing. Any other failure raises OSError."""
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def port_message(port: int, config_path: Path, cause: str) -> str:
    return (
        f"[ERROR] Port {port} on {HOST} is not available: {cause}.\n"
        f"        Change [hub] port in {config_path} or pass --port <N>, then restart. No fallback port is used.\n"
        f"        Diagnose: Get-NetTCPConnection -LocalPort {port} -State Listen; "
        f"netsh int ipv4 show excludedportrange protocol=tcp"
    )


def bind_port(port: int, world: World) -> socket.socket:
    """Listener-table preflight, then an exclusive bind. uvicorn's own bind sets SO_REUSEADDR, with which two
    Windows listeners silently share a port; SO_EXCLUSIVEADDRUSE refuses that. The table catches a 0.0.0.0
    listener, which an exclusive 127.0.0.1 bind does not detect. 0.0.0.0 is never probe-bound."""
    for addr, pid in world.listeners(port):
        if addr in ("0.0.0.0", HOST, "::", "::1"):
            shown = f"[{addr}]" if ":" in addr else addr
            raise PortUnavailable(port, f"in use by a listener on {shown} (PID {pid})")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind((HOST, port))
    except OSError as e:
        s.close()
        winerror = getattr(e, "winerror", None)
        cause = {
            10048: "in use (WinError 10048)",
            10013: "reserved or access denied (WinError 10013)",
        }.get(winerror, f"{e.strerror or e} (WinError {winerror})" if winerror else f"{e.strerror or e}")
        raise PortUnavailable(port, cause) from None
    return s


def panel_port_message(port: int, config_path: Path, cause: str) -> str:
    return (
        f"[ERROR] The operator panel port {port} on {HOST} is not available: {cause}.\n"
        f"        Change [panel] ui_port in {config_path} (or set enabled = false), then restart."
    )


class PortUnavailable(Exception):
    def __init__(self, port: int, cause: str) -> None:
        super().__init__(cause)
        self.port, self.cause = port, cause


class _DiagHandler(logging.handlers.RotatingFileHandler):
    """Never prints. logging's default handleError writes a traceback to stderr, the operator's console; here
    a failure (a full disk, or a rotation blocked by a reader that does not share delete) is only counted.
    A failed rotation is retried after a minute and writing goes on in the current file."""

    failures = 0
    _rotate_after = 0.0

    def _open(self) -> Any:  # UTF-8 with \n line ends, like the record file
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors, newline="\n")

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if time.monotonic() < self._rotate_after:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            self._rotate_after = time.monotonic() + 60
            if self.stream is None:
                self.stream = self._open()

    def handleError(self, record: logging.LogRecord) -> None:
        self.failures += 1


def install_diag_logging(cfg: Config) -> logging.Handler:
    """Root logging goes to the diag file before MCPServer exists, so its basicConfig becomes a no-op and no
    SDK or uvicorn log line can reach the console."""
    logging.raiseExceptions = False  # backstop: no handler error ever prints to the console
    handler = _DiagHandler(
        cfg.data_dir / "logs" / "hub-diag.log",
        maxBytes=int(cfg.diag_max_mb * 1024 * 1024),
        backupCount=cfg.diag_backups,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(cfg.diag_level)
    logging.captureWarnings(True)
    return handler


# ------------------------------------------------------------------------------------------ state


@dataclass
class ClientState:
    registered: bool = False
    registered_at: float | None = None  # wall clock, for status
    first_registered_at: str | None = None  # UTC, for records
    register_count: int = 0
    transport_seen: bool = False  # never prints "connected"
    last_seen_wall: float | None = None
    last_seen_mono: float | None = None
    open_requests: int = 0  # ASGI calls in flight, streams included
    era: str | None = None
    client_info: str | None = None
    stale: bool = False
    missing_reported: bool = False


class _Server(uvicorn.Server):
    def capture_signals(self) -> Any:  # the hub owns SIGINT/SIGBREAK; uvicorn's re-raise would end in a traceback
        return contextlib.nullcontext()


def _era(protocol_version: str | None) -> str | None:
    if protocol_version is None:
        return None
    return protocol_version if protocol_version in MODERN_PROTOCOL_VERSIONS else f"legacy:{protocol_version}"


def _duration(seconds: float) -> str:
    return f"{seconds / 60:g} min" if seconds >= 60 and seconds % 60 == 0 else f"{seconds:g} s"


def build_app(hub: "Hub", tokens: Mapping[str, str]) -> tuple[MCPServer, BearerAuth]:
    """The production assembly (also used by tests): MCPServer with the fixed tools, wrapped by the auth layer."""
    mcp = MCPServer("rco", version="m1")

    async def register(
        name: Annotated[str, Field(description="The word the user typed after the registration command.")],
        ctx: Context,
    ) -> CallToolResult:
        return await hub.register(name, ctx)

    async def list_skills(ctx: Context) -> CallToolResult:
        return await hub.list_skills()

    # Stage 1 design 4.1: bare types with a description only; value checks run in the hub, so they are recorded.
    async def get_turn(
        ctx: Context,
        wait_s: Annotated[float | None, Field(description=PARAM["wait_s"])] = None,
        code: Annotated[str | None, Field(description=PARAM["code"])] = None,
    ) -> CallToolResult:
        return await hub.get_turn(wait_s, code, ctx)

    async def claim_turn(
        turn_id: Annotated[str, Field(description=PARAM["turn_id"])],
        offer: Annotated[str, Field(description=PARAM["offer"])],
        ctx: Context,
    ) -> CallToolResult:
        return await hub.claim_turn(turn_id, offer, ctx)

    async def submit_turn(
        turn_id: Annotated[str, Field(description=PARAM["turn_id"])],
        nonce: Annotated[str, Field(description=PARAM["nonce"])],
        output: Annotated[str, Field(description=PARAM["output"])],
        ctx: Context,
        status: Annotated[str | None, Field(description=PARAM["status"])] = None,
        files_changed: Annotated[str | None, Field(description=PARAM["files_changed"])] = None,
    ) -> CallToolResult:
        return await hub.submit_turn(turn_id, nonce, output, status, files_changed, ctx)

    async def propose_run(task: Annotated[str, Field(description=PARAM["task"])], ctx: Context) -> CallToolResult:
        return await hub.propose_run(task, ctx)

    mcp.add_tool(register, name="register", description=REGISTER_DESCRIPTION, structured_output=False)
    mcp.add_tool(list_skills, name="list_skills", description=LIST_SKILLS_DESCRIPTION, structured_output=False)
    for fn, desc in (
        (get_turn, GET_TURN_DESCRIPTION), (claim_turn, CLAIM_TURN_DESCRIPTION), (submit_turn, SUBMIT_TURN_DESCRIPTION),
        (propose_run, PROPOSE_RUN_DESCRIPTION),
    ):  # no annotations (D5), as register
        mcp.add_tool(fn, name=fn.__name__, description=desc, structured_output=False)
    app = mcp.streamable_http_app(host=HOST, session_idle_timeout=hub.cfg.session_idle_timeout)
    auth_env = {c.id: c.auth_env for c in hub.registry.clients}
    return mcp, BearerAuth(app, tokens, auth_env, hub)


class Hub:
    @classmethod
    def create(
        cls,
        argv: list[str] | None,
        world: World | None = None,
        default_config: Path | None = None,
        console: con.Console | None = None,
    ) -> "Hub":
        world = world or World()
        console = console or con.open_console(world.out or sys.stdout, world.env)
        config_path, port = parse_args(argv, default_config or Path.cwd() / "rco.toml")
        try:
            cfg = load_config(config_path, world.env, port)
            registry = load_registry(cfg.registry)
        except ConfigError as e:
            raise StartupFailure(f"[ERROR] {e}") from None
        tokens, sources = load_tokens(registry, world)
        identity = identity_name(world.package_identity())
        check_package_identity(identity, cfg.data_dir, world.env)
        # OC-1 revised: no data folder in an app package's private storage. Checked first from what exists (the folder
        # or its nearest parent) before anything is created, then from where a new file lands, because in a package
        # container a fresh folder's parent looks real. A refusal at the second check removes what this start created.
        check_redirection(cfg.data_dir, cfg.path, real_data_dir(cfg.data_dir, world.realpath))
        created = canary_creates(cfg.data_dir)
        try:
            write_canary(cfg.data_dir)
            real = probe_real_dir(cfg.data_dir, world.realpath)
        except OSError as e:
            raise StartupFailure(
                f"[ERROR] The data directory {cfg.data_dir} is not writable: {e}.\n"
                f"        Change [paths] data_dir in {cfg.path}, then restart."
            ) from None
        try:
            check_redirection(cfg.data_dir, cfg.path, real)
        except StartupFailure:
            undo_created(created)
            raise
        sock = world.sock
        if sock is None:
            try:
                sock = bind_port(cfg.port, world)
            except PortUnavailable as e:
                _record_start_failure(cfg, e.cause, e.port)
                raise StartupFailure(port_message(e.port, cfg.path, e.cause)) from None
        panel_sock = world.panel_sock
        if cfg.panel_enabled and panel_sock is None:
            try:
                panel_sock = bind_port(cfg.panel_port, world)
            except PortUnavailable as e:
                if world.sock is None:
                    sock.close()
                _record_start_failure(cfg, f"panel: {e.cause}", e.port)
                raise StartupFailure(panel_port_message(e.port, cfg.path, e.cause)) from None
        return cls(cfg, registry, world, console, tokens, sources, identity, sock, panel_sock)

    def __init__(
        self,
        cfg: Config,
        registry: Registry,
        world: World,
        console: con.Console,
        tokens: dict[str, str],
        sources: dict[str, dict[str, str]],
        identity: str,
        sock: socket.socket,
        panel_sock: socket.socket | None = None,
    ) -> None:
        self.cfg, self.registry, self.world, self.console, self.sock = cfg, registry, world, console, sock
        console.color = console.colorable and cfg.color == "auto"
        # The operator panel mirrors every console line (a bounded log with a running sequence number).
        self.log: collections.deque[tuple[int, str]] = collections.deque(maxlen=3000)
        self.log_seq = 0
        console.tap = self._tap
        self.panel_sock = panel_sock
        self.panel: Panel | None = None
        self.panel_server: _Server | None = None
        self.mono = world.monotonic
        self.started_mono = self.mono()
        self.hub_run = str(uuid.uuid4())
        self.port = sock.getsockname()[1]
        self.url = f"http://{HOST}:{self.port}/mcp"
        self.state = {c.id: ClientState() for c in registry.clients}
        self.paused = False
        self.ready_announced = False
        self.records_healthy = True
        self.first_registration_mono: float | None = None
        self.extra_commands = set(cfg.extra_commands) | (
            {"release"} if cfg.re_register_policy == "refuse_until_release" else set()
        ) | ({"panel"} if panel_sock is not None else set())
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: _Server | None = None
        self.started = threading.Event()  # set once the startup lines are on the console
        self._stopping = False
        self._held: list[str] | None = []  # console notes held back until the startup lines print
        self._placeholder_warned: set[str] = set()
        self._hint_warned: set[tuple[str, str]] = set()
        # auth_rejected per (reason, env_var): [UTC minute, rejections that minute, how many a record covers]
        self._auth: dict[tuple[str, str | None], list[int]] = {}
        self._register_lock: asyncio.Lock | None = None
        self._pending_stop: str | None = None  # a stop asked for before the loop ran
        self._stop_recorded = threading.Event()  # set once server_stop has been written (or failed)
        self._ctrl_ref: Any = None  # the native console control handler, kept alive while installed
        # Stage 1 (turns): the entry code is evaluated from the config in force (4.3 note (c)).
        self.code_mode = cfg.entry_code == "on"
        self._warned: dict[tuple[str, str], float] = {}  # (client, [WARN] line) -> mono it last printed
        self._resume_mono: float | None = None  # the last resume: reminder ages count from it (5.7)
        self._turn_changed = asyncio.Event()  # replaced on every turn-state change; wakes the dormant waits (4.7)

        self._diag = install_diag_logging(cfg)
        try:
            self.records = Records(cfg.data_dir / "records" / "rco-records.jsonl", self.hub_run)
            self.records.write(
                "server_start", host=HOST, port=self.port, url=self.url, pid=os.getpid(),
                python_exe=sys.executable, python_version=platform.python_version(),
                mcp_version=importlib.metadata.version("mcp"), config_path=str(cfg.path), config_sha256=cfg.sha256,
                registry_path=str(registry.path), registry_sha256=registry.sha256, data_dir=str(cfg.data_dir),
                data_dir_realpath=real_data_dir(cfg.data_dir, world.realpath), skills_dir=str(cfg.skills_dir),
                adapter=cfg.adapter, package_identity=identity,
                canary_writable=True, token_sources=sources, staleness_s=cfg.staleness_s,
                session_idle_timeout=cfg.session_idle_timeout,
            )
        except OSError as e:
            self._release_resources()
            raise StartupFailure(f"[ERROR] Cannot write the record file under {cfg.data_dir}: {e}") from None
        # Stage 1 design 7.3: rebuild run and turn state from the record file, silently (no record, no console line,
        # no file written). An unreadable history refuses the start rather than hide behind empty state.
        self.runs_dir = cfg.data_dir / "records" / "runs"
        self.book = TurnBook.create(
            registry.clients, str(self.runs_dir), str(cfg.data_dir), cfg.packet_cap_chars, cfg.output_cap_chars
        )
        t0 = time.monotonic()
        try:
            history = read_events(self.records.path)
        except RecordReadError as e:
            self._release_resources()
            raise StartupFailure(f"[ERROR] Cannot read the record file {self.records.path}: {e.strerror}") from None
        skipped = turns.replay(history, self.book, self._read_quiet, self.code_mode)
        for why in skipped:
            log.warning("replay skipped a record line: %s", why)
        live = self.book.live_turn
        if live is not None and live.state == "broken":  # detection is unrecorded (5.2): the diag log only
            log.warning("replay: the packet file of %s is missing or changed; the turn is broken", live.id)
        log.info(
            "replay: %d record lines in %.3f s; %d skipped; open run %s", len(history), time.monotonic() - t0,
            len(skipped), self.book.run.id if self.book.run else "none",
        )
        try:
            self.skills = SkillCatalog(
                cfg.skills_dir, cfg.adapter, settle_ms=cfg.settle_ms, deadline_ms=cfg.scan_deadline_ms,
                emit=self._skill_event,
            )
        except ImportError as e:
            self._release_resources()
            raise StartupFailure(f"[ERROR] {cfg.path}: [skills] adapter: cannot load '{cfg.adapter}': {e}") from None
        self.mcp, self.app = build_app(self, tokens)
        if panel_sock is not None:
            self.panel = Panel(self)

    # ------------------------------------------------------------------ console and records

    def _tap(self, texts: list[str]) -> None:  # under the console lock, from any thread
        for text in texts:
            self.log_seq += 1
            self.log.append((self.log_seq, text))

    def say(self, *lines: str, info: bool = False) -> None:
        """Print hub lines (held back until the startup lines are out). info=True lines hide at level 'warn'."""
        if info and self.cfg.level == "warn":
            return
        if self._held is not None:
            self._held.extend(lines)
        else:
            self.console.lines(list(lines))

    def record(self, event: str, **fields: Any) -> bool:
        """Append one record. False means the caller must not apply its effect (OC-22)."""
        try:
            self.records.write(event, **fields)
        except RecordWriteError as e:
            self._write_failed(e)
            return self.cfg.on_write_failure == "continue"
        self._write_ok()
        return True

    def record_turn(self, event: str, **fields: Any) -> dict[str, Any] | None:
        """Strict record for run and turn events (Stage 1 design 7.4): None on any write failure, whatever
        on_write_failure says, because replay cannot rebuild an unrecorded effect. The one-time [ERROR] and stop_hub
        still apply. A SchemaError (a programming error) is logged and raised: a tool handler turns it into
        'refused internal', a console command into the done-callback's [ERROR] line."""
        try:
            rec = self.records.write(event, **fields)
        except RecordWriteError as e:
            self._write_failed(e)
            return None
        except SchemaError:
            log.exception("record %s does not match the schema", event)
            raise
        self._write_ok()
        return rec

    def _write_failed(self, e: RecordWriteError) -> None:
        log.error("record write failed: %s", e)
        if self.records_healthy:
            self.records_healthy = False
            self.say(f"[ERROR] Cannot write the record file: {e}. Actions that need a record are refused.")
        if self.cfg.on_write_failure == "stop_hub" and not self._stopping:
            self.request_stop("error")

    def _write_ok(self) -> None:
        if not self.records_healthy:
            self.records_healthy = True
            self.say("[INFO] The record file is writable again.")

    def _skill_event(self, event: str, fields: dict[str, Any], line: str | None) -> bool:
        ok = self.record(event, **fields)
        if ok and line:
            self.say(line, info=line.startswith("[INFO]"))
        return ok

    # ------------------------------------------------------------------ presence (AuthObserver)

    def _touch(self, st: ClientState) -> None:
        st.last_seen_wall, st.last_seen_mono = time.time(), self.mono()

    def request_started(self, client_id: str, protocol_version: str | None) -> None:
        st = self.state[client_id]
        st.transport_seen = True
        st.open_requests += 1
        self._touch(st)
        if protocol_version:
            st.era = _era(protocol_version)

    def request_finished(self, client_id: str) -> None:
        st = self.state[client_id]
        st.open_requests -= 1
        self._touch(st)

    def auth_rejected(self, reason: str, env_var: str | None) -> None:
        """Counted per (reason, env_var) per UTC minute: the minute's first rejection is recorded at once
        (count_minute 1); the rest are counted in memory and recorded as one more line with the minute's total
        when the minute ends (1 s monitor tick) or the hub stops. A client retrying a bad token therefore adds
        at most two lines a minute, not one fsynced line per request."""
        key = (reason, env_var)
        minute = int(time.time() // 60)
        agg = self._auth.get(key)
        if agg is not None and agg[0] != minute:
            if agg[1] > agg[2]:
                self._record_auth(key)
            agg = None
        if agg is None:
            agg = self._auth[key] = [minute, 0, 0]
        agg[1] += 1
        if agg[2] == 0 and self._record_auth(key):  # nothing recorded yet this minute (or the last try failed)
            if env_var and env_var not in self._placeholder_warned:
                self._placeholder_warned.add(env_var)
                c = next(c for c in self.registry.clients if c.auth_env == env_var)
                self.say(
                    f"[WARN] {c.display_name} sent the literal ${{{env_var}}}: the app cannot see the variable. "
                    f"Set it at Windows user level, then fully restart the app."
                )

    def _record_auth(self, key: tuple[str, str | None]) -> bool:
        agg = self._auth[key]
        reason, env_var = key
        fields: dict[str, Any] = {
            "reason": reason, "count_minute": agg[1], "minute": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(agg[0] * 60)),
        }
        if env_var:
            fields["env_var"] = env_var
        ok = self.record("auth_rejected", **fields)
        if ok:
            agg[2] = agg[1]
        return ok

    def flush_auth(self, final: bool = False) -> None:
        """Record the total of every ended minute (at stop: also the current one) that no record covers yet."""
        minute = int(time.time() // 60)
        for key, agg in list(self._auth.items()):
            if final or agg[0] != minute:
                if agg[1] > agg[2]:
                    self._record_auth(key)
                del self._auth[key]

    def present(self, st: ClientState) -> bool:
        if st.open_requests > 0:
            return True
        if st.last_seen_mono is None:
            return False
        return self.cfg.staleness_s is None or self.mono() - st.last_seen_mono < self.cfg.staleness_s

    def quorum(self) -> dict[str, Any]:
        waiting = []
        for c in self.registry.clients:
            st = self.state[c.id]
            if not st.registered:
                waiting.append(f"{c.display_name} not registered")
            elif not self.present(st):
                waiting.append(f"{c.display_name} silent")
        if self.paused:
            waiting.append("hub paused")
        n = sum(1 for st in self.state.values() if st.registered and self.present(st))
        return {"ok": not waiting, "waiting": waiting, "present": n, "total": len(self.state)}

    def quorum_ok(self) -> bool:
        """Every registry client registered and present, hub not paused. Future two-model tools must pass it."""
        return self.quorum()["ok"]

    def _presence_fields(self, c: Client, threshold: float | None) -> dict[str, Any]:
        st = self.state[c.id]
        age = None if st.last_seen_mono is None else round(self.mono() - st.last_seen_mono, 1)
        return {"client": c.id, "last_seen_age_s": age, "open_requests": st.open_requests, "threshold_s": threshold}

    def _active_again(self, c: Client) -> None:
        st = self.state[c.id]
        if st.stale and self.record("client_active_again", **self._presence_fields(c, self.cfg.staleness_s)):
            st.stale = False
            self.say(f"[INFO] {c.display_name} ({c.handle}) active again.", info=True)

    def check_presence(self) -> None:
        now = self.mono()
        for c in self.registry.clients:
            st = self.state[c.id]
            if st.registered:
                if self.present(st):
                    self._active_again(c)
                elif not st.stale and self.record("client_stale", **self._presence_fields(c, self.cfg.staleness_s)):
                    st.stale = True
                    assert self.cfg.staleness_s is not None
                    self.say(
                        f"[WARN] {c.display_name} ({c.handle}) silent for {_duration(self.cfg.staleness_s)}; "
                        f"hub waiting for the operator."
                    )
            elif (
                not st.missing_reported
                and self.cfg.missing_report_s is not None
                and self.first_registration_mono is not None
                and now - self.first_registration_mono >= self.cfg.missing_report_s
                and self.record("client_missing", **self._presence_fields(c, self.cfg.missing_report_s))
            ):
                st.missing_reported = True
                self.say(f"[WARN] Still waiting for {c.display_name} ({c.handle}): not registered; {c.invocation_hint}.")

    # ------------------------------------------------------------------ tools

    @staticmethod
    def _text(text: str, **kw: Any) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=text)], **kw)

    def _refuse(self, c: Client, name: str, reason: str) -> CallToolResult:
        if self.record("client_register_rejected", client=c.id, name_arg=name, reason=reason):
            self.say(f"[WARN] Registration refused for {c.display_name} ({c.handle}): {reason}")
        return self._text(f"RCO hub: registration refused: {reason}", is_error=True)

    async def register(self, name: str, ctx: Context) -> CallToolResult:
        client_id = getattr(ctx.request_context.request.state, "rco_client", None)
        if client_id not in self.state:  # unreachable behind the auth layer
            return self._text("RCO hub: registration refused: unauthenticated", is_error=True)
        await self.skills.rescan()  # outside the lock: a slow scan never queues other registrations behind it
        assert self._register_lock is not None
        async with self._register_lock:  # two first registrations arriving together print one connected line
            return self._register(self.registry.get(client_id), name, ctx)

    def _register(self, c: Client, name: str, ctx: Context) -> CallToolResult:
        """Synchronous under the lock: nothing can interleave between the checks and the state change."""
        st = self.state[c.id]
        skill_count = self.skills.count
        name_warning = None
        if not self.registry.accepts_name(c, name):
            reason = f"name '{name.strip()}' does not match this app's token (expected {' or '.join(c.invocation_names)})"
            if self.cfg.name_mismatch == "reject":
                return self._refuse(c, name, reason)
            if self.cfg.name_mismatch == "warn":  # printed once the registration is recorded
                name_warning = f"[WARN] {c.display_name} ({c.handle}) registered with {reason}; identity comes from the token."
        try:
            params = ctx.session.client_params
        except Exception:
            params = None
        info = params.client_info if params is not None else None
        client_info = info.name if info is not None else None
        if client_info is not None and client_info not in c.client_info_hints:
            hinted = self.record(
                "identity_hint_mismatch", client=c.id, client_info=client_info, expected_hints=list(c.client_info_hints)
            )
            if hinted and (c.id, client_info) not in self._hint_warned:
                self._hint_warned.add((c.id, client_info))
                expected = ", ".join(c.client_info_hints) or "none"
                self.say(
                    f"[WARN] {c.display_name} ({c.handle}) presented clientInfo '{client_info}' (expected {expected}); "
                    f"identity comes from the token."
                )
        if st.registered and self.cfg.re_register_policy == "refuse_until_release":
            return self._refuse(c, name, f"{c.display_name} is already registered (the operator can type 'release {c.id}')")

        session = (ctx.headers or {}).get("mcp-session-id")
        registration_id = str(uuid.uuid4())
        era = _era(ctx.protocol_version)
        fields = dict(
            client=c.id, handle=c.handle, registration_id=registration_id, name_arg=name, era=era,
            client_info=client_info, session=session[:8] if session else None, skill_count=skill_count,
        )
        if st.registered:
            count = st.register_count + 1
            if not self.record("client_re_registered", **fields, count=count, first_registered_at=st.first_registered_at):
                return self._text("RCO hub: registration refused: the hub cannot write its record", is_error=True)
            st.register_count, st.era, st.client_info = count, era, client_info
            if name_warning:
                self.say(name_warning)
            policy = self.cfg.re_register_policy
            if policy == "idempotent_line" and (self.ready_announced or not self.cfg.quiet_until_ready):
                self.say(f"[INFO] {c.display_name} re-registered ({c.handle}) #{count}", info=True)
            elif policy == "line_after_stale":
                self._active_again(c)
        else:
            if not self.record("client_registered", **fields):
                return self._text("RCO hub: registration refused: the hub cannot write its record", is_error=True)
            st.registered, st.register_count, st.stale = True, 1, False
            st.registered_at, st.first_registered_at = time.time(), utc_now()
            st.era, st.client_info = era, client_info
            if self.first_registration_mono is None:
                self.first_registration_mono = self.mono()
            if name_warning:
                self.say(name_warning)
            self.say(self.registry.connected_line(c))
            self._maybe_ready()
        meta = {"rco/handle": c.handle, "rco/registration_id": registration_id}
        return self._text(self.registry.payload(c, skill_count), meta=meta)

    def _maybe_ready(self) -> None:
        if self.ready_announced or not all(st.registered for st in self.state.values()):
            return
        if self.record("hub_ready", handles=[c.handle for c in self.registry.clients]):
            self.ready_announced = True  # once per hub run (OC-4)
            self.say(*self.registry.ready_lines())

    async def list_skills(self) -> CallToolResult:
        await self.skills.rescan()
        return self._text(self.skills.listing_json())

    # ------------------------------------------------------------------ turn tools (Stage 1 design 4, 5.6)

    def _at(self) -> At:
        return At(utc_now(), self.mono())

    def _gate(self) -> Gate:
        """quorum()['waiting'] plus the invocation_hint of each client that is not registered or not present."""
        q = self.quorum()
        hints = tuple(
            c.invocation_hint for c in self.registry.clients
            if not self.state[c.id].registered or not self.present(self.state[c.id])
        )
        return Gate(tuple(q["waiting"]), hints)

    @staticmethod
    def _caller(ctx: Context) -> Caller:
        """Diagnostics of the calling session for turn_claimed; never identity (BP:342)."""
        try:
            info = ctx.session.client_params.client_info
        except Exception:
            info = None
        session = (ctx.headers or {}).get("mcp-session-id")
        return Caller(
            _era(ctx.protocol_version), session[:8] if session else None,
            {"name": info.name, "version": info.version} if info is not None else None,
        )

    def _read_quiet(self, path: str) -> bytes | None:
        """The engine's reader on the tool, end and replay paths: an unreadable file counts as missing, so an
        unreadable packet makes its turn broken rather than failing the call."""
        try:
            return read_file(path)
        except OSError as e:
            log.warning("cannot read %s: %s", path, e)
            return None

    def _caller_client(self, ctx: Context) -> Client:
        client_id = getattr(ctx.request_context.request.state, "rco_client", None)  # identity: the token only
        if client_id not in self.state:  # unreachable behind the auth layer
            raise RuntimeError("a turn tool was called without an authenticated client")
        return self.registry.get(client_id)

    def _changed(self) -> None:
        """Every turn-state change wakes the dormant waits, which then re-check synchronously (4.7)."""
        event, self._turn_changed = self._turn_changed, asyncio.Event()
        event.set()

    def _stage(self, plan: Plan) -> tuple[str, str] | None:
        """Step 2 of 5.6: the files the record will reference (the frozen packet); (path, error) on failure."""
        for path, text in plan.files:
            try:
                write_atomic(path, text)  # replaces a crash-orphan packet that no record names (7.2, 7.5)
            except OSError as e:
                log.error("cannot write %s: %s", path, e)
                return path, e.strerror or str(e)
        return None

    def _apply(self, plan: Plan) -> list[str] | None:
        """Steps 3-5 of 5.6 after _stage: the commit record (strict), apply, the records written after it
        (routing_decision), then the views. The console lines, or None when the commit record failed: nothing
        was applied."""
        rec = self.record_turn(plan.event, **plan.fields)
        if rec is None:
            return None
        self.book.commit(plan, rec, self.mono())
        self._changed()
        for event, fields in plan.after:
            self.record_turn(event, **fields)  # the turn stands if this fails; records-failing blocks the next action
        lines = list(plan.lines)
        for path, text in plan.views:
            try:
                write_atomic(path, text)
            except OSError as e:  # the record holds the full text (7.5)
                log.error("cannot save %s: %s", path, e)
                error, saved = f"[ERROR] Cannot save {path}: {e.strerror or e}", turns.saved_line(path)
                lines = [error if line == saved else line for line in lines] if saved in lines else lines + [error]
        return lines

    def _say_turn(self, client_id: str, lines: list[str]) -> None:
        """Tool-path console lines, info=False. An identical line for the same client within 10 s is recorded but
        not printed (4.3 (a)), which bounds retry spam."""
        now = self.mono()
        for line in lines:
            last = self._warned.get((client_id, line))
            if line.startswith("[WARN]") and last is not None and now - last < WARN_REPEAT_S:
                continue
            self._warned[(client_id, line)] = now
            self.say(line)

    def _tool_result(self, c: Client, res: Refusal | Plan, source: str) -> CallToolResult:
        if isinstance(res, Plan):
            failed = self._stage(res)
            lines = None if failed else self._apply(res)
            if lines is None:  # 4.3 note (b): decided at the commit point; not recordable
                return self._text(turns.no_record(source).text, is_error=True)
            if lines:
                self.say(*lines)
            return self._text(res.text)
        if res.reason == "packet_changed":
            log.warning("%s: the packet file of the live turn is missing or changed (turn broken)", source)
        if res.fields is not None:
            for path, text in res.files:  # the late copies, before the turn_refused record that names them
                try:
                    write_atomic(path, text, make_parent=True)
                except OSError as e:
                    log.error("cannot save %s: %s", path, e)
                    res.fields["late_file"] = None
                    self.say(f"[ERROR] Cannot save {path}: {e.strerror or e}")
            self.record_turn("turn_refused", **res.fields)  # a failed write still returns the refusal (4.3 (b))
        self._say_turn(c.id, res.lines)
        return self._text(res.text, is_error=True)

    def _internal(self, source: str) -> CallToolResult:
        """4.1: every handler catches its own exceptions, SchemaError included, so the outcome line stays."""
        log.exception("%s failed", source)
        r = turns.internal(source)
        self.say(*r.lines)
        return self._text(r.text, is_error=True)

    def _fetch(self, c: Client, wait_s: float | None, code: str | None, caller: Caller) -> Refusal | Plan:
        return self.book.check_fetch(
            c, wait_s, self._at(), self._gate(), self._read_quiet, code=code, code_mode=self.code_mode, caller=caller
        )

    async def get_turn(self, wait_s: float | None, code: str | None, ctx: Context) -> CallToolResult:
        try:
            c = self._caller_client(ctx)
            caller = self._caller(ctx)
            self.book.count_fetch(c.id, wait_s)  # fetches count every call by the target, refusals included (8.1)
            res = self._fetch(c, wait_s, code, caller)
            if isinstance(res, Refusal) and res.reason in turns.WAITABLE and self._wait_for(c, wait_s) > 0:
                res = await self._wait(c, wait_s, code, caller, ctx)
            return self._tool_result(c, res, "get_turn")
        except Exception:
            return self._internal("get_turn")

    @staticmethod
    def _wait_for(c: Client, wait_s: float | None) -> float:
        w = turns.effective_wait(wait_s, c.turn_wait_max_s)
        return w if math.isfinite(w) else 0.0

    async def _wait(
        self, c: Client, wait_s: float | None, code: str | None, caller: Caller, ctx: Context
    ) -> Refusal | Plan:
        """The dormant wait path (4.7): every Stage 1 cap is 0, so it only runs with a registry cap above 0. Waits
        on the turn-change event with a real timeout of at most 1 s per pass, reports progress every
        wait_progress_s, and re-checks synchronously after every wake; the deadline is on hub.mono."""
        limit = self._wait_for(c, wait_s)
        start = self.mono()
        deadline, next_progress = start + limit, start + self.cfg.wait_progress_s
        while (now := self.mono()) < deadline:
            if now >= next_progress:
                with contextlib.suppress(Exception):  # a client without a progress token gets nothing
                    await ctx.report_progress(round(now - start, 3), limit)
                next_progress = now + self.cfg.wait_progress_s
            changed = self._turn_changed
            timeout = max(min(MAX_WAIT_PASS_S, deadline - self.mono(), next_progress - self.mono()), 0.01)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), timeout)
            res = self._fetch(c, wait_s, code, caller)
            if not (isinstance(res, Refusal) and res.reason in turns.WAITABLE):
                self.book.count_wait(c.id, self.mono() - start)
                return self._fetch(c, wait_s, code, caller)  # re-checked with the wait counted; nothing in between
        waited = self.mono() - start
        self.book.count_wait(c.id, waited)
        return self.book.no_turn_after_wait(c, waited)

    async def claim_turn(self, turn_id: str, offer: str, ctx: Context) -> CallToolResult:
        try:
            c = self._caller_client(ctx)
            res = self.book.check_claim(
                c, turn_id, offer, self._at(), self._gate(), self._read_quiet, code_mode=self.code_mode,
                caller=self._caller(ctx),
            )
            return self._tool_result(c, res, "claim_turn")
        except Exception:
            return self._internal("claim_turn")

    async def submit_turn(
        self, turn_id: str, nonce: str, output: str, status: str | None, files_changed: str | None, ctx: Context
    ) -> CallToolResult:
        """Not gated by quorum, pause or registration: identity = token + nonce (4.5, 7.5)."""
        try:
            c = self._caller_client(ctx)
            res = self.book.check_submit(c, turn_id, nonce, output, self._at(), status=status, files_changed=files_changed)
            return self._tool_result(c, res, "submit_turn")
        except Exception:
            return self._internal("submit_turn")

    async def propose_run(self, task: str, ctx: Context) -> CallToolResult:
        try:
            c = self._caller_client(ctx)
            return self._tool_result(c, self.book.check_propose(c, task, self._at(), self._gate()), "propose_run")
        except Exception:
            return self._internal("propose_run")

    # ------------------------------------------------------------------ console commands

    def snapshot(self) -> dict[str, Any]:
        now = self.mono()
        clients = []
        for c in self.registry.clients:
            st = self.state[c.id]
            clients.append({
                "id": c.id, "display_name": c.display_name, "handle": c.handle, "registered": st.registered,
                "registered_at": time.strftime("%H:%M:%S", time.localtime(st.registered_at)) if st.registered_at else None,
                "register_count": st.register_count, "transport_seen": st.transport_seen,
                "open_requests": st.open_requests,
                "last_seen_age_s": None if st.last_seen_mono is None else now - st.last_seen_mono,
                "era": st.era, "client_info": st.client_info, "stale": st.stale and st.registered,
                "missing": st.missing_reported and not st.registered,
            })
        run, live, proposal = self.book.run, self.book.live_turn, self.book.proposal
        return {
            "paused": self.paused, "uptime_s": now - self.started_mono, "url": self.url,
            "data_dir": str(self.cfg.data_dir), "records_healthy": self.records_healthy,
            "skills": self.skills.listing(), "clients": clients, "quorum": self.quorum(),
            # Stage 1 (5.8): the run state the dispatcher needs, and the status block ([] = the M1 output)
            "run": None if run is None else {"id": run.id, "pause_reasons": list(run.pause_reasons)},
            "turn": None if live is None else {"id": live.id, "state": live.state, "client": live.client},
            "proposal": None if proposal is None else proposal.id,
            "turn_status": self.book.status_lines(self._at(), self.code_mode),
            "turn_commands": [(c.turn_command, c.turn_hint) for c in self.registry.clients],
        }

    async def command(self, line: str) -> None:
        res = con.dispatch(self.snapshot(), line, self.extra_commands)
        if res is None:
            return
        if res.turn is not None and res.turn["verb"] in con.TURN_VERBS:
            self._turn_command(res)
            return
        refused = f"[ERROR] '{res.command}' not applied: the record file cannot be written."
        recorded = self.record("operator_command", command=res.command, args=res.args, result=res.result)
        if not recorded and any(e != "stop" for e in res.effects):  # stop always works (human control)
            self.console.line(refused)
            return
        for effect in res.effects:
            if effect == "pause":
                fields: dict[str, Any] = {"by": "operator"}  # with no run open, exactly as at M1
                reason = (res.turn or {}).get("reason")
                if self.book.run is not None:
                    fields.update(run_id=self.book.run.id, reason=reason)
                elif reason:
                    fields["reason"] = reason
                if not self.record("hub_paused", **fields):
                    self.console.line(refused)
                    return
                self.paused = True
            elif effect == "resume":
                # 5.3: one hub_resumed clears the hub flag and every run pause reason. A run reason is run state
                # (replay folds hub_resumed with run_id), so that record is strict.
                plan = self.book.check_resume()
                if plan is None:
                    ok = self.record("hub_resumed", by="operator")
                else:
                    rec = self.record_turn("hub_resumed", **plan.fields)
                    ok = rec is not None
                if not ok:
                    self.console.line(refused)
                    return
                self.paused = False
                self._resume_mono = self.mono()
                if plan is not None:
                    self.book.commit(plan, rec, self.mono())
                    self._changed()
                    res.text = res.text + plan.lines
            elif effect.startswith("release:"):
                st = self.state[effect.split(":", 1)[1]]
                st.registered, st.register_count, st.stale = False, 0, False
            elif effect == "panel" and self.panel is not None:
                res.text = res.text + [self.panel.info_line()]
                self.panel.open_browser()
        if res.text:
            self.console.lines(res.text)
        if "stop" in res.effects:
            self.request_stop("operator")

    def _run_folder_names(self) -> list[str]:
        try:
            return os.listdir(self.runs_dir)
        except FileNotFoundError:
            return []
        except OSError as e:
            log.warning("cannot list %s: %s", self.runs_dir, e)
            return []

    def _turn_check(self, turn: dict[str, Any], at: At) -> tuple[Refusal | Plan, dict[str, Any] | None]:
        """The engine's pure check of a turn verb, plus the turn_refused fields of a file failure met while checking."""
        book, verb = self.book, turn["verb"]
        if verb == "run":
            names = self._run_folder_names()
            if "file" not in turn:
                return book.check_run(turn["task"], at, folder_names=names), None
            path = os.path.abspath(turn["file"])  # relative to the hub's working directory (the RCO folder)
            try:
                data, error = read_file(path), None
            except OSError as e:
                data, error = None, e.strerror or str(e)
            task = turns.task_from_file(path, data, error)
            if isinstance(task, Refusal):
                return task, None
            return book.check_run(task, at, task_source="file", task_file=path, folder_names=names), None
        if verb == "next":
            # A role '<skill>' or '<skill>:<agent>' naming a loaded skill brings that skill, and the agent's part of
            # it, into the packet, as the adapter renders it.
            skill_name, _, agent = (turn["role"] or "").partition(":")
            found = self.skills.find(skill_name) if skill_name else None
            skill = None if found is None else (found.name, found.sha256, self.skills.brief(found, agent or None))
            try:
                return book.check_next(
                    turn["client"], turn["role"], turn["picks"], turn["note"], at, self._gate(), read_file,
                    code_mode=self.code_mode, skill=skill,
                ), None
            except OSError as e:
                path, error = str(e.filename or "?"), e.strerror or str(e)
                run_id = book.run.id if book.run else None
                return turns.io_failure(path, error), turns.console_failure("io", f"{path}: {error}", run_id)
        if verb == "recall":
            return book.check_recall(turn["ref"], at), None
        if verb == "stopped":
            return book.check_stopped(at), None
        if verb == "end":
            return book.check_end(turn["outcome"], at, self._read_quiet), None
        return book.check_note(turn["rest"], at), None

    def _turn_command(self, res: con.Result) -> None:
        """A turn verb (5.8, 8.2): the engine checks first (pure), then operator_command records the true result,
        then the turn events. A failure after the check (file I/O, strict record) is also recorded, as
        turn_refused{source: console}. Replies to a typed command always print."""
        assert res.turn is not None
        check, failure = self._turn_check(res.turn, self._at())
        if isinstance(check, Plan):  # the folder and the staged files decide the true result too (5.8 catalogue)
            check, failure = self._prepare(check)
        recorded = self.record("operator_command", command=res.command, args=res.args, result=check.result)
        if isinstance(check, Refusal):
            if failure is not None:
                self.record_turn("turn_refused", **failure)
            self.console.lines(check.lines)
            return
        refused = f"[ERROR] '{res.command}' not applied: the record file cannot be written."
        if not recorded:
            if check.mkdir is not None:  # nothing records this run: leave no empty folder behind
                with contextlib.suppress(OSError):
                    os.rmdir(check.mkdir)
            self.console.line(refused)
            return
        run_id = check.fields.get("run_id")
        lines = self._apply(check)
        if lines is None:
            failure = turns.console_failure(
                "no_record", "the record file cannot be written", run_id, check.fields.get("turn_id")
            )
            self.record_turn("turn_refused", **failure)
            self.console.line(refused)
            return
        if lines:
            self.console.lines(lines)

    def _prepare(self, plan: Plan) -> tuple[Refusal | Plan, dict[str, Any] | None]:
        """Step 2 of 5.6 for a console plan, before operator_command, so that command records its true result (8.2):
        the exclusive create of a run folder (7.2) and the staged files. A failure is the catalogue refusal
        (refused:run_folder_exists or refused:io) plus its turn_refused{source: console} fields."""
        run_id = plan.fields.get("run_id")
        if plan.mkdir is not None:  # a run folder is created exclusively, never reused (7.2)
            try:
                os.makedirs(self.runs_dir, exist_ok=True)
                os.mkdir(plan.mkdir)
            except FileExistsError:
                return turns.run_folder_exists(plan.mkdir), turns.console_failure("run_folder_exists", plan.mkdir, run_id)
            except OSError as e:
                error = e.strerror or str(e)
                return turns.io_failure(plan.mkdir, error), turns.console_failure("io", f"{plan.mkdir}: {error}", run_id)
        failed = self._stage(plan)
        if failed is not None:
            path, error = failed
            return turns.io_failure(path, error), turns.console_failure("io", f"{path}: {error}", run_id)
        return plan, None

    def _on_line(self, line: str) -> None:  # key thread
        with contextlib.suppress(RuntimeError):  # the loop may be closing
            if self.loop is not None and not self.loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(self.command(line), self.loop)
                future.add_done_callback(lambda f: self._command_done(f, line))

    def _command_done(self, future: Any, line: str) -> None:
        """Stage 1 design 7.4 rule 2: an error inside a command (a SchemaError included) would otherwise vanish in
        the discarded future."""
        if future.cancelled() or future.exception() is None:
            return
        log.error("console command %r failed", line, exc_info=future.exception())
        word = line.split()[0].lower() if line.split() else ""
        self.console.line(f"[ERROR] '{word}' failed: internal error (details in the diag log).")

    def stop_threadsafe(self, reason: str = "operator") -> None:
        if self.loop is None:  # e.g. Ctrl+C during startup: _serve applies it once the loop runs (no I/O here)
            self._pending_stop = self._pending_stop or reason
            return
        with contextlib.suppress(RuntimeError):  # the loop may be closing
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.request_stop, reason)

    def request_stop(self, reason: str) -> None:
        """The shutdown routine: record, then let uvicorn close; streams get at most 3 s."""
        if self._stopping:
            return
        self._stopping = True
        self.flush_auth(final=True)
        self.record("server_stop", reason=reason, uptime_s=round(self.mono() - self.started_mono, 1))
        self._stop_recorded.set()
        AppStatus.should_exit = True  # legacy GET streams (sse_starlette) learn of shutdown only through this
        if self.server is not None:
            self.server.should_exit = True
        if self.panel_server is not None:
            self.panel_server.should_exit = True

    # ------------------------------------------------------------------ run

    def _signal(self, signum: int, frame: Any) -> None:
        if self._stopping and self.server is not None:
            self.server.force_exit = True  # a second Ctrl+C skips the 3 s grace
        self.stop_threadsafe("signal")

    def _console_ctrl(self, event: int) -> bool:
        """Native console control handler (runs on a system thread). Closing the window, logoff and shutdown end
        the process as soon as the handlers return, before a Python signal handler could run, so server_stop is
        recorded here first (waiting at most 4 s, inside Windows' 5 s). Ctrl+C and Ctrl+Break return False and
        reach the SIGINT/SIGBREAK handlers."""
        if event not in CLOSE_EVENTS:
            return False
        self.stop_threadsafe("signal")
        self._stop_recorded.wait(4.0)
        return True

    def _install_close_handler(self) -> None:
        """Registered after the CRT's handler (installed by signal.signal), so Windows calls it first."""
        if os.name != "nt":
            return
        from ctypes import wintypes

        handler = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(lambda event: bool(self._console_ctrl(event)))
        with contextlib.suppress(OSError, AttributeError):
            if ctypes.WinDLL("kernel32").SetConsoleCtrlHandler(handler, True):
                self._ctrl_ref = handler

    def _remove_close_handler(self) -> None:
        if self._ctrl_ref is not None:  # the reference itself is kept: a call may still be in flight
            with contextlib.suppress(OSError, AttributeError):
                ctypes.WinDLL("kernel32").SetConsoleCtrlHandler(self._ctrl_ref, False)

    def run(self) -> int:
        handlers: dict[int, Any] = {}
        code = 0
        try:
            if self.world.signals and threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
                    if sig is not None:
                        handlers[sig] = signal.signal(sig, self._signal)
                self._install_close_handler()
            try:
                asyncio.run(self._serve())
                if self.server is not None and not self.server.started and not self._stopping:
                    raise RuntimeError("the server did not start")
            except KeyboardInterrupt:
                if not self._stopping:
                    self.request_stop("signal")
            except (Exception, SystemExit) as e:  # an internal error; uvicorn exits(3) when lifespan startup fails
                log.exception("hub crashed")
                if not self._stopping:
                    self.request_stop("error")
                detail = "the server did not start" if isinstance(e, SystemExit) else str(e)
                self.console.close_prompt()
                self.console.line(f"[ERROR] The hub stopped on an internal error: {detail} (details in the diag log).")
                code = 1
            finally:
                self._remove_close_handler()
                for sig, h in handlers.items():
                    signal.signal(sig, h)
            self.console.close_prompt()
            self.console.lines(
                ["[INFO] MCP server stopped."]
                + [f"[INFO] Reconnect after a restart: {c.reconnect_hint}" for c in self.registry.clients]
            )
        finally:
            self._release_resources()
        return code

    async def _serve(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._register_lock = asyncio.Lock()
        AppStatus.should_exit = False  # class-level flag: reset for this run
        names = sorted(t.name for t in await self.mcp.list_tools())
        if tuple(names) != TOOLS:
            raise RuntimeError(f"tool surface changed: {names}")
        await self.skills.rescan(deadline=False)  # initial scan; its console notes are held back
        config = uvicorn.Config(
            self.app, host=HOST, port=self.port, lifespan="on", log_config=None, access_log=False,
            proxy_headers=False, server_header=False, timeout_graceful_shutdown=3,
        )
        self.server = _Server(config)
        if self.panel is not None:
            self.panel_server = _Server(uvicorn.Config(
                self.panel.app, host=HOST, port=self.cfg.panel_port, lifespan="off", log_config=None,
                access_log=False, proxy_headers=False, server_header=False, timeout_graceful_shutdown=1,
            ))
        if self._pending_stop is not None and not self._stopping:
            self.request_stop(self._pending_stop)
        if self._stopping:
            self.server.should_exit = True
            if self.panel_server is not None:
                self.panel_server.should_exit = True
        tasks = [asyncio.create_task(t) for t in (self._announce(), self._monitor(), self._poll_skills())]
        panel_task = None
        if self.panel_server is not None and self.panel_sock is not None:
            panel_task = asyncio.create_task(self.panel_server.serve(sockets=[self.panel_sock]))
        try:
            await self.server.serve(sockets=[self.sock])
        finally:
            if panel_task is not None:
                assert self.panel_server is not None
                self.panel_server.should_exit = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(panel_task, timeout=3)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _announce(self) -> None:
        assert self.server is not None
        while not self.server.started:  # set once uvicorn serves the pre-bound socket
            if self.server.should_exit:
                return
            await asyncio.sleep(0.02)
        held, self._held = self._held or [], None
        panel = [self.panel.info_line()] if self.panel is not None else []
        self.console.lines(self.registry.startup_lines() + panel + held)
        self.console.show_prompt()
        self.started.set()
        if self.panel is not None:
            self.panel.open_browser()
        source = (self.world.keys, None) if self.world.keys is not None else con.default_keys()
        if source is not None:
            keys, special_pending = source
            con.start_key_thread(
                self.console, keys, self._on_line, lambda: self.stop_threadsafe("signal"), special_pending
            )

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                self.check_presence()
                self.flush_auth()
                self.check_reminders()
            except Exception:
                log.exception("monitor tick failed")

    def check_reminders(self) -> None:
        """Stage 1 design 5.7: transition-only lines, once per turn, on hub.mono. Ages count from the latest of the
        event, this hub start and the last resume; never while the run is paused or the hub flag is set."""
        floor = max(self.started_mono, self._resume_mono if self._resume_mono is not None else self.started_mono)
        lines = self.book.reminders(
            self._at(), self.cfg.offer_reminder_s, self.cfg.claim_reminder_s, self.paused, floor
        )
        if lines:
            self.say(*lines)

    async def _poll_skills(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.poll_interval_s)
            await self.skills.rescan()

    def _release_resources(self) -> None:
        """Idempotent; runs on every exit path of run()."""
        if hasattr(self, "records"):
            with contextlib.suppress(OSError):
                self.records.close()
        with contextlib.suppress(Exception):
            logging.getLogger().removeHandler(self._diag)
            self._diag.close()
        with contextlib.suppress(OSError):
            self.sock.close()
        if self.panel_sock is not None:
            with contextlib.suppress(OSError):
                self.panel_sock.close()


def _record_start_failure(cfg: Config, cause: str, port: int) -> None:
    try:
        records = Records(cfg.data_dir / "records" / "rco-records.jsonl", str(uuid.uuid4()))
        try:
            records.write("server_start_failed", reason=cause, port=port, config_key="hub.port")
        finally:
            records.close()
    except OSError:
        pass


def run(argv: list[str] | None = None, world: World | None = None, default_config: Path | None = None) -> int:
    world = world or World()
    console = con.open_console(world.out or sys.stdout, world.env)
    try:
        hub = Hub.create(argv, world, default_config, console)
    except StartupFailure as e:
        console.lines(str(e).split("\n"))
        return 2
    return hub.run()


def main(argv: list[str] | None = None, default_config: Path | None = None) -> None:
    code = run(argv, default_config=default_config)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    # os._exit: a daemon thread blocked in stdin.readline() (piped stdin) holds the stdin lock, which would
    # make interpreter shutdown fail. Records and the diag log are already closed at this point.
    os._exit(code)
