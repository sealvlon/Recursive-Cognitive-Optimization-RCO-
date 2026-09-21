"""Offline maintainer smoke check for an extracted Windows release.

Does not start a server, open a window, load desktop settings, or install skills.
Only an isolated temporary fixture and the explicitly selected report are written.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile


def run(report_path):
    """Write a path-free JSON result and return an executable-compatible exit code."""
    checks = []
    phase = "runtime_imports"
    report = {"passed": False, "python": sys.version.split()[0], "checks": checks,
              "evidence_level": "offline_package_and_static_fixture_only",
              "live_desktop_agents_tested": False, "hardware_checks_tested": False}
    try:
        # Keep static imports: PyInstaller must collect these into the executable.
        import mcp
        import uvicorn
        import rco.hub
        import tkinter
        from middleware.desktop.profile import AsicProfile

        checks.append({"name": phase, "passed": True})
        phase = "bundled_tcl_runtime"
        tcl = tkinter.Tcl()
        if not tcl.call("info", "patchlevel"):
            raise RuntimeError("Tcl unavailable")
        checks.append({"name": phase, "passed": True})

        phase = "release_assets"
        root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
        required = ("registry.toml", "skills/JSASIC.skill.toml", "asic/LICENSE",
                    "middleware/desktop/panel.html", "middleware/desktop/project.json",
                    "middleware/desktop_skills/codex/middleware/SKILL.md",
                    "middleware/desktop_skills/claude/middleware/SKILL.md",
                    "middleware/profiles/jane_street/requirements.json",
                    "middleware/profiles/jane_street/verify_template_candidate.py",
                    "middleware/evidence/asic/baseline_manifest.json")
        if any(not (root / name).is_file() or not (root / name).stat().st_size for name in required):
            raise ValueError("Required release assets missing")
        checks.append({"name": phase, "passed": True})

        with tempfile.TemporaryDirectory(prefix="rco-package-check-") as temporary:
            work = Path(temporary)
            phase = "frozen_fixture_integrity"
            for relative in ("asic", "middleware/profiles/jane_street", "middleware/evidence/asic"):
                shutil.copytree(root / relative, work / relative)
            profile = AsicProfile(work)
            expected = profile.manifest["files"]
            sha = lambda value: hashlib.sha256(value).hexdigest()
            inventory = lambda folder: {p.relative_to(folder).as_posix(): sha(p.read_bytes())
                                         for p in folder.rglob("*") if p.is_file()}
            if inventory(work / "asic") != expected:
                raise ValueError("Fixture inventory mismatch")
            if sha((profile.folder / "requirements.json").read_bytes()) != profile.manifest["requirements_sha256"]:
                raise ValueError("Requirements revision mismatch")
            checks.append({"name": phase, "passed": True})

            phase = "four_step_static_fixture_flow"
            state = profile.prepare("package-smoke")
            if state["baseline"]["passed"]:
                raise ValueError("Baseline unexpectedly passed")
            approval = json.dumps({"approved": True, "summary": "Static configuration only.", "objections": []})
            for index, output in enumerate(("Propose the permitted 6x4 allocation.", approval,
                                           "Static configuration passed; hardware checks unrun.", approval)):
                profile.after_turn(index, output, state)
            if not state["retained"] or not state["evaluation"]["passed"]:
                raise ValueError("Candidate not retained")
            if state["evaluation"]["evidence_level"] != "static_configuration_only":
                raise ValueError("Evidence boundary mismatch")
            if inventory(work / "asic") != expected:
                raise ValueError("Source fixture modified")
            checks.append({"name": phase, "passed": True})

            phase = "candidate_tamper_rejected"
            candidate = Path(state["candidate"])
            rtl = candidate / "src/uart_tx.v"
            rtl.write_bytes(rtl.read_bytes() + b"\n// unauthorized smoke-test change\n")
            if profile.evaluate(candidate, profile.manifest)["passed"]:
                raise ValueError("Tampered candidate accepted")
            try:
                profile.after_turn(3, approval, state)
            except ValueError:
                pass
            else:
                raise ValueError("Tampered candidate retained")
            checks.append({"name": phase, "passed": True})
        report["passed"] = True
    except Exception as error:
        checks.append({"name": phase, "passed": False})
        # Exception messages may contain account paths or configuration values.
        report["error"] = {"type": type(error).__name__,
                           "message": "Offline package verification failed at the named check."}

    try:
        Path(report_path).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return 1
    return 0 if report["passed"] else 1
