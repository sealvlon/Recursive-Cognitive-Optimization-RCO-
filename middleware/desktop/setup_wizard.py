"""Visual, backed-up setup for native desktop clients; no model subprocesses.

Only local RCO connection settings and the bundled activation skills are changed.
Tests inject a home directory and environment store; they never touch real settings.
"""
from __future__ import annotations

import ctypes
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import tomllib

from middleware.rco_bridge.desktop_setup import install_desktop_skills

URL = "http://127.0.0.1:8799/mcp"
ENV_NAMES = ("RCO_TOKEN_C", "RCO_TOKEN_O")
CODEX_SERVER = {"url": URL, "bearer_token_env_var": ENV_NAMES[1], "enabled": True,
                "startup_timeout_sec": 30, "tool_timeout_sec": 180}
CLAUDE_SERVER = {"type": "http", "url": URL,
                 "headers": {"Authorization": "Bearer ${RCO_TOKEN_C}"}}


class SetupError(RuntimeError):
    pass


class SettingsConflict(SetupError):
    pass


class WindowsEnvironment:
    """RCO's own local shared keys only; never provider authentication."""
    def get(self, name):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
            if isinstance(value, str) and value:
                return value
        except (ImportError, OSError):
            pass
        return os.environ.get(name)

    def set(self, name, value):
        if os.name != "nt":
            raise SetupError("The packaged desktop setup is available on Windows.")
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        os.environ[name] = value

    def notify(self):
        if os.name == "nt":
            # Applications already running still need a restart to inherit keys.
            from ctypes import wintypes
            api = ctypes.WinDLL("user32", use_last_error=True).SendMessageTimeoutW
            api.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                            wintypes.LPCWSTR, wintypes.UINT, wintypes.UINT,
                            ctypes.POINTER(ctypes.c_size_t)]
            api.restype = wintypes.LPARAM
            result = ctypes.c_size_t()
            api(0xFFFF, 0x001A, 0, "Environment", 2, 1000, ctypes.byref(result))


def _read(path, kind):
    if not path.exists():
        return "", {}
    try:
        text = path.read_text(encoding="utf-8-sig")
        value = tomllib.loads(text) if kind == "toml" else json.loads(text)
        if not isinstance(value, dict):
            raise ValueError()
        _validate_shape(value)
        return text, value
    except (OSError, ValueError, UnicodeError):
        # Parse exceptions can echo secret settings; do not expose their text.
        raise SetupError(f"Cannot read {path.name}. Its existing settings were left unchanged.") from None


def _validate_shape(value):
    """Reject malformed connection containers before staging any write."""
    for name in ("mcp_servers", "mcpServers"):
        if name in value:
            servers = value[name]
            if not isinstance(servers, dict):
                raise ValueError()
            if "rco" in servers:
                server = servers["rco"]
                if not isinstance(server, dict):
                    raise ValueError()
                for header in ("headers", "http_headers", "env_http_headers"):
                    if header in server and not isinstance(server[header], dict):
                        raise ValueError()
    if "projects" in value:
        if not isinstance(value["projects"], dict):
            raise ValueError()
        for project in value["projects"].values():
            if not isinstance(project, dict):
                raise ValueError()
            _validate_shape(project)


def _project_key(projects, workspace):
    expected = str(workspace.resolve()).replace("\\", "/").casefold().rstrip("/")
    return next((key for key in projects if key.replace("\\", "/").casefold().rstrip("/") == expected),
                str(workspace.resolve()).replace("\\", "/"))


def _codex_ok(server):
    return (server.get("url") == URL and server.get("bearer_token_env_var") == ENV_NAMES[1]
            and server.get("enabled", True) is True and not server.get("command")
            and not server.get("http_headers") and not server.get("env_http_headers"))


def _claude_ok(server):
    return server == CLAUDE_SERVER


def _keys(store):
    values = {name: (store.get(name) or "").strip() for name in ENV_NAMES}
    if any(value and len(value) < 32 for value in values.values()):
        raise SetupError("An existing local RCO connection key is invalid. Setup preserved it; repair that RCO connection before continuing.")
    if all(values.values()) and len(set(values.values())) != 2:
        raise SetupError("The two existing local RCO connection keys are identical. Setup preserved them; each app needs its own key.")
    return values


