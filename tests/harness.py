"""Test harness.

HubHarness runs the production assembly (rco.hub.Hub.create + Hub.run) in a background thread on a real
127.0.0.1 socket that the test pre-binds to port 0 with SO_EXCLUSIVEADDRUSE and injects through World.sock
(the test-only path; production refuses port 0). The console sink and key source are injected, tokens come
from an injected environment, HKCU is never read, and every path lives under pytest's tmp_path.
HubProcess runs mcp_server.py as a real subprocess (stdin/stdout/stderr piped).
Clients speak real HTTP: the mcp 2.2.0 SDK Client over streamable HTTP (mode "2026-07-28" = Claude-like,
mode "legacy" = Codex-like SDK client) and raw httpx2 requests for Codex-like 2025-06-18 sessions and GET streams.
"""

import asyncio
import contextlib
import io
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Implementation

from rco import console as con
from rco.hub import Hub, World

ROOT = Path(__file__).resolve().parent.parent
REAL_REGISTRY = ROOT / "registry.toml"
LEGACY_PV = "2025-06-18"
MODERN_PV = "2026-07-28"
TOKEN_C = "test-token-c-" + "0123456789abcdef" * 2  # 45 chars, test-only
TOKEN_O = "test-token-o-" + "fedcba9876543210" * 2
TOKENS_ENV = {"RCO_TOKEN_C": TOKEN_C, "RCO_TOKEN_O": TOKEN_O}

# Exact strings from the build prompt, section 4 (the tests assert these literals, not the registry file).
STARTUP = ["[INFO] MCP server started (local)", "[INFO] Waiting for model connections..."]
CONNECTED_C = "[INFO] Claude connected (middleware-c)"
CONNECTED_O = "[INFO] OpenAI/Codex connected (middleware-o)"
READY = ["", "Ready to route skills and manage sessions."]
CHECKS = (
    "✓ Connected to Python MCP server (local)\n"
    "✓ Available skills: 0 (will load dynamically)\n"
    "✓ Ready to receive tasks.\n"
)
PAYLOAD_C = "MCP server for Claude initialized.\n" + CHECKS + "\nYou can now start using skills and routing requests."
PAYLOAD_O = (
    "MCP server for OpenAI (Codex) initialized.\n"
    + CHECKS
    + "\nYou can now start using skills and work with Claude through the middleware."
)
STOP_LINES = [
    "[INFO] MCP server stopped.",
    "[INFO] Reconnect after a restart: Claude: /mcp -> Reconnect, or a new session",
    "[INFO] Reconnect after a restart: Codex: open a new thread",
]
# The quorum line that ends `status` output, exactly as design section 7 gives it.
QUORUM = re.compile(r"^(READY \d+/\d+|WAITING: .+ Nothing proceeds single-model\.)$")


def quorum_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if QUORUM.match(line)]


VALID_SKILL = """\
# PROVISIONAL RCO placeholder skill format, Gate 1. NOT the real RCO skill format.
format = "rco-provisional-0"
name = "{name}"
description = "Find the weakest assumption in the current work and argue against it."
when_it_fits = "A draft or plan exists and nobody has challenged it yet."
inputs = ["the work to challenge"]
outputs = ["the assumption", "the argument", "what was checked"]
good_result = "Names one concrete assumption, gives evidence, says what would change the conclusion."
model_preference = "either"
acts_on_world = false
may_touch = []
"""


def skill_text(name: str = "challenge-assumption") -> str:
    return VALID_SKILL.replace("{name}", name)


# ------------------------------------------------------------------------------------------ screen


def screen(raw: str) -> list[str]:
    """What a terminal shows for `raw`: \\r returns to column 0, \\b steps back, ESC[2K clears the line."""
    lines: list[str] = []
    cur: list[str] = []
    col = 0
    i = 0
    while i < len(raw):
        if raw.startswith("\x1b[", i):
            j = i + 2
            while j < len(raw) and not raw[j].isalpha():
                j += 1
            if raw[j : j + 1] == "K":
                cur = []
            i = j + 1
            continue
        ch = raw[i]
        if ch == "\n":
            lines.append("".join(cur).rstrip())
            cur, col = [], 0
        elif ch == "\r":
            col = 0
        elif ch == "\b":
            col = max(col - 1, 0)
        else:
            if col < len(cur):
                cur[col] = ch
            else:
                cur.append(ch)
            col += 1
        i += 1
    lines.append("".join(cur).rstrip())
    return lines


