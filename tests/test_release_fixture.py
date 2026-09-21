"""Exercise the shipped example using isolated copies, never live run state."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from middleware.desktop.profile import AsicProfile
from rco_skills.provisional import describe


ROOT = Path(__file__).resolve().parents[1]
APPROVAL = json.dumps({"approved": True, "summary": "Permitted static delta only.", "objections": []})


def hashes(folder):
    return {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob("*") if path.is_file()}


class ReleaseFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rco-fixture-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in ("asic", "middleware/profiles", "middleware/evidence/asic"):
            shutil.copytree(ROOT / relative, self.root / relative)
        self.profile = AsicProfile(self.root)

    def approved_candidate(self):
        state = self.profile.prepare("release-test")
        self.profile.after_turn(1, APPROVAL, state)
        return state, Path(state["candidate"])

    def test_release_fixture_and_protected_inputs_match_manifest(self):
        manifest = self.profile.manifest
        self.assertEqual(hashes(self.root / "asic"), manifest["files"])
        for filename, key in (("requirements.json", "requirements_sha256"),
                              ("verify_template_candidate.py", "evaluator_sha256")):
            value = hashlib.sha256((self.profile.folder / filename).read_bytes()).hexdigest()
            self.assertEqual(value, manifest[key])
        skill_file = ROOT / "skills/JSASIC.skill.toml"
        skill = describe(skill_file, skill_file.read_bytes())
        self.assertFalse(skill["acts_on_world"])
        self.assertEqual(skill["may_touch"], [])

    def test_full_static_loop_preserves_baseline_and_bounds_evidence(self):
        original = hashes(self.root / "asic")
        state = self.profile.prepare("release-test")
        self.assertFalse(state["baseline"]["passed"])
        self.profile.after_turn(0, "Propose project.tiles = 6x4.", state)
        self.profile.after_turn(1, APPROVAL, state)
        self.profile.after_turn(2, "Configuration check passed; hardware checks are unrun.", state)
        self.profile.after_turn(3, APPROVAL, state)
        self.assertTrue(state["retained"])
        self.assertTrue(state["evaluation"]["passed"])
        self.assertEqual(state["evaluation"]["evidence_level"], "static_configuration_only")
        self.assertIn("RTL simulation", state["evaluation"]["unrun"])
        self.assertEqual(hashes(self.root / "asic"), original)
        changed = [name for name, digest in hashes(Path(state["candidate"])).items()
                   if digest != original[name]]
        self.assertEqual(changed, ["info.yaml"])

    def test_tampered_candidate_fails_and_cannot_be_retained(self):
        state, candidate = self.approved_candidate()
        changes = {
            "rtl": ("src/uart_tx.v", lambda value: value + b"\n// unauthorized\n"),
            "unrelated_config": ("info.yaml", lambda value: value + b"\n# unauthorized\n"),
            "wrong_tile": ("info.yaml", lambda value: value.replace(b'tiles: "6x4"', b'tiles: "8x4"')),
        }
        for name, (relative, mutate) in changes.items():
            with self.subTest(name=name):
                path = candidate / relative
                original = path.read_bytes()
                try:
                    path.write_bytes(mutate(original))
                    self.assertFalse(self.profile.evaluate(candidate, self.profile.manifest)["passed"])
                    with self.assertRaisesRegex(ValueError, "changed after verification"):
                        self.profile.after_turn(3, APPROVAL, state)
                    self.assertFalse(state["retained"])
                finally:
                    path.write_bytes(original)
        extra = candidate / "unexpected.txt"
        extra.write_text("extra", encoding="utf-8")
        self.assertFalse(self.profile.evaluate(candidate, self.profile.manifest)["passed"])
        extra.unlink()
        missing = candidate / "src/uart_tx.v"
        missing.unlink()
        self.assertFalse(self.profile.evaluate(candidate, self.profile.manifest)["passed"])

    def test_changed_baseline_or_evaluator_cannot_start(self):
        rtl = self.root / "asic/src/uart_tx.v"
        rtl.write_bytes(rtl.read_bytes() + b"\n// unexpected baseline edit\n")
        with self.assertRaisesRegex(ValueError, "baseline changed"):
            self.profile.prepare("changed-baseline")
        evaluator = self.profile.evaluator_path
        evaluator.write_bytes(evaluator.read_bytes() + b"\n# unexpected evaluator edit\n")
        with self.assertRaisesRegex(ValueError, "protected evaluator changed"):
            AsicProfile(self.root)

    def test_rejected_review_or_changed_acceptance_input_prevents_write(self):
        state = self.profile.prepare("release-test")
        candidate = Path(state["candidate"])
        original = hashes(candidate)
        for response in ("looks good", '{"approved": false, "objections": []}',
                         '{"approved": true, "objections": ["unresolved"]}'):
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    self.profile.after_turn(1, response, state)
                self.assertEqual(hashes(candidate), original)
        self.profile.manifest_path.write_bytes(self.profile.manifest_bytes + b"\n")
        with self.assertRaisesRegex(ValueError, "acceptance inputs changed"):
            self.profile.after_turn(1, APPROVAL, state)
        self.assertEqual(hashes(candidate), original)

    def test_requirements_changes_block_start_write_and_retention(self):
        path = self.profile.requirements_path
        original = path.read_bytes()
        changed = original + b"\n"
        path.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "protected requirements changed"):
            AsicProfile(self.root)
        path.write_bytes(original)
        state = self.profile.prepare("requirements-check")
        before = hashes(Path(state["candidate"]))
        path.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "acceptance inputs changed"):
            self.profile.after_turn(1, APPROVAL, state)
        self.assertEqual(hashes(Path(state["candidate"])), before)
        path.write_bytes(original)
        self.profile.after_turn(1, APPROVAL, state)
        path.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "acceptance inputs changed"):
            self.profile.after_turn(3, APPROVAL, state)
        self.assertFalse(state["retained"])


if __name__ == "__main__":
    unittest.main()
