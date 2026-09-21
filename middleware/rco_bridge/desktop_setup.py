"""Install native desktop activation skills without touching auth or permission settings."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
from typing import Any


def bundled_skill_root() -> Path:
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "desktop_skills"
    return Path(__file__).resolve().parent.parent / "desktop_skills"


def install_desktop_skills(
    source_root: Path | None = None, user_home: Path | None = None,
) -> dict[str, Any]:
    """Idempotent, backed-up install of skill files only; returns non-secret paths."""
    sources = Path(source_root) if source_root else bundled_skill_root()
    home = Path(user_home) if user_home else Path.home()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_root = home / ".rco" / "backups" / "desktop-skills" / stamp
    report: dict[str, Any] = {"updated": [], "unchanged": [], "preserved": [], "backup": None}
    mappings = (
        ("codex", home / ".agents" / "skills" / "middleware"),
        ("claude", home / ".claude" / "skills" / "middleware"),
    )
    for host, destination in mappings:
        source = sources / host / "middleware"
        if not (source / "SKILL.md").is_file():
            raise FileNotFoundError(f"Bundled {host} activation skill is missing: {source}")
        for original in sorted(source.rglob("*")):
            if not original.is_file():
                continue
            relative = original.relative_to(source)
            target = destination / relative
            data = original.read_bytes()
            if target.is_file() and target.read_bytes() == data:
                report["unchanged"].append(str(target))
                continue
            if target.is_file() and relative == Path("agents/openai.yaml"):
                # Existing UI metadata and invocation policy belong to the user's host setup.
                report["preserved"].append(str(target))
                continue
            if target.exists():
                backup = backup_root / host / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                report["backup"] = str(backup_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write next to the destination then atomically replace it, preserving unrelated files.
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".middleware-", delete=False) as stream:
                staging = Path(stream.name)
                stream.write(data)
            try:
                staging.replace(target)
            finally:
                staging.unlink(missing_ok=True)
            report["updated"].append(str(target))
    return report


def desktop_connection_metadata(project: Path, user_home: Path | None = None) -> dict[str, Any]:
    """Report existing RCO locations and configured field names, never auth values."""
    home = Path(user_home) if user_home else Path.home()
    expected_url = "http://127.0.0.1:8799/mcp"
    report: dict[str, Any] = {"expected_url": expected_url, "clients": {}, "errors": []}
    try:
        config = tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8-sig"))
        server = config.get("mcp_servers", {}).get("rco", {})
        report["clients"]["codex"] = {
            "configured": server.get("url") == expected_url and server.get("enabled", True),
            "url": server.get("url"), "auth_env_name": server.get("bearer_token_env_var"),
        }
    except (OSError, ValueError) as exc:
        report["errors"].append({"client": "codex", "error_type": type(exc).__name__})
    try:
        config = json.loads((home / ".claude.json").read_text(encoding="utf-8-sig"))
        server = config.get("mcpServers", {}).get("rco", {})
        scope = "user"
        target = str(project.resolve()).replace("\\", "/").casefold().rstrip("/")
        for path, value in config.get("projects", {}).items():
            if path.replace("\\", "/").casefold().rstrip("/") == target:
                local = value.get("mcpServers", {}).get("rco")
                if local:
                    server, scope = local, "project"
        report["clients"]["claude"] = {
            "configured": server.get("url") == expected_url,
            "url": server.get("url"), "scope": scope,
            "auth_header_present": bool(server.get("headers", {}).get("Authorization")),
        }
    except (OSError, ValueError) as exc:
        report["errors"].append({"client": "claude", "error_type": type(exc).__name__})
    # Configuration is not evidence that either app has registered or is actively listening.
    report["runtime_verified"] = False
    return report