class Sink(io.TextIOBase):
    """Thread-safe capture of everything the console writes (not a TTY, so no colour)."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        with self._lock:
            self._parts.append(s)
        return len(s)

    def isatty(self) -> bool:
        return False

    def text(self) -> str:
        with self._lock:
            return "".join(self._parts)


class FakeClock:
    """Monotonic clock with an offset, to simulate a slow manual run without waiting for it."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


# ------------------------------------------------------------------------------------------ config


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return repr(v)


def write_config(
    base: Path,
    *,
    registry: Path | str = REAL_REGISTRY,
    data_dir: Path | str | None = None,
    port: int = 8799,
    sections: dict[str, dict[str, Any]] | None = None,
    name: str = "rco.toml",
    bom: bool = False,
) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    doc: dict[str, dict[str, Any]] = {
        "hub": {"port": port, "registry": str(registry)},
        "paths": {"data_dir": str(data_dir if data_dir is not None else base / "data"), "skills_dir": str(base / "skills")},
        "skills": {"adapter": "rco_skills.provisional", "settle_ms": 0, "poll_interval_s": 0.2},
    }
    for section, table in (sections or {}).items():
        doc.setdefault(section, {}).update(table)
    text = "".join(
        f"[{section}]\n" + "".join(f"{k} = {toml_value(v)}\n" for k, v in table.items()) for section, table in doc.items()
    )
    path = base / name
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return path


def prebind() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind(("127.0.0.1", 0))
    return s


def free_port() -> int:
    with prebind() as s:
        return s.getsockname()[1]


def read_records(data_dir: Path, event: str | None = None) -> list[dict[str, Any]]:
    path = data_dir / "records" / "rco-records.jsonl"
    if not path.exists():
        return []
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in recs if event is None or r["event"] == event]


def make_world(base: Path, **kw: Any) -> World:
    defaults: dict[str, Any] = dict(
        # USERPROFILE too: the default [paths] data_dir is %USERPROFILE%\.rco\data (OC-1 revised), so a config
        # without data_dir can never reach the real profile.
        env={**TOKENS_ENV, "LOCALAPPDATA": str(base / "lad"), "APPDATA": str(base / "ad"), "USERPROFILE": str(base / "up")},
        read_user_env=lambda name: None,
        package_identity=lambda: 15700,
        listeners=lambda port: [],
        signals=False,
    )
    defaults.update(kw)
    return World(**defaults)


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout} s waiting for {what}")
        time.sleep(0.05)


# ------------------------------------------------------------------------------------------ hub in-process


