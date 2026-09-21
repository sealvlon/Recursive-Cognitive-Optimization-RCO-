"""First project profile: a tightly scoped ASIC configuration engineering task.

The desktop agents propose/review; Python owns candidate writes and frozen checks.
No provider names, credentials, or host controls belong in this profile.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil

DEFAULT_TASK = "Work on the ASIC: correct the template's tile allocation to the current official 6×4 requirement, preserve the RTL, and verify the isolated candidate."


def digest(data):
    return hashlib.sha256(data).hexdigest()


class AsicProfile:
    name = "JSASIC"
    version = "desktop-1.0.0"
    step_costs = (2, 3, 2, 3)  # one claim + submit, plus the protected evaluator where used
    steps = [
        ("builder", "Propose the one-field correction. Return a concise rationale, exact proposed value, and remaining hardware evidence gaps. Text only."),
        ("reviewer", "Adversarially review the proposal against the frozen acceptance criteria. Return JSON with approved (boolean), summary, objections (list). If any requirement is ambiguous, approved must be false. Text only."),
        ("builder", "Inspect the executable evidence supplied by Python. Identify whether the permitted change passed and what was not tested. Do not claim the chip fits or is tapeout-ready. Text only."),
        ("reviewer", "Review the full evidence and final conclusion. Return JSON with approved (boolean), summary, objections (list). Reject any claim extending beyond the static configuration check. Text only."),
    ]

    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.folder = self.workspace / "middleware" / "profiles" / "jane_street"
        self.manifest_path = self.workspace / "middleware" / "evidence" / "asic" / "baseline_manifest.json"
        self.manifest_bytes = self.manifest_path.read_bytes()
        self.manifest = json.loads(self.manifest_bytes)
        self.requirements_path = self.folder / "requirements.json"
        self.requirements_bytes = self.requirements_path.read_bytes()
        if digest(self.requirements_bytes) != self.manifest["requirements_sha256"]:
            raise ValueError("The protected requirements changed; review is required before running.")
        self.requirements = json.loads(self.requirements_bytes)
        self.evaluator_path = self.folder / "verify_template_candidate.py"
        self.evaluator_bytes = self.evaluator_path.read_bytes()
        if digest(self.evaluator_bytes) != self.manifest["evaluator_sha256"]:
            raise ValueError("The protected evaluator changed; review is required before running.")
        spec = importlib.util.spec_from_file_location("rco_frozen_asic_evaluator", self.evaluator_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.evaluate = module.evaluate
        self.revision = digest(self.manifest_bytes)

    def prepare(self, run_id):
        baseline = self.workspace / "asic"
        current = {p.relative_to(baseline).as_posix(): digest(p.read_bytes()) for p in baseline.rglob("*")
                   if p.is_file() and ".git" not in p.relative_to(baseline).parts}
        if current != self.manifest["files"]:
            raise ValueError("The ASIC baseline changed since its frozen manifest. A new baseline must be reviewed.")
        candidate = self.workspace / "middleware" / "candidates" / ("desktop-" + run_id)
        if candidate.exists():
            raise ValueError("Candidate directory already exists; it will not be overwritten.")
        candidate.mkdir(parents=True)
        for relative in self.manifest["files"]:
            dest = candidate / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(baseline / relative, dest)
        result = self.evaluate(candidate, self.manifest)
        return {"candidate": str(candidate), "revision": self.revision, "baseline": result,
                "evaluation": None, "retained": False, "evaluator_sha256": digest(self.evaluator_bytes)}

    def brief(self, state):
        requirements = self.requirements
        return json.dumps({
            "skill": self.name, "skill_version": self.version,
            "artifact_revision": self.revision,
            "official_requirements": requirements["official"],
            "scope": "Only change info.yaml project.tiles from 8x4 to 6x4. Preserve every other byte. Python applies and checks the candidate; desktop agents return text only.",
            "acceptance": ["exact original file inventory", "all RTL/tests/docs bytes preserved",
                           "only authorized tile-value delta", "6x4 value appears exactly once"],
            "permissions": "No tool execution or file edits by agents for this task. No publishing, credentials changes, spending, submission, or evaluator changes.",
            "baseline": state.get("baseline"), "candidate": state.get("candidate"),
            "evaluation": state.get("evaluation"),
            "evidence_boundary": "Static configuration only. Accord remains an architecture/model; RTL simulation, synthesis, area, timing, and physical checks are unrun."
        }, ensure_ascii=False)

    @staticmethod
    def approval(output):
        text = output.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        try:
            value = json.loads(text)
        except ValueError:
            raise ValueError("Reviewer response needs a valid JSON decision; no approval was inferred.")
        if not isinstance(value, dict) or value.get("approved") is not True or value.get("objections"):
            raise ValueError("Reviewer raised an objection or did not approve. Human decision required.")

    def after_turn(self, index, output, state):
        if index in (1, 3):
            if (self.manifest_path.read_bytes() != self.manifest_bytes
                    or self.evaluator_path.read_bytes() != self.evaluator_bytes
                    or self.requirements_path.read_bytes() != self.requirements_bytes):
                raise ValueError("Protected acceptance inputs changed during this run.")
            self.approval(output)
        if index == 1:
            candidate = Path(state["candidate"]).resolve()
            base = (self.workspace / "middleware" / "candidates").resolve()
            if not candidate.is_relative_to(base):
                raise ValueError("Candidate left the approved workspace.")
            info = candidate / "info.yaml"
            before = info.read_bytes()
            if digest(before) != self.manifest["files"]["info.yaml"]:
                raise ValueError("Candidate was edited outside its declared change.")
            after, count = re.subn(rb'(?m)^  tiles: "8x4"(?=[ \t]*\r?$)', b'  tiles: "6x4"', before)
            if count != 1:
                raise ValueError("Expected exactly one tile-allocation field.")
            info.write_bytes(after)
            state["evaluation"] = self.evaluate(candidate, self.manifest)
            report = candidate.parent / (candidate.name + "-evidence.json")
            report.write_text(json.dumps(state["evaluation"], indent=2), encoding="utf-8")
            state["evidence_path"] = str(report)
            if not state["evaluation"]["passed"]:
                raise ValueError("Candidate failed the frozen checks; baseline was not changed.")
            return 1
        if index == 3:
            current = self.evaluate(Path(state["candidate"]), self.manifest)
            if not current["passed"]:
                raise ValueError("Candidate changed after verification and is rejected.")
            state["evaluation"] = current
            state["retained"] = True
            return 1
        return 0