def inspect_setup(workspace, home=None, environment=None):
    workspace, home = Path(workspace), Path(home) if home else Path.home()
    environment = environment or WindowsEnvironment()
    _, codex = _read(home / ".codex/config.toml", "toml")
    _, local = _read(workspace / ".codex/config.toml", "toml")
    _, claude = _read(home / ".claude.json", "json")
    _, mcp = _read(workspace / ".mcp.json", "json")
    codex_server = codex.get("mcp_servers", {}).get("rco", {})
    local_server = local.get("mcp_servers", {}).get("rco", {})
    projects = claude.get("projects", {})
    key = _project_key(projects, workspace)
    claude_server = projects.get(key, {}).get("mcpServers", {}).get("rco", {})
    global_claude = claude.get("mcpServers", {}).get("rco", {})
    project_mcp = mcp.get("mcpServers", {}).get("rco", {})
    conflicts = []
    for label, server, predicate in (
        ("Codex connection", codex_server, _codex_ok),
        ("Codex project connection", local_server, _codex_ok),
        ("Claude project connection", claude_server, _claude_ok),
        ("Claude shared project connection", project_mcp, _claude_ok),
    ):
        if server and not predicate(server):
            conflicts.append(label)
    # A new project-scoped entry overrides this existing global entry, so disclose it.
    if not claude_server and global_claude and not _claude_ok(global_claude):
        conflicts.append("Claude connection for this project")
    values = _keys(environment)
    skill_ready = True
    for host, destination in (("codex", home / ".agents/skills/middleware"),
                              ("claude", home / ".claude/skills/middleware")):
        source = workspace / "middleware/desktop_skills" / host / "middleware/SKILL.md"
        installed = destination / "SKILL.md"
        if not source.is_file():
            raise SetupError("The application folder is incomplete. Extract the entire download again.")
        skill_ready = skill_ready and installed.is_file() and installed.read_bytes() == source.read_bytes()
    ready = (not conflicts and _codex_ok(local_server or codex_server)
             and _claude_ok(claude_server or global_claude or project_mcp)
             and all(values.values()) and skill_ready)
    return {"ready": bool(ready), "conflicts": conflicts}


def _table_path(header):
    try:
        node = tomllib.loads(header + "\n__rco_probe__ = true\n")
        path = []
        while "__rco_probe__" not in node:
            if len(node) != 1:
                return None
            key, node = next(iter(node.items()))
            path.append(key)
            if not isinstance(node, dict):
                return None
        return tuple(path)
    except (ValueError, TypeError):
        return None


def _codex_text(text):
    """Preserve all unrelated TOML byte-for-byte, including comments and ordering.

    Unusual inline/dotted RCO declarations are refused rather than risking unrelated
    settings. Parse and compare the entire resulting document before it is saved.
    """
    original = tomllib.loads(text)
    target = ("mcp_servers", "rco")
    result, skipping = [], False
    for line in text.splitlines(keepends=True):
        if re.match(r"^\s*\[", line):
            path = _table_path(line.strip())
            skipping = bool(path and path[:2] == target)
        if not skipping:
            result.append(line)
    candidate = "".join(result).rstrip() + "\n\n[mcp_servers.rco]\n"
    for name, value in CODEX_SERVER.items():
        candidate += f"{name} = {json.dumps(value)}\n"
    expected = copy.deepcopy(original)
    expected.setdefault("mcp_servers", {})["rco"] = CODEX_SERVER
    try:
        if tomllib.loads(candidate) != expected:
            raise ValueError()
    except ValueError:
        raise SetupError("Codex uses an unusual RCO settings layout. Setup left it unchanged; use a standard [mcp_servers.rco] section before retrying.") from None
    return candidate


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".rco-setup-", delete=False) as stream:
        temp = Path(stream.name)
        stream.write(data)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def configure(workspace, home=None, environment=None, replace_conflicts=False):
    """Called only by the user's Configure button (or an isolated test)."""
    workspace, home = Path(workspace).resolve(), Path(home) if home else Path.home()
    environment = environment or WindowsEnvironment()
    report = inspect_setup(workspace, home, environment)
    if report["conflicts"] and not replace_conflicts:
        raise SettingsConflict("A different RCO connection already exists. Choose the replacement button to back it up and use this application.")
    changes = {}
    codex_path = home / ".codex/config.toml"
    text, doc = _read(codex_path, "toml")
    if not _codex_ok(doc.get("mcp_servers", {}).get("rco", {})):
        changes[codex_path] = _codex_text(text).encode("utf-8")
    local_path = workspace / ".codex/config.toml"
    text, doc = _read(local_path, "toml")
    local_server = doc.get("mcp_servers", {}).get("rco")
    if local_server and not _codex_ok(local_server):
        changes[local_path] = _codex_text(text).encode("utf-8")
    claude_path = home / ".claude.json"
    _, claude = _read(claude_path, "json")
    projects = claude.setdefault("projects", {})
    key = _project_key(projects, workspace)
    servers = projects.setdefault(key, {}).setdefault("mcpServers", {})
    if not _claude_ok(servers.get("rco", {})):
        servers["rco"] = CLAUDE_SERVER
        changes[claude_path] = (json.dumps(claude, indent=2) + "\n").encode("utf-8")
    mcp_path = workspace / ".mcp.json"
    _, mcp = _read(mcp_path, "json")
    server = mcp.get("mcpServers", {}).get("rco")
    if server and not _claude_ok(server):
        mcp["mcpServers"]["rco"] = CLAUDE_SERVER
        changes[mcp_path] = (json.dumps(mcp, indent=2) + "\n").encode("utf-8")
    values = _keys(environment)
    for name, value in values.items():
        if not value:
            values[name] = secrets.token_urlsafe(48)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / ".rco/backups/desktop-setup" / stamp
    originals = {path: path.read_bytes() if path.exists() else None for path in changes}
    for index, (path, data) in enumerate(originals.items()):
        if data is not None:
            backup.mkdir(parents=True, exist_ok=True)
            (backup / f"{index}-{path.name}").write_bytes(data)
    if backup.exists():
        (backup / "restore-locations.json").write_text(json.dumps(
            {f"{i}-{path.name}": str(path) for i, (path, data) in enumerate(originals.items()) if data is not None},
            indent=2), encoding="utf-8")
    written = []
    try:
        for path, data in changes.items():
            _atomic(path, data)
            written.append(path)
        install_desktop_skills(workspace / "middleware/desktop_skills", home)
        for name, value in values.items():
            # Never replace an existing connection key.
            if not environment.get(name):
                environment.set(name, value)
        environment.notify()
    except Exception:
        for path in reversed(written):
            old = originals[path]
            if old is None:
                path.unlink(missing_ok=True)
            else:
                _atomic(path, old)
        raise
    return {"backup": str(backup) if backup.exists() else None,
            "settings_updated": len(changes), "restart_apps": True}