class HubHarness:
    def __init__(
        self,
        base: Path,
        *,
        registry: Path = REAL_REGISTRY,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] | None = None,
        package_identity: int = 15700,
        config: dict[str, dict[str, Any]] | None = None,
        data_dir: Path | str | None = None,
        realpath: Callable[[str], str] | None = None,
        panel: bool = False,
    ) -> None:
        self.base = base
        self.skills_dir = base / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.opened: list[str] = []  # the addresses the hub asked the browser to open (the operator panel)
        panel_sock = prebind() if panel else None  # the test-only pre-bound path, like the hub socket
        if panel_sock is not None:
            config = {**(config or {}), "panel": {"enabled": True, "ui_port": panel_sock.getsockname()[1]}}
        self.config_path = write_config(base, registry=registry, data_dir=data_dir, sections=config)
        self.sink = Sink()
        self.keys: queue.Queue[str] = queue.Queue()
        self.console = con.Console(self.sink, width=80)
        world_kw: dict[str, Any] = dict(
            out=self.sink, keys=self.keys.get, package_identity=lambda: package_identity, sock=prebind(),
            panel_sock=panel_sock, browser=self.opened.append,
        )
        if env is not None:
            world_kw["env"] = env
        if clock is not None:
            world_kw["monotonic"] = clock
        if realpath is not None:
            world_kw["realpath"] = realpath
        self.world = make_world(base, **world_kw)
        try:
            self.hub = Hub.create(["--config", str(self.config_path)], self.world, console=self.console)
        except BaseException:
            self.world.sock.close()
            if self.world.panel_sock is not None:
                self.world.panel_sock.close()
            raise
        self.data_dir = self.hub.cfg.data_dir
        self.url, self.port = self.hub.url, self.hub.port
        self.panel_url = self.hub.panel.url if self.hub.panel is not None else None
        self.code: int | None = None
        self.thread = threading.Thread(target=self._run, name="hub", daemon=True)
        self.thread.start()
        if not self.hub.started.wait(15):
            self.close()
            raise RuntimeError("the hub did not start")

    def _run(self) -> None:
        self.code = self.hub.run()

    # console
    def type(self, text: str) -> None:
        for ch in text:
            self.keys.put(ch)

    def command(self, line: str) -> None:
        self.type(line + "\r")

    def screen(self) -> list[str]:
        return screen(self.sink.text())

    def wait_screen(self, needle: str, timeout: float = 5.0, count: int = 1) -> list[str]:
        wait_until(lambda: sum(needle in line for line in self.screen()) >= count, timeout, repr(needle))
        return self.screen()

    async def await_screen(self, needle: str, timeout: float = 5.0, count: int = 1) -> list[str]:
        return await asyncio.to_thread(self.wait_screen, needle, timeout, count)

    def wait_quorum(self, count: int = 1, timeout: float = 5.0) -> list[str]:
        """Wait for the quorum line that ends `status` output."""
        wait_until(lambda: len(quorum_lines(self.screen())) >= count, timeout, "the status quorum line")
        return self.screen()

    async def await_quorum(self, count: int = 1, timeout: float = 5.0) -> list[str]:
        return await asyncio.to_thread(self.wait_quorum, count, timeout)

    def records(self, event: str | None = None) -> list[dict[str, Any]]:
        return read_records(self.data_dir, event)

    def diag(self) -> str:
        return (self.data_dir / "logs" / "hub-diag.log").read_text(encoding="utf-8")

    def stop(self, timeout: float = 15.0) -> int | None:
        if self.thread.is_alive():
            self.hub.stop_threadsafe("operator")
            self.thread.join(timeout)
        return self.code

    def close(self) -> None:
        self.stop()
        self.keys.put("")  # end the key thread
        assert not self.thread.is_alive(), "hub thread did not finish"


# ------------------------------------------------------------------------------------------ hub subprocess


class HubProcess:
    def __init__(self, base: Path, *, sections: dict[str, dict[str, Any]] | None = None) -> None:
        (base / "skills").mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.data_dir = base / "data"
        self.config_path = write_config(base, port=self.port, sections=sections)
        env = {k: v for k, v in os.environ.items() if k.upper() not in TOKENS_ENV}
        env.update(TOKENS_ENV)
        env["USERPROFILE"] = str(base / "up")  # the default data_dir (%USERPROFILE%\.rco\data) stays in tmp
        env.pop("NO_COLOR", None)
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp_server.py"), "--config", str(self.config_path)],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self._buf = {"out": bytearray(), "err": bytearray()}
        self._lock = threading.Lock()
        self._readers = [
            threading.Thread(target=self._pump, args=(self.proc.stdout, "out"), daemon=True),
            threading.Thread(target=self._pump, args=(self.proc.stderr, "err"), daemon=True),
        ]
        for t in self._readers:
            t.start()

    def _pump(self, stream: Any, key: str) -> None:
        while True:
            chunk = stream.read1(4096)
            if not chunk:
                return
            with self._lock:
                self._buf[key].extend(chunk)

    def stdout(self) -> str:
        with self._lock:
            return bytes(self._buf["out"]).decode("utf-8", "replace")

    def stderr(self) -> str:
        with self._lock:
            return bytes(self._buf["err"]).decode("utf-8", "replace")

    def screen(self) -> list[str]:
        return screen(self.stdout())

    def wait_for(self, needle: str, timeout: float = 30.0) -> None:
        wait_until(lambda: needle in self.stdout() or self.proc.poll() is not None, timeout, repr(needle))
        assert needle in self.stdout(), f"hub exited early: {self.stdout()!r} {self.stderr()!r}"

    def send(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def records(self, event: str | None = None) -> list[dict[str, Any]]:
        return read_records(self.data_dir, event)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(15)
        for t in self._readers:
            t.join(5)
        for s in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            with contextlib.suppress(Exception):
                s.close()


# ------------------------------------------------------------------------------------------ clients


def arun(coro: Any, timeout: float = 60.0) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(bounded())


def text(result: Any) -> str:
    assert len(result.content) == 1, result
    return result.content[0].text


@contextlib.asynccontextmanager
async def sdk_client(url: str, token: str, mode: str, info: str) -> AsyncIterator[Client]:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=300.0)) as http:
        async with Client(
            streamable_http_client(url, http_client=http), mode=mode, client_info=Implementation(name=info, version="test")
        ) as client:
            yield client


