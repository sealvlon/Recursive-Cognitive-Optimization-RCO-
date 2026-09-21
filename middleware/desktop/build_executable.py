"""Build the windowless Windows app from this repository."""
from pathlib import Path
import os
import json
import struct
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]


def main():
    if sys.platform != "win32":
        raise RuntimeError("Build the Windows executable on Windows.")
    build = ROOT / ".build" / uuid.uuid4().hex[:12]
    env = os.environ.copy()
    env["RCO_SOURCE_ROOT"] = str(ROOT)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT), env.get("PYTHONPATH")]))
    sys.path.insert(0, str(ROOT))
    from scripts.build_release import source_hashes, sha256
    before = source_hashes()
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
               "--windowed", "--name", "RCO Middleware", "--distpath", str(ROOT / "dist"),
               "--workpath", str(build), "--specpath", str(build), "--paths", str(ROOT),
               "--collect-submodules", "uvicorn", "--collect-submodules", "rco",
               "--collect-submodules", "rco_skills", "--collect-data", "rco",
               "--copy-metadata", "mcp", "--copy-metadata", "mcp-types",
               "--hidden-import", "win32api", "--hidden-import", "win32con",
               "--hidden-import", "win32security", "--hidden-import", "pywintypes",
               str(ROOT / "middleware/desktop/launcher.py")]
    subprocess.run(command, cwd=ROOT, env=env, check=True)
    data = (ROOT / "dist/RCO Middleware.exe").read_bytes()
    pe = struct.unpack_from("<I", data, 0x3c)[0]
    if struct.unpack_from("<H", data, pe + 24 + 68)[0] != 2:
        raise RuntimeError("Build did not produce a windowless executable.")
    if before != source_hashes():
        raise RuntimeError("Source changed during the build; rebuild before packaging.")
    (ROOT / "dist/BUILD-INFO.json").write_text(json.dumps({
        "python": sys.version.split()[0], "platform": "windows-x64", "windowless": True,
        "executable_sha256": sha256(data), "source_sha256": before,
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
