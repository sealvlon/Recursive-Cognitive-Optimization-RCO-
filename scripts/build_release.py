"""Create clean source and Windows release ZIPs. Maintainer entry point only."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("middleware", "rco", "rco_skills", "skills", "asic", "tests", "scripts", "docs", ".github")
ROOT_FILES = ("README.md", "SECURITY.md", "CONTRIBUTING.md", "THIRD_PARTY_NOTICES.md",
              ".gitignore", ".gitattributes", "VERSION", "requirements.txt", "requirements-dev.txt", "registry.toml")
EXCLUDED = {"__pycache__", ".git", ".state", ".rco-desktop", ".build", ".build-deps", ".test-deps", "candidates", "config"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def source_files():
    selected = {Path(name): ROOT / name for name in ROOT_FILES if (ROOT / name).is_file()}
    for directory in FOLDERS:
        for file in (ROOT / directory).rglob("*"):
            rel = file.relative_to(ROOT)
            if not file.is_file() or any(part in EXCLUDED for part in rel.parts):
                continue
            if file.suffix in {".pyc", ".pyo", ".log", ".exe"}:
                continue
            if "evidence" in rel.parts and rel.as_posix() != "middleware/evidence/asic/baseline_manifest.json":
                continue
            if file.name == ".env" or file.name.startswith(".env."):
                raise RuntimeError(f"Refusing to publish environment file: {rel}")
            selected[rel] = file
    for rel, file in selected.items():
        if not file.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError(f"Refusing a source link outside the repository: {rel}")
        for part in (file, *file.parents):
            if part == ROOT:
                break
            if part.is_symlink() or part.is_junction():
                raise RuntimeError(f"Refusing a linked source file or folder: {rel}")
        data = file.read_bytes()
        if re.search(rb"[A-Za-z]:[\\/]+Users[\\/]+", data, re.I):
            raise RuntimeError(f"Personal Windows path found in {rel}")
        if re.search(rb"(?:sk-[A-Za-z0-9_-]{24,}|ghp_[A-Za-z0-9]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)", data):
            raise RuntimeError(f"Possible credential found in {rel}; inspect before release.")
    return selected


def source_hashes():
    return {rel.as_posix(): sha256(file.read_bytes()) for rel, file in sorted(source_files().items())}


def collect_licenses():
    """Copy upstream notices from the actual build environment."""
    folder = ROOT / "dist/THIRD_PARTY_LICENSES"
    folder.mkdir(parents=True, exist_ok=True)
    dependencies = []
    copied = {}
    names = [line.split("==")[0] for line in (ROOT / "requirements.txt").read_text().splitlines()
             if "==" in line and not line.startswith("#")]
    names += ["pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "packaging", "pefile", "pywin32-ctypes"]
    for name in sorted(set(names), key=str.casefold):
        dist = metadata.distribution(name)
        notices = []
        for relative in dist.files or ():
            if not re.search(r"(?:license|licence|copying|notice|authors)", str(relative), re.I):
                continue
            source = Path(dist.locate_file(relative))
            if not source.is_file() or source.suffix in {".py", ".pyc", ".pyd"}:
                continue
            safe = str(relative).replace("\\", "/").split("/")
            if ".." in safe:
                continue
            target = folder / name / Path(*safe)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied[Path("THIRD_PARTY_LICENSES") / target.relative_to(folder)] = target
            notices.append(target.relative_to(folder).as_posix())
        if not notices:
            raise RuntimeError(f"No installed license notice found for {name}; review before shipping.")
        dependencies.append({"name": dist.metadata["Name"], "version": dist.version,
                             "license": dist.metadata.get("License-Expression") or dist.metadata.get("License", "See notices"),
                             "notices": notices})
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("CPython license is missing from the build environment.")
    shutil.copyfile(python_license, folder / "PYTHON-LICENSE.txt")
    copied[Path("THIRD_PARTY_LICENSES/PYTHON-LICENSE.txt")] = folder / "PYTHON-LICENSE.txt"
    (folder / "dependencies.json").write_text(json.dumps({"python": sys.version.split()[0], "packages": dependencies}, indent=2) + "\n", encoding="utf-8")
    copied[Path("THIRD_PARTY_LICENSES/dependencies.json")] = folder / "dependencies.json"
    return copied


def write_archive(path, files, version, kind):
    manifest = {"version": version, "kind": kind, "files": {rel.as_posix(): sha256(file.read_bytes())
                for rel, file in sorted(files.items())}}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, source in sorted(files.items()):
            archive.write(source, "rco-desktop/" + relative.as_posix())
        archive.writestr("rco-desktop/PACKAGE-MANIFEST.json", json.dumps(manifest, indent=2) + "\n")
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError(f"ZIP integrity check failed: {path.name}")
        for relative, digest in manifest["files"].items():
            if sha256(archive.read("rco-desktop/" + relative)) != digest:
                raise RuntimeError(f"Archive hash mismatch: {relative}")


def package(version):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?", version):
        raise ValueError("Version must be a safe semantic version.")
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    files = source_files()
    executable = dist / "RCO Middleware.exe"
    if not executable.is_file():
        raise RuntimeError("Build the executable before packaging.")
    build_path = dist / "BUILD-INFO.json"
    build = json.loads(build_path.read_text(encoding="utf-8"))
    if build["source_sha256"] != source_hashes() or build["executable_sha256"] != sha256(executable.read_bytes()):
        raise RuntimeError("The executable and source no longer match. Rebuild before packaging.")
    windows_files = {**files, Path("RCO Middleware.exe"): executable,
                     Path("BUILD-INFO.json"): build_path, **collect_licenses()}
    outputs = [(dist / f"rco-desktop-{version}-source.zip", files, "source"),
               (dist / f"rco-desktop-{version}-windows-x64.zip", windows_files, "windows-x64")]
    for path, selected, kind in outputs:
        write_archive(path, selected, version, kind)
    # Exercise the frozen runtime after extraction somewhere outside the checkout.
    # This mode does not read app settings, open a window, or start any service.
    with tempfile.TemporaryDirectory(prefix="rco-release-verification-") as temporary:
        extracted = Path(temporary)
        with zipfile.ZipFile(outputs[1][0]) as archive:
            archive.extractall(extracted)
        report_path = extracted / "verification.json"
        app = extracted / "rco-desktop/RCO Middleware.exe"
        try:
            subprocess.run([str(app), "--verify-package", str(report_path)], cwd=app.parent,
                           check=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        except OSError as error:
            if getattr(error, "winerror", None) != 4551:
                raise
            # Keep policy enforcement intact. Ship an explicit blocked result;
            # do not retry from another location or claim an execution pass.
            report = {"passed": False, "status": "blocked_by_windows_application_control",
                      "winerror": 4551, "checks": [], "live_desktop_agents_tested": False,
                      "hardware_checks_tested": False,
                      "message": "The build host blocked execution of the unsigned application. Runtime verification did not run."}
            print("Windows Application Control blocked the executable check; see VALIDATION.json.")
        else:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not report.get("passed"):
                raise RuntimeError("Extracted executable verification failed.")
    report["version"] = version
    report["executable_sha256"] = build["executable_sha256"]
    (dist / "VALIDATION.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sums = "".join(f"{sha256(path.read_bytes())}  {path.name}\n" for path, _, _ in outputs)
    sums += f"{sha256((dist / 'VALIDATION.json').read_bytes())}  VALIDATION.json\n"
    (dist / "SHA256SUMS.txt").write_text(sums, encoding="ascii")
    print("Release archives and SHA256SUMS.txt are in dist/.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=(ROOT / "VERSION").read_text().strip())
    parser.add_argument("--package-only", action="store_true", help="Repackage an already tested executable without rebuilding.")
    args = parser.parse_args()
    if not args.package_only:
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "-m", "unittest", "middleware.desktop.test_desktop", "middleware.tests.test_desktop_setup", "middleware.desktop.test_setup_wizard", "-v"], cwd=ROOT, check=True)
        subprocess.run([sys.executable, "middleware/desktop/build_executable.py"], cwd=ROOT, check=True)
    package(args.version)


if __name__ == "__main__":
    main()
