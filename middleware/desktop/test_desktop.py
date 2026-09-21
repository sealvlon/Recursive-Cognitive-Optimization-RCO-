"""Scripted MCP transport tests, not proof of authenticated desktop model inference."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from middleware.desktop.server import DesktopHub, validate_limits
from middleware.rco_bridge import RCO_ROOT
from tests import harness as legacy


WORKSPACE = Path(__file__).resolve().parents[2]


def first_line(result):
    return legacy.text(result).splitlines()[0]


def claim_fields(result):
    text = legacy.text(result)
    if not text.startswith("RCO turn: claimed\n"):
        raise AssertionError(text)
    fields = dict(line.split(": ", 1) for line in text.splitlines()[1:4])
    return fields["turn_id"], fields["nonce"]


@contextmanager
def desktop_harness():
    with tempfile.TemporaryDirectory(prefix="rco-desktop-test-") as temporary:
        base = Path(temporary)
        project = base / "project"
        manifest = json.loads((WORKSPACE / "middleware/evidence/asic/baseline_manifest.json").read_text())
        for relative in manifest["files"]:
            target = project / "asic" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(WORKSPACE / "asic" / relative, target)
        for relative in (
            "middleware/evidence/asic/baseline_manifest.json",
            "middleware/profiles/jane_street/verify_template_candidate.py",
            "middleware/profiles/jane_street/requirements.json",
        ):
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(WORKSPACE / relative, target)
        # Harness credentials and sockets are synthetic and all writes stay under temporary.
        with patch.object(legacy, "Hub", DesktopHub), patch(
            "middleware.desktop.server.workspace_path", return_value=project
        ):
            h = legacy.HubHarness(base / "hub")
        try:
            yield h
        finally:
            h.close()


async def control(h, **body):
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(h.hub.control(body), h.hub.loop))


async def register_pair(a, b):
    for client, name in ((a, "Claude"), (b, "OpenAI")):
        result = await client.call_tool("register", {"name": name})
        assert first_line(result) == "RCO desktop: connected", legacy.text(result)


async def first_claim(a, b):
    waiting = asyncio.create_task(a.call_tool("get_turn", {"wait_s": 3}))
    await asyncio.sleep(.05)
    other = await b.call_tool("get_turn", {"wait_s": .05})
    claim = await waiting
    assert first_line(other) == "RCO desktop: waiting", legacy.text(other)
    claim_fields(claim)
    return claim


async def submit(client, claimed, output="Propose project.tiles=6x4. Static configuration only."):
    turn_id, nonce = claim_fields(claimed)
    return await client.call_tool("submit_turn", {
        "turn_id": turn_id, "nonce": nonce, "status": "done", "output": output,
    })


APPROVED = json.dumps({"approved": True, "summary": "Static evidence accepted.", "objections": []})


class DesktopTransportTests(unittest.TestCase):
    def test_registration_requires_both_real_listeners_before_run(self):
        with desktop_harness() as h:
            async def scenario():
                async with legacy.claude_like(h) as a, legacy.codex_like(h) as b:
                    await register_pair(a, b)
                    self.assertIsNone(h.hub.book.run)
                    result = await a.call_tool("get_turn", {"wait_s": .05})
                    self.assertEqual(first_line(result), "RCO desktop: waiting")
                    self.assertIsNone(h.hub.book.run)
                    claim = await first_claim(a, b)
                    self.assertEqual(h.hub.desktop["model_turns"], 1)
                    self.assertIn("role: builder", legacy.text(claim))
            legacy.arun(scenario())

    def test_human_handoff_blocks_until_approval_and_forwards_previous_output(self):
        with desktop_harness() as h:
            async def scenario():
                async with legacy.claude_like(h) as a, legacy.codex_like(h) as b:
                    await register_pair(a, b)
                    claim = await first_claim(a, b)
                    proposal = "PROPOSAL-UNIQUE-917: set tiles to 6x4; no hardware proof."
                    result = await submit(a, claim, proposal)
                    self.assertEqual(first_line(result), "RCO turn: received")
                    self.assertEqual(h.hub.desktop["status"], "approval")
                    self.assertIsNone(h.hub.book.live_turn)
                    self.assertEqual(first_line(await b.call_tool("get_turn", {"wait_s": .01})), "RCO desktop: waiting")
                    await control(h, action="approve")
                    next_claim = await b.call_tool("get_turn", {"wait_s": .01})
                    claim_fields(next_claim)
                    self.assertIn(proposal, legacy.text(next_claim))
            legacy.arun(scenario())

    def test_auto_four_steps_at_exact_turn_limit_and_duplicate_is_idempotent(self):
        with desktop_harness() as h:
            baseline = (h.hub.workspace / "asic/info.yaml").read_bytes()
            async def scenario():
                async with legacy.claude_like(h) as a, legacy.codex_like(h) as b:
                    await control(h, action="start", mode="auto", limits={"max_model_turns": 4, "max_rounds": 2, "max_tool_executions": 10})
                    await register_pair(a, b)
                    current = await first_claim(a, b)
                    outputs = ["PROPOSAL-UNIQUE-419: 6x4 tile correction; static-only.", APPROVED,
                               "The frozen configuration checks passed; simulation and timing remain unrun.", APPROVED]
                    for index, (client, output) in enumerate(zip((a, b, a, b), outputs)):
                        if index:
                            current = await client.call_tool("get_turn", {"wait_s": .05})
                            self.assertIn(outputs[index - 1], legacy.text(current))
                        if index == 3:
                            # Allow the other listener and scheduler to inspect the fully used turn budget.
                            waiting = await a.call_tool("get_turn", {"wait_s": .05})
                            self.assertEqual(first_line(waiting), "RCO desktop: waiting")
                        result = await submit(client, current, output)
                        self.assertEqual(first_line(result), "RCO turn: received")
                        cursor = h.hub.desktop["cursor"]
                        duplicate = await submit(client, current, output)
                        self.assertEqual(first_line(duplicate), "RCO turn: duplicate")
                        self.assertEqual(h.hub.desktop["cursor"], cursor)
                    self.assertEqual(h.hub.desktop["status"], "completed")
                    self.assertEqual(h.hub.desktop["model_turns"], 4)
                    self.assertEqual(h.hub.desktop["tool_executions"], 10)
                    self.assertTrue(h.hub.desktop["profile_state"]["retained"])
                    self.assertEqual(first_line(await a.call_tool("get_turn", {"wait_s": 0})), "RCO desktop: completed")
            legacy.arun(scenario())
            self.assertEqual((h.hub.workspace / "asic/info.yaml").read_bytes(), baseline)
            self.assertEqual(len(h.records("turn_submitted")), 4)

    def test_pause_blocks_handoff_and_resume_continues(self):
        with desktop_harness() as h:
            async def scenario():
                async with legacy.claude_like(h) as a, legacy.codex_like(h) as b:
                    await control(h, action="start", mode="auto")
                    await register_pair(a, b)
                    claim = await first_claim(a, b)
                    await control(h, action="pause")
                    self.assertEqual(first_line(await submit(a, claim)), "RCO turn: received")
                    self.assertIsNone(h.hub.book.live_turn)
                    self.assertEqual(first_line(await b.call_tool("get_turn", {"wait_s": .01})), "RCO desktop: waiting")
                    await control(h, action="resume")
                    claim_fields(await b.call_tool("get_turn", {"wait_s": .01}))
            legacy.arun(scenario())

    def test_stop_quarantines_late_result_and_preserves_baseline(self):
        with desktop_harness() as h:
            async def scenario():
                async with legacy.claude_like(h) as a, legacy.codex_like(h) as b:
                    await register_pair(a, b)
                    claim = await first_claim(a, b)
                    await control(h, action="stop")
                    result = await submit(a, claim, "Late output must not advance the workflow.")
                    self.assertEqual(first_line(result), "RCO turn: refused recalled")
                    self.assertEqual(h.hub.desktop["cursor"], 0)
                    self.assertFalse(h.hub.desktop["profile_state"]["retained"])
                    self.assertIn("cannot be forcibly cancelled", h.hub.desktop["stop_reason"])
                    self.assertEqual(first_line(await b.call_tool("get_turn", {"wait_s": 0})), "RCO desktop: stopped")
            legacy.arun(scenario())

    def test_wall_time_expiry_rejects_claimed_result(self):
        with desktop_harness() as h:
            async def scenario():
                async with legacy.claude_like(h) as a, legacy.codex_like(h) as b:
                    await register_pair(a, b)
                    claim = await first_claim(a, b)
                    h.hub.desktop["started_at"] = time.time() - 100000
                    result = await submit(a, claim)
                    self.assertEqual(first_line(result), "RCO turn: refused recalled")
                    self.assertEqual(h.hub.desktop["cursor"], 0)
                    self.assertIn("time limit", h.hub.desktop["stop_reason"])
            legacy.arun(scenario())

    def test_reviewer_rejection_prevents_candidate_change(self):
        with desktop_harness() as h:
            async def scenario():
                async with legacy.claude_like(h) as a, legacy.codex_like(h) as b:
                    await control(h, action="start", mode="auto")
                    await register_pair(a, b)
                    await submit(a, await first_claim(a, b))
                    claim = await b.call_tool("get_turn", {"wait_s": .01})
                    await submit(b, claim, json.dumps({"approved": False, "summary": "Objection", "objections": ["Cannot verify."]}))
                    state = h.hub.desktop["profile_state"]
                    self.assertEqual(h.hub.desktop["status"], "blocked")
                    self.assertFalse(state["retained"])
                    self.assertEqual((Path(state["candidate"]) / "info.yaml").read_bytes(),
                                     (h.hub.workspace / "asic/info.yaml").read_bytes())
            legacy.arun(scenario())

    def test_supervised_checkpoint_after_one_round(self):
        with desktop_harness() as h:
            async def scenario():
                async with legacy.claude_like(h) as a, legacy.codex_like(h) as b:
                    await control(h, action="start", mode="supervised", limits={"checkpoint_rounds": 1})
                    await register_pair(a, b)
                    await submit(a, await first_claim(a, b))
                    self.assertEqual(h.hub.desktop["status"], "running")
                    await submit(b, await b.call_tool("get_turn", {"wait_s": .01}), APPROVED)
                    self.assertEqual(h.hub.desktop["status"], "approval")
                    self.assertEqual(h.hub.desktop["cursor"], 2)
                    self.assertEqual(first_line(await a.call_tool("get_turn", {"wait_s": .01})), "RCO desktop: waiting")
                    await control(h, action="approve")
                    claim_fields(await a.call_tool("get_turn", {"wait_s": .01}))
            legacy.arun(scenario())

    def test_operation_limit_reserves_the_frozen_evaluator(self):
        with desktop_harness() as h:
            async def scenario():
                with self.assertRaisesRegex(ValueError, "10 coordinator operations"):
                    await control(h, action="start", mode="auto", limits={"max_tool_executions": 4})
                self.assertIsNone(h.hub.book.run)
                self.assertEqual(h.hub.desktop["tool_executions"], 0)
            legacy.arun(scenario())

    def test_corrupted_verified_candidate_is_rejected_at_final_review(self):
        with desktop_harness() as h:
            async def scenario():
                async with legacy.claude_like(h) as a, legacy.codex_like(h) as b:
                    await control(h, action="start", mode="auto")
                    await register_pair(a, b)
                    await submit(a, await first_claim(a, b))
                    await submit(b, await b.call_tool("get_turn", {"wait_s": .01}), APPROVED)
                    state = h.hub.desktop["profile_state"]
                    self.assertTrue(state["evaluation"]["passed"])
                    await submit(a, await a.call_tool("get_turn", {"wait_s": .01}), "Static checks only.")
                    candidate = Path(state["candidate"])
                    original = (h.hub.workspace / "asic/src/project.v").read_bytes()
                    (candidate / "src/project.v").write_bytes(original + b"\n// unauthorized delta\n")
                    await submit(b, await b.call_tool("get_turn", {"wait_s": .01}), APPROVED)
                    self.assertEqual(h.hub.desktop["status"], "blocked")
                    self.assertFalse(state["retained"])
                    self.assertEqual((h.hub.workspace / "asic/src/project.v").read_bytes(), original)
            legacy.arun(scenario())

    def test_recovery_preserves_claim_and_requires_reconciliation(self):
        with desktop_harness() as h:
            async def first_session():
                async with legacy.claude_like(h) as a, legacy.codex_like(h) as b:
                    await register_pair(a, b)
                    return claim_fields(await first_claim(a, b))[0]
            claimed_id = legacy.arun(first_session())
            workspace = h.hub.workspace
            h.close()
            with patch.object(legacy, "Hub", DesktopHub), patch(
                "middleware.desktop.server.workspace_path", return_value=workspace
            ):
                recovered = legacy.HubHarness(h.base)
            try:
                self.assertEqual(recovered.hub.desktop["status"], "paused")
                self.assertEqual(recovered.hub.book.live_turn.id, claimed_id)
                self.assertEqual(recovered.hub.book.live_turn.state, "claimed")
                async def second_session():
                    async with legacy.claude_like(recovered) as a, legacy.codex_like(recovered) as b:
                        await register_pair(a, b)
                        self.assertEqual(first_line(await a.call_tool("get_turn", {"wait_s": .01})), "RCO desktop: waiting")
                        with self.assertRaisesRegex(ValueError, "claimed turn is unresolved"):
                            await control(recovered, action="resume")
                legacy.arun(second_session())
                self.assertEqual(len(recovered.records("turn_claimed")), 1)
            finally:
                recovered.close()


class DesktopInputTests(unittest.TestCase):
    def test_limits_reject_bool_negative_unknown_and_unbounded_values(self):
        for settings in ({"max_rounds": True}, {"max_minutes": 0}, {"max_minutes": 121},
                         {"max_model_turns": 201}, {"max_retries": 2}, []):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                validate_limits(settings)


if __name__ == "__main__":
    unittest.main()