def claude_like(target: Any, token: str = TOKEN_C, info: str = "claude-code") -> Any:
    """Claude Code's shape: protocol 2026-07-28, clientInfo claude-code (listen stream via .listen())."""
    return sdk_client(target.url, token, MODERN_PV, info)


def codex_like(target: Any, token: str = TOKEN_O, info: str = "codex-mcp-client") -> Any:
    """Codex's shape: legacy initialize (2025-06-18 negotiated), clientInfo codex-mcp-client, GET stream."""
    return sdk_client(target.url, token, "legacy", info)


def rpc_messages(response: httpx2.Response) -> list[dict[str, Any]]:
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        body = response.json()
        return body if isinstance(body, list) else [body]
    return [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]


class Raw:
    """Raw httpx2 client for Codex-like 2025-06-18 sessions and for malformed or hostile requests."""

    def __init__(self, url: str, token: str | None) -> None:
        self.url, self.token = url, token
        self.http = httpx2.AsyncClient(timeout=httpx2.Timeout(10.0, read=60.0))
        self._id = 0

    async def __aenter__(self) -> "Raw":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.http.aclose()

    def headers(self, sid: str | None = None, pv: str | None = LEGACY_PV, **extra: str) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.token is not None:
            h["Authorization"] = f"Bearer {self.token}"
        if pv:
            h["MCP-Protocol-Version"] = pv
        if sid:
            h["Mcp-Session-Id"] = sid
        h.update(extra)
        return h

    async def post(
        self, body: dict[str, Any], sid: str | None = None, pv: str | None = LEGACY_PV, url: str | None = None, **extra: str
    ) -> httpx2.Response:
        return await self.http.post(url or self.url, json=body, headers=self.headers(sid, pv, **extra))

    def next_id(self) -> int:
        self._id += 1
        return self._id

    async def initialize(self, name: str = "codex-mcp-client") -> str:
        params = {"protocolVersion": LEGACY_PV, "capabilities": {}, "clientInfo": {"name": name, "version": "test"}}
        r = await self.post({"jsonrpc": "2.0", "id": self.next_id(), "method": "initialize", "params": params}, pv=None)
        assert r.status_code == 200, (r.status_code, r.text)
        sid = r.headers["mcp-session-id"]
        r2 = await self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid=sid)
        assert r2.status_code == 202, (r2.status_code, r2.text)
        return sid

    async def initialize_status(self) -> int:
        params = {"protocolVersion": LEGACY_PV, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
        r = await self.post({"jsonrpc": "2.0", "id": self.next_id(), "method": "initialize", "params": params}, pv=None)
        return r.status_code

    async def request(self, sid: str, method: str, params: dict[str, Any] | None = None) -> httpx2.Response:
        body = {"jsonrpc": "2.0", "id": self.next_id(), "method": method, "params": params or {}}
        return await self.post(body, sid=sid)

    async def call_tool(self, sid: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        r = await self.request(sid, "tools/call", {"name": name, "arguments": args})
        assert r.status_code == 200, (r.status_code, r.text)
        return next(m for m in rpc_messages(r) if "result" in m or "error" in m)

    @contextlib.asynccontextmanager
    async def get_stream(self, sid: str) -> AsyncIterator[httpx2.Response]:
        headers = {"Accept": "text/event-stream", "MCP-Protocol-Version": LEGACY_PV, "Mcp-Session-Id": sid}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        async with self.http.stream("GET", self.url, headers=headers) as r:
            assert r.status_code == 200, r.status_code
            yield r


def raw_http(port: int, request: bytes) -> tuple[int, bytes]:
    """Send raw bytes (e.g. a non-ASCII header) and return (status, full response)."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(request)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1]), data
