"""Bounded bridge for the existing RCO turn engine, not a replacement hub."""

from pathlib import Path
import os
import sys

_package_root = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                 else Path(__file__).resolve().parents[2])
RCO_ROOT = Path(os.environ.get("RCO_SOURCE_ROOT", str(_package_root))).resolve()
if not (RCO_ROOT / "rco" / "turns.py").is_file():
    raise RuntimeError("The RCO application folder is incomplete. Extract the entire download before opening it.")
sys.path.insert(0, str(RCO_ROOT))
