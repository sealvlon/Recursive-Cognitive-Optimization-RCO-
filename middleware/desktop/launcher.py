"""Windowless entry point for the user's existing desktop-app RCO hub."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback
import urllib.error
import urllib.request


PANEL_BASE = "http://127.0.0.1:8798"


def workspace_path() -> Path:
    override = os.environ.get("RCO_DESKTOP_WORKSPACE")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _json_get(path: str) -> dict | None:
    # A local application must not send its requests through a configured proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(PANEL_BASE + path, timeout=0.8) as response:
            result = json.loads(response.read(65536))
        return result if isinstance(result, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def reopen_existing(workspace: Path) -> bool:
    health = _json_get("/api/health")
    if not health or health.get("application") != "rco-desktop":
        return False
    if Path(str(health.get("workspace", ""))).resolve() != workspace:
        raise RuntimeError("RCO is already open for a different project. Close that RCO window before opening this project.")
    response = _json_get("/api/open")
    if response is None or response.get("opened") is not True:
        raise RuntimeError("RCO is running, but its dashboard could not be opened. Please try double-clicking again.")
    return True


class InstanceLock:
    """A Windows kernel mutex covers startup as well as the running service."""

    def __init__(self, workspace: Path):
        self.handle = None
        self.owned = True
        self.api = None
        if os.name != "nt":
            return
        from ctypes import wintypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        identity = hashlib.sha256(str(workspace).casefold().encode("utf-8")).hexdigest()[:24]
        self.handle = self.api.CreateMutexW(None, True, "Local\\RCO-Desktop-" + identity)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.owned = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS

    def close(self):
        if self.handle:
            if self.owned:
                self.api.ReleaseMutex(self.handle)
            self.api.CloseHandle(self.handle)
            self.handle = None


class PrivateLog:
    """Keep launch diagnostics, without persisting the browser's access token."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 2_000_000:
            path.replace(path.with_suffix(".previous.log"))
        self.stream = path.open("a", encoding="utf-8", buffering=1)
        self.encoding = "utf-8"

    def write(self, value: str):
        value = re.sub(r"([?&]t=)[A-Za-z0-9_\-]+", r"\1[private]", str(value))
        return self.stream.write(value)

    def flush(self):
        self.stream.flush()

    def isatty(self):
        return False


def show_error(message: str, log_path: Path):
    text = f"{message}\n\nDetails were saved here:\n{log_path}"
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showerror("RCO Middleware", text, parent=root)
        root.destroy()
    except Exception:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(None, text, "RCO Middleware", 0x10)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-package":
        from middleware.desktop.package_check import run
        return run(Path(sys.argv[2]))
    workspace = workspace_path()
    log_path = workspace / ".rco-desktop" / "desktop-launcher.log"
    instance = None
    try:
        if reopen_existing(workspace):
            return 0
        instance = InstanceLock(workspace)
        if not instance.owned:
            # A simultaneous click can arrive while the first copy is unpacking.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if reopen_existing(workspace):
                    return 0
                time.sleep(0.2)
            raise RuntimeError("RCO is still starting. Please wait a moment, then double-click again.")
        os.environ["RCO_DESKTOP_WORKSPACE"] = str(workspace)
        os.environ["RCO_SOURCE_ROOT"] = str(workspace)
        os.chdir(workspace)
        if not getattr(sys, "frozen", False):
            sys.path.insert(0, str(workspace))
        log = PrivateLog(log_path)
        sys.stdout = sys.stderr = log
        if sys.stdin is None:
            sys.stdin = open(os.devnull, "r", encoding="utf-8")
        print(f"\nRCO desktop starting {time.strftime('%Y-%m-%d %H:%M:%S')}")
        from middleware.desktop.setup_wizard import ensure_setup
        if not ensure_setup(workspace):
            return 0
        from middleware.desktop.server import run
        result = run()
        if result not in (None, 0):
            raise RuntimeError("RCO could not start. Its connection details are in the launch log.")
        return 0
    except Exception as exc:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as error_log:
                traceback.print_exc(file=error_log)
        finally:
            show_error(str(exc), log_path)
        return 1
    finally:
        if instance:
            instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