def ensure_setup(workspace):
    """Return False when the user closes/cancels the visual first-run setup."""
    workspace = Path(workspace)
    environment = WindowsEnvironment()
    report = inspect_setup(workspace, environment=environment)
    if not report["ready"]:
        import tkinter as tk
        from tkinter import messagebox, ttk
        root = tk.Tk()
        root.title("Welcome to RCO Middleware")
        root.geometry("650x470")
        root.minsize(610, 450)
        root.configure(background="#f4f7fc")
        style = ttk.Style(root)
        style.configure("TFrame", background="#f4f7fc")
        style.configure("TLabel", background="#f4f7fc", font=("Segoe UI", 11))
        style.configure("Heading.TLabel", font=("Segoe UI", 20, "bold"))
        frame = ttk.Frame(root, padding=28)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Connect your desktop apps", style="Heading.TLabel").pack(anchor="w")
        copy = ("RCO coordinates your existing Codex and Claude Code desktop sessions.\n\n"
                "Configure adds this computer's private local connection keys, the RCO connection in each app, "
                "and the two activation skills. Existing files are backed up; unrelated settings are preserved.\n\n"
                "After setup, restart both desktop apps and open this extracted folder in each app. "
                "The dashboard will show the short connection message to send in each session. "
                "You stay signed in to your existing accounts.")
        ttk.Label(frame, text=copy, wraplength=585, justify="left").pack(anchor="w", pady=(20, 12))
        if report["conflicts"]:
            ttk.Label(frame, text="An RCO connection is already configured. Continuing will back it up and replace "
                      "only the RCO entries needed for this application.", wraplength=585,
                      foreground="#9a4a12").pack(anchor="w", pady=(0, 12))
        complete = [False]
        def install():
            try:
                result = configure(workspace, environment=environment,
                                   replace_conflicts=bool(report["conflicts"]))
                note = "Setup is complete. Restart Codex and Claude Code, then open this extracted folder in each app."
                if result["backup"]:
                    note += "\n\nYour previous connection settings were backed up to:\n" + result["backup"]
                messagebox.showinfo("Ready to connect", note, parent=root)
                complete[0] = True
                root.destroy()
            except Exception as exc:
                text = str(exc) if isinstance(exc, SetupError) else "Setup could not finish. Check that your settings files are writable, then try again."
                messagebox.showerror("Setup needs attention", text, parent=root)
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(15, 0))
        ttk.Button(buttons, text="Cancel", command=root.destroy).pack(side="right")
        ttk.Button(buttons, text="Back up and use this app" if report["conflicts"] else "Configure desktop apps",
                   command=install).pack(side="right", padx=(0, 12))
        root.mainloop()
        if not complete[0]:
            return False
    # Override stale inherited process values with the persistent local values.
    for name, value in _keys(environment).items():
        os.environ[name] = value
    return True
