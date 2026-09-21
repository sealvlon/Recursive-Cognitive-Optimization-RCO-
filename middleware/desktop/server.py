"""One shared RCO MCP hub with a visual, desktop-session-driven workflow.

The AI inference runs in the user's already authenticated desktop coding sessions.
This module never launches an AI CLI, reads provider credentials, or simulates an
app connection. The existing fixed six MCP tools are reused without a new surface.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import importlib
import json
import math
import os
from pathlib import Path
import sys
import time

from middleware.rco_bridge import RCO_ROOT
from rco.hub import Hub, World, write_atomic
from rco.panel import Panel
from rco.records import read_events
from rco.turns import Plan, Refusal
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .profile import AsicProfile, DEFAULT_TASK

DEFAULT_LIMITS = {"max_rounds": 3, "max_model_turns": 6, "max_tool_executions": 20,
                  "max_minutes": 15, "checkpoint_rounds": 1}


def workspace_path():
    return Path(os.environ.get("RCO_DESKTOP_WORKSPACE", Path(__file__).resolve().parents[2])).resolve()


def validate_limits(values):
    if not isinstance(values, dict) or set(values) - set(DEFAULT_LIMITS):
        raise ValueError("Unknown limit setting.")
    out = {**DEFAULT_LIMITS, **values}
    for key, value in out.items():
        if type(value) is not int or value < 1 or value > (120 if key == "max_minutes" else 200):
            raise ValueError("Limits must be positive bounded whole numbers.")
    return out


class DesktopPanel(Panel):
    def __init__(self, hub):
        super().__init__(hub)
        self.app.routes[:] = [Route("/", self.page), Route("/api/state", self.state),
                              Route("/api/control", self.control, methods=["POST"]),
                              Route("/api/text/{kind}/{turn_id}", self.text),
                              Route("/api/health", self.health), Route("/api/open", self.reopen)]

    async def page(self, request):
        if not self._authorised(request):
            return HTMLResponse("Open RCO Middleware from its icon to view this panel.", status_code=403)
        return HTMLResponse((self.hub.workspace / "middleware" / "desktop" / "panel.html").read_text(encoding="utf-8"))

    def build_state(self, since):
        state = super().build_state(since)
        state["desktop"] = self.hub.desktop_snapshot()
        for client in state["clients"]:
            client["listening"] = self.hub.listeners.get(client["id"], 0) > 0
            client["activation_ready"] = client["id"] in self.hub.activation
        return state

    async def control(self, request):
        if not self._authorised(request):
            return self._denied()
        try:
            await self.hub.control(await self._json(request))
            return JSONResponse({"ok": True, "state": self.build_state(0)})
        except (ValueError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)

    async def health(self, request):
        return JSONResponse({"application": "rco-desktop", "workspace": str(self.hub.workspace), "pid": os.getpid()})

    async def reopen(self, request):
        self.hub.world.browser(self.url)
        return JSONResponse({"opened": True})


class DesktopHub(Hub):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.workspace = workspace_path()
        profile_config = self.workspace / "middleware" / "desktop" / "project.json"
        declaration = json.loads(profile_config.read_text(encoding="utf-8")) if profile_config.exists() else {
            "schema_version": 1, "profile_module": "middleware.desktop.profile", "profile_class": "AsicProfile"}
        if declaration.get("schema_version") != 1:
            raise ValueError("Unsupported project profile version.")
        profile_type = getattr(importlib.import_module(declaration["profile_module"]), declaration["profile_class"])
        self.profile = profile_type(self.workspace)
        self.activation = {}
        self.listeners = {}
        self.scheduler_lock = asyncio.Lock()
        self.state_path = self.cfg.data_dir / "desktop-state.json"
        self.desktop = {"status": "waiting", "mode": "human", "task": DEFAULT_TASK,
                        "limits": dict(DEFAULT_LIMITS), "armed": True, "cursor": 0, "approved": None,
                        "stop_reason": None, "pending": None, "model_turns": 0, "tool_executions": 0,
                        "evidence": None, "profile": self.profile.name, "skill_version": self.profile.version,
                        "builder": self.registry.clients[0].id, "reviewer": self.registry.clients[1].id,
                        "profile_state": {}, "outcome": None}
        if self.state_path.exists():
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.desktop.update(saved)
            if self.book.run:
                latest = [r for r in read_events(self.records.path) if r["event"] == "run_note"
                          and r.get("run_id") == self.book.run.id and "desktop_state" in r]
                if latest:
                    self.desktop.update(latest[-1]["desktop_state"])
                committed = len(self.book.run.turns_in("submitted"))
                if committed != self.desktop["cursor"]:
                    self.desktop.update(status="blocked", armed=False, approved=None,
                                        stop_reason="A submission committed before its workflow checkpoint. Reconcile the saved evidence before continuing; no work was repeated.")
                elif self.desktop["status"] not in ("completed", "blocked", "stopped"):
                    self.desktop.update(status="paused", armed=False, approved=None,
                                        stop_reason="Recovered checkpoint. Review before resuming; live claims are not repeated.")
            else:
                self.desktop.update(status="waiting", armed=False, cursor=0, approved=None, stop_reason=None)
        elif self.book.run:
            self.desktop.update(status="blocked", armed=False, stop_reason="Run exists without a workflow checkpoint. Review required.")
        self.panel = DesktopPanel(self)

    def save(self):
        if self.book.run:
            if not self.record("run_note", run_id=self.book.run.id, text="desktop workflow checkpoint v1",
                               desktop_state=json.loads(json.dumps(self.desktop))):
                raise RuntimeError("Cannot persist the workflow checkpoint.")
        write_atomic(str(self.state_path), json.dumps(self.desktop, indent=2), make_parent=True)

    def apply_plan(self, plan):
        if isinstance(plan, Refusal):
            raise RuntimeError(plan.text or plan.detail or plan.reason)
        plan, _ = self._prepare(plan)
        if isinstance(plan, Refusal):
            raise RuntimeError(plan.text or plan.detail or plan.reason)
        lines = self._apply(plan)
        if lines is None:
            raise RuntimeError("The event journal could not be written.")
        return plan

    def desktop_snapshot(self):
        d = json.loads(json.dumps(self.desktop))
        d["rounds"] = d["cursor"] // 2
        d["activation"] = {"codex": "$middleware OpenAI", "claude": "/middleware Claude"}
        d["connected_count"] = sum(st.registered and self.present(st) for st in self.state.values())
        d["listening_count"] = sum(bool(v) for v in self.listeners.values())
        d["tool_execution_label"] = "Coordinator operations; desktop app tool usage is not observable"
        d["session_type"] = "Existing desktop coding sessions through the shared local RCO MCP server"
        return d

    async def register(self, name, ctx):
        result = await super().register(name, ctx)
        if not result.is_error:
            c = self._caller_client(ctx)
            self.activation[c.id] = self.mono()
            result = self._text("RCO desktop: connected\n" + "\n".join(x.text for x in result.content if hasattr(x, "text"))
                                + "\nStay in this desktop session. Call get_turn(wait_s=45), do each claimed task, submit, and wait again until stopped/completed/disconnected.")
        return result

    def exceeded(self):
        d, limits = self.desktop, self.desktop["limits"]
        started = d.get("started_at")
        if started and (time.time() < started or time.time() - started >= limits["max_minutes"] * 60):
            return "Run time limit reached."
        if self.book.live_turn:
            return None  # a budget admitted this claim; allow its bounded result to arrive
        if d["model_turns"] >= min(limits["max_model_turns"], limits["max_rounds"] * 2):
            return "Model turn / round limit reached."
        cost = self.profile.step_costs[d["cursor"]] if d["cursor"] < len(self.profile.steps) else 0
        if d["tool_executions"] + cost > limits["max_tool_executions"]:
            return "Coordinator operation limit reached."
        return None

    def halt(self, reason, status="blocked"):
        d = self.desktop
        live = self.book.live_turn
        if live:
            self.apply_plan(self.book.check_recall(live.id, self._at()))
        d.update(status=status, armed=False, approved=None, pending=None, stop_reason=reason)
        if self.book.run and self.book.run.stop_pending:
            d["stop_reason"] += " An already running desktop turn cannot be forcibly cancelled by MCP. Press Stop in that app; its late result is quarantined."
        self.save()
        self._changed()

    async def control(self, body):
        async with self.scheduler_lock:
            action = body.get("action")
            d = self.desktop
            if action == "start":
                if self.book.live_turn or (self.book.run and self.book.run.stop_pending):
                    raise ValueError("A turn is still unresolved. Stop or reconcile it before starting another run.")
                if self.book.run:
                    self.apply_plan(self.book.check_end("closed before new run", self._at(), self._read_quiet))
                mode = body.get("mode", "human")
                if mode not in ("human", "supervised", "auto"):
                    raise ValueError("Unknown operating mode.")
                task = str(body.get("task", DEFAULT_TASK)).strip()
                if not task or len(task) > 4000:
                    raise ValueError("Provide a task of 1 to 4000 characters.")
                limits = validate_limits(body.get("limits", {}))
                if min(limits["max_model_turns"], limits["max_rounds"] * 2) < len(self.profile.steps):
                    raise ValueError(f"This task needs {len(self.profile.steps)} model turns.")
                if limits["max_tool_executions"] < sum(self.profile.step_costs):
                    raise ValueError(f"This task needs {sum(self.profile.step_costs)} coordinator operations, including its verification checks.")
                d.update(status="waiting", armed=True, mode=mode, task=task, limits=limits, cursor=0,
                         approved=None, pending=None, stop_reason=None, model_turns=0, tool_executions=0,
                         profile_state={}, outcome=None, evidence=None)
                self.paused = False
            elif action == "approve":
                if not d["pending"] or d["status"] != "approval":
                    raise ValueError("No handoff is waiting for approval.")
                d.update(approved=d["cursor"], pending=None, status="running")
            elif action == "pause":
                if d["status"] not in ("running", "waiting", "approval"):
                    raise ValueError("There is no active run to pause.")
                d.update(status="paused", approved=None, pending=None)
                self.paused = True
            elif action == "resume":
                if d["status"] != "paused":
                    raise ValueError("There is no paused run to resume.")
                if self.book.live_turn and self.book.live_turn.state == "claimed":
                    raise ValueError("A claimed turn is unresolved. Wait for it, or stop it in its desktop app.")
                if self.book.run and self.book.run.stop_pending:
                    raise ValueError("Confirm that the recalled desktop turn stopped before resuming.")
                plan = self.book.check_resume()
                if plan:
                    self.apply_plan(plan)
                self.paused = False
                d.update(status="running" if self.book.run else "waiting", armed=True, approved=None, stop_reason=None)
            elif action == "stop":
                self.halt("Stopped by you.", "stopped")
                return
            elif action == "confirm_stopped":
                self.apply_plan(self.book.check_stopped(self._at()))
                d["stop_reason"] = "Desktop cancellation confirmed by you."
            elif action == "shutdown":
                self.halt("RCO application closed by you.", "stopped")
                asyncio.get_running_loop().call_later(.3, self.request_stop, "operator")
                return
            else:
                raise ValueError("Unknown control.")
            self.save()
            self._changed()
            await self.pump_locked()

    async def pump(self):
        async with self.scheduler_lock:
            await self.pump_locked()

    async def pump_locked(self):
        d = self.desktop
        if not d["armed"] or d["status"] in ("paused", "stopped", "completed", "blocked"):
            return
        if not self.quorum_ok() or not all(c.id in self.activation for c in self.registry.clients):
            d["status"] = "waiting"
            return
        if not self.book.run:
            # Both native agents must have actually entered get_turn, not just registered.
            if not all(self.listeners.get(c.id, 0) for c in self.registry.clients):
                d["status"] = "waiting"
                return
            self.apply_plan(self.book.check_run(d["task"], self._at(), folder_names=self._run_folder_names()))
            d["started_at"] = time.time()
            try:
                d["profile_state"] = self.profile.prepare(self.book.run.id)
            except Exception as exc:
                self.halt(str(exc))
                return
            d.update(status="running", artifact_revision=self.profile.revision)
            self.save()
        if d["cursor"] >= len(self.profile.steps):
            d.update(status="completed", armed=False, pending=None,
                     outcome="Verified candidate retained; baseline unchanged.", stop_reason="Task completed.")
            self.save()
            self._changed()
            return
        reason = self.exceeded()
        if reason:
            self.halt(reason)
            return
        if self.book.live_turn:
            return
        cursor = d["cursor"]
        needs_approval = cursor > 0 and (d["mode"] == "human" or
                            (d["mode"] == "supervised" and cursor % (2 * d["limits"]["checkpoint_rounds"]) == 0))
        if needs_approval and d["approved"] != cursor:
            d.update(status="approval", pending="Approve the next inter-agent handoff.")
            self.save()
            return
        role, action = self.profile.steps[cursor]
        recipient = d[role]
        previous = d[self.profile.steps[cursor - 1][0]] if cursor else "operator"
        note = json.dumps({"sender": previous, "recipient": recipient, "run": self.book.run.id,
                           "step": cursor + 1, "artifact_revision": self.profile.revision,
                           "requested_next_action": action, "profile": json.loads(self.profile.brief(d["profile_state"]))}, ensure_ascii=False)
        self.apply_plan(self.book.check_next(recipient, role, [], note, self._at(), self._gate(), self._read_quiet,
                                            skill=(self.profile.name, self.profile.revision, "Follow the task profile and its frozen acceptance criteria. Return concise evidence, not private reasoning.")))
        d.update(status="running", pending=None, approved=None)
        self.save()

    async def get_turn(self, wait_s, code, ctx):
        c = self._caller_client(ctx)
        if not self.state[c.id].registered:
            return self._text("RCO desktop: disconnected\nActivate the middleware in this desktop session first.")
        if c.id not in self.activation or self.mono() - self.activation[c.id] > 1800:
            self.activation.pop(c.id, None)
            return self._text("RCO desktop: disconnected\nThe bounded desktop activation expired. Reconnect when ready.")
        if wait_s is not None and (isinstance(wait_s, bool) or not isinstance(wait_s, (float, int)) or not math.isfinite(wait_s) or wait_s < 0):
            return self._text("RCO desktop: disconnected\nInvalid waiting interval.")
        limit = min(45, 45 if wait_s is None else wait_s)
        deadline = self.mono() + limit
        progress_at = self.mono() + 10
        self.listeners[c.id] = self.listeners.get(c.id, 0) + 1
        try:
            while True:
                await self.pump()
                d = self.desktop
                if d["status"] in ("stopped", "completed", "blocked"):
                    marker = "completed" if d["status"] == "completed" else "stopped"
                    self.activation.pop(c.id, None)
                    return self._text(f"RCO desktop: {marker}\n{d['stop_reason']}")
                if d["status"] == "running" and not self.paused and not self.exceeded():
                    self.book.count_fetch(c.id, 0)
                    plan = self._fetch(c, 0, code, self._caller(ctx))
                    if isinstance(plan, Plan):
                        result = self._tool_result(c, plan, "get_turn")
                        if not result.is_error:
                            d["model_turns"] += 1
                            d["tool_executions"] += 1
                            self.save()
                        return result
                    if plan.reason not in ("no_run", "not_yours", "run_paused", "already_claimed", "waiting"):
                        return self._tool_result(c, plan, "get_turn")
                now = self.mono()
                if now >= deadline:
                    return self._text("RCO desktop: waiting\n" + (d.get("pending") or d.get("stop_reason") or "Waiting for the other desktop session or the visual controls.") + "\nCall get_turn(wait_s=45) again without a new user command.")
                if now >= progress_at:
                    with contextlib.suppress(Exception):
                        await ctx.report_progress(limit - (deadline - now), limit)
                    progress_at = now + 10
                changed = self._turn_changed
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(changed.wait(), min(1, max(.01, deadline - now)))
        finally:
            self.listeners[c.id] -= 1

    async def submit_turn(self, turn_id, nonce, output, status, files_changed, ctx):
        async with self.scheduler_lock:
            c = self._caller_client(ctx)
            turn = self.book.find(turn_id)
            was_submitted = turn is not None and turn.state == "submitted"
            if self.desktop.get("started_at") and time.time() - self.desktop["started_at"] >= self.desktop["limits"]["max_minutes"] * 60 and not was_submitted:
                self.halt("Run time limit reached before the result arrived.")
            result = await super().submit_turn(turn_id, nonce, output, status, files_changed, ctx)
            if result.is_error or was_submitted:
                return result
            d = self.desktop
            d["tool_executions"] += 1
            if status in ("blocked", "declined"):
                self.halt("The desktop agent reported a blocker. Review its evidence.")
                return result
            try:
                d["tool_executions"] += self.profile.after_turn(d["cursor"], output, d["profile_state"])
                d["evidence"] = d["profile_state"].get("evaluation")
                d["cursor"] += 1
                self.save()
            except Exception as exc:
                self.halt(str(exc))
                return result
            await self.pump_locked()
            return result

    async def _monitor(self):
        while True:
            await asyncio.sleep(1)
            self.check_presence()
            self.flush_auth()
            try:
                await self.pump()
            except Exception as exc:
                self.halt("Coordinator paused: " + str(exc))


def make_config(workspace):
    folder = workspace / ".rco-desktop"
    folder.mkdir(parents=True, exist_ok=True)
    quote = lambda p: json.dumps(str(p))
    content = f'''[hub]
host = "127.0.0.1"
port = 8799
registry = {quote(RCO_ROOT / "registry.toml")}
[paths]
data_dir = {quote(workspace / ".rco-desktop" / "data")}
skills_dir = {quote(RCO_ROOT / "skills")}
[skills]
adapter = "rco_skills.provisional"
[records]
on_write_failure = "fail_action"
[console]
color = "never"
[turns]
packet_cap_chars = 24000
output_cap_chars = 6000
entry_code = "off"
[panel]
enabled = true
ui_port = 8798
open_browser = true
'''
    path = folder / "desktop.toml"
    path.write_text(content, encoding="utf-8")
    return path


def run():
    workspace = workspace_path()
    from middleware.rco_bridge.desktop_setup import install_desktop_skills
    install_desktop_skills(source_root=workspace / "middleware" / "desktop_skills")
    config = make_config(workspace)
    world = World(out=io.StringIO())
    hub = DesktopHub.create([], world=world, default_config=config)
    return hub.run()
