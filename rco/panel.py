"""Operator panel: a local web page with buttons over the hub's console commands.

Every button builds the same console line the operator could type (design 5.8) and runs it through
Hub.command, so the records, the engine checks and the console lines are exactly those of the typed
command; the page only shows state and collects input. It listens on 127.0.0.1 on its own port. Every
request carries the panel token, minted at start and kept in memory only, so a model session on this
machine cannot reach the operator's decisions through localhost. The console itself keeps working; the
panel mirrors its lines.
"""

import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from rco import turns
from rco.config import HOST

if TYPE_CHECKING:
    from rco.hub import Hub

log = logging.getLogger("rco.panel")
PAGE = Path(__file__).with_name("panel.html")
TOKEN_HEADER = "x-rco-panel"
ROLES = ("propose", "challenge", "revise", "review", "verify", "summarise")


class Panel:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.token = secrets.token_urlsafe(24)
        self.port = hub.cfg.panel_port
        self.url = f"http://{HOST}:{self.port}/?t={self.token}"
        self.app = Starlette(routes=[
            Route("/", self.page),
            Route("/api/state", self.state),
            Route("/api/command", self.command, methods=["POST"]),
            Route("/api/run", self.open_run, methods=["POST"]),
            Route("/api/text/{kind}/{turn_id}", self.text),
            Route("/api/output/{turn_id}", self.save_output, methods=["PUT"]),
        ])

    def info_line(self) -> str:
        return f"[INFO] Operator panel: {self.url}"

    def open_browser(self) -> None:
        if self.hub.cfg.panel_open_browser:
            self.hub.world.browser(self.url)

    # ------------------------------------------------------------------ auth

    def _authorised(self, request: Request) -> bool:
        given = request.headers.get(TOKEN_HEADER) or request.query_params.get("t") or ""
        return hmac.compare_digest(given.encode("utf-8", "replace"), self.token.encode("utf-8"))

    @staticmethod
    def _denied() -> Response:
        return JSONResponse({"error": "panel token missing or wrong; type 'panel' at the hub console"}, status_code=403)

    # ------------------------------------------------------------------ routes

    async def page(self, request: Request) -> Response:
        if not self._authorised(request):
            return HTMLResponse(
                "<!doctype html><title>RCO panel</title><body style='font-family:system-ui;padding:2rem'>"
                "<h2>RCO operator panel</h2><p>Open it from the hub: type <code>panel</code> at the hub console "
                "and use the address it prints.</p></body>", status_code=403,
            )
        try:
            html = PAGE.read_text(encoding="utf-8")
        except OSError as e:
            return PlainTextResponse(f"panel page missing: {e}", status_code=500)
        return HTMLResponse(html)

    async def state(self, request: Request) -> Response:
        if not self._authorised(request):
            return self._denied()
        try:
            since = int(request.query_params.get("since", "0"))
        except ValueError:
            since = 0
        return JSONResponse(self.build_state(since))

    async def command(self, request: Request) -> Response:
        if not self._authorised(request):
            return self._denied()
        body = await self._json(request)
        line = " ".join(str(body.get("line", "")).split("\n")).strip()
        if not line:
            return JSONResponse({"lines": []})
        return JSONResponse({"lines": await self._run_line(line)})

    async def open_run(self, request: Request) -> Response:
        """run <task>; a multi-line task goes through a file (run @<path>), as the console grammar allows."""
        if not self._authorised(request):
            return self._denied()
        body = await self._json(request)
        task = str(body.get("task", "")).replace("\r\n", "\n").strip()
        if not task:
            return JSONResponse({"lines": ["[ERROR] The task is empty."]})
        if "\n" not in task:
            return JSONResponse({"lines": await self._run_line(f"run {task}")})
        path = os.path.join(str(self.hub.cfg.data_dir), "panel", "task.md")
        try:
            from rco.hub import write_atomic

            write_atomic(path, task + "\n", make_parent=True)
        except OSError as e:
            return JSONResponse({"lines": [f"[ERROR] Cannot write the task file {path}: {e}"]})
        return JSONResponse({"lines": await self._run_line(f"run @{path}")})

    async def text(self, request: Request) -> Response:
        if not self._authorised(request):
            return self._denied()
        kind, turn_id = request.path_params["kind"], request.path_params["turn_id"]
        t = self.hub.book.find(turn_id)
        if t is None or kind not in ("output", "packet"):
            return PlainTextResponse("no such turn", status_code=404)
        if kind == "output":
            if t.output_file:
                data = self.hub._read_quiet(t.output_file)
                if data is not None:
                    return PlainTextResponse(turns.normalise(data))
            return PlainTextResponse(t.output or "", status_code=200 if t.output else 404)
        data = self.hub._read_quiet(t.packet_file)
        return PlainTextResponse(turns.normalise(data) if data is not None else "packet file missing", status_code=200 if data else 404)

    async def save_output(self, request: Request) -> Response:
        """The operator's edit of a submitted output (design 3 step 6: edit the file, then next)."""
        if not self._authorised(request):
            return self._denied()
        turn_id = request.path_params["turn_id"]
        body = await self._json(request)
        text = str(body.get("text", "")).replace("\r\n", "\n")
        t = self.hub.book.find(turn_id)
        run = self.hub.book.run
        if t is None or run is None or t.run_id != run.id:
            return JSONResponse({"error": "no such turn in the open run"}, status_code=404)
        if t.state != "submitted" or not t.output_file:
            return JSONResponse({"error": f"{turn_id} has no submitted output to edit"}, status_code=409)
        try:
            from rco.hub import write_atomic

            write_atomic(t.output_file, text if text.endswith("\n") else text + "\n")
        except OSError as e:
            return JSONResponse({"error": f"cannot write {t.output_file}: {e}"}, status_code=500)
        return JSONResponse({"ok": True, "chars": len(text), "file": t.output_file})

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _json(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    async def _run_line(self, line: str) -> list[str]:
        hub = self.hub
        hub.console.line(f"[PANEL] > {line}")  # the console shows what the panel did, like a typed line
        before = hub.log_seq
        try:
            await hub.command(line)
        except Exception:
            log.exception("panel command %r failed", line)
            hub.console.line(f"[ERROR] '{line.split()[0]}' failed: internal error (details in the diag log).")
        return [text for seq, text in list(hub.log) if seq > before]

    def build_state(self, since: int) -> dict[str, Any]:
        hub = self.hub
        snap = hub.snapshot()
        book = hub.book
        run = book.run
        by_id = {c.id: c for c in hub.registry.clients}
        clients = []
        for c in snap["clients"]:
            reg = by_id[c["id"]]
            clients.append({
                **c, "turn_command": reg.turn_command, "turn_hint": reg.turn_hint,
                "invocation_hint": reg.invocation_hint,
            })
        turn_list = []
        if run is not None:
            for t in run.turns.values():
                turn_list.append({
                    "id": t.id, "k": t.k, "client": t.client, "client_name": book.name(t.client), "role": t.role,
                    "skill": t.skill,
                    "note": t.note, "state": t.state, "status": t.status, "inputs": t.inputs,
                    "packet_chars": t.packet_chars, "output_chars": len(t.output) if t.output else None,
                    "summary": turns.summary(t.output) if t.output else None,
                    "offered_ts": t.offered_ts, "claimed_ts": t.claimed_ts, "submitted_ts": t.submitted_ts,
                    "recalled_ts": t.recalled_ts, "output_file": t.output_file, "packet_file": t.packet_file,
                })
        live = book.live_turn
        live_out = None
        if live is not None:
            reg = by_id.get(live.client)
            live_out = {
                "id": live.id, "state": live.state, "client": live.client, "client_name": book.name(live.client),
                "role": live.role, "turn_command": reg.turn_command if reg else "", "turn_hint": reg.turn_hint if reg else "",
                "code": live.code if hub.code_mode else None, "claimed_ts": live.claimed_ts, "offered_ts": live.offered_ts,
            }
        proposal = book.proposal
        return {
            "seq": hub.log_seq,
            "console": [{"seq": s, "text": t} for s, t in list(hub.log) if s > since],
            "paused": snap["paused"], "uptime_s": snap["uptime_s"], "hub_url": snap["url"], "data_dir": snap["data_dir"],
            "records_healthy": snap["records_healthy"], "quorum": snap["quorum"], "clients": clients,
            "run": None if run is None else {
                "id": run.id, "task": run.task, "opened_ts": run.opened_ts, "pause_reasons": list(run.pause_reasons),
                "stop_pending": run.stop_pending, "suspect": bool(run.suspect_lines), "dir": book.run_dir(run.id),
            },
            "turns": turn_list, "live": live_out,
            "proposal": None if proposal is None else {
                "id": proposal.id, "client_name": book.name(proposal.client), "task": proposal.task,
            },
            "status_lines": snap["turn_status"], "roles": list(ROLES), "code_mode": hub.code_mode,
            "skills": [
                {"name": s.name, "file": s.file, "brief": hub.skills.brief(s), "agents": hub.skills.agents(s)}
                for s in sorted(hub.skills.skills.values(), key=lambda s: s.name)
            ],
        }
