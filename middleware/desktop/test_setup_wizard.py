"""Isolated tests: fake home, fake environment, no native app or real settings writes."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from middleware.desktop.setup_wizard import (
    CODEX_SERVER, CLAUDE_SERVER, ENV_NAMES, SettingsConflict, SetupError,
    _codex_text, configure, inspect_setup,
)


class FakeEnvironment:
    def __init__(self):
        self.values = {}
        self.notifications = 0

    def get(self, name):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def notify(self):
        self.notifications += 1


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.project = self.base / "extracted app"
        self.home.mkdir()
        self.project.mkdir()
        self.environment = FakeEnvironment()
        source = Path(__file__).resolve().parents[1] / "desktop_skills"
        shutil.copytree(source, self.project / "middleware/desktop_skills")

    def configure(self, **kwargs):
        return configure(self.project, self.home, self.environment, **kwargs)

    def inspect(self):
        return inspect_setup(self.project, self.home, self.environment)

    def test_first_run_creates_only_local_connections_and_skills(self):
        before = dict(os.environ)
        self.assertFalse(self.inspect()["ready"])
        self.configure()
        self.assertTrue(self.inspect()["ready"])
        self.assertEqual(os.environ, before)
        self.assertEqual(set(self.environment.values), set(ENV_NAMES))
        self.assertEqual(len(set(self.environment.values.values())), 2)
        self.assertTrue(all(len(v) >= 32 for v in self.environment.values.values()))
        codex = tomllib.loads((self.home / ".codex/config.toml").read_text())
        self.assertEqual(codex["mcp_servers"]["rco"], CODEX_SERVER)
        claude = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(next(iter(claude["projects"].values()))["mcpServers"]["rco"], CLAUDE_SERVER)
        for path in self.home.rglob("*"):
            if path.is_file():
                self.assertFalse(any(value.encode() in path.read_bytes()
                                     for value in self.environment.values.values()))

    def test_existing_settings_preserved_and_exact_backups_created(self):
        codex_path = self.home / ".codex/config.toml"
        codex_path.parent.mkdir()
        original_codex = b'# keep this comment\nmodel = "chosen-model"\n[mcp_servers.other]\ncommand = "other-tool"\n'
        codex_path.write_bytes(original_codex)
        claude_path = self.home / ".claude.json"
        original_claude = b'{"theme":"dark","mcpServers":{"other":{"command":"other-tool"}}}'
        claude_path.write_bytes(original_claude)
        result = self.configure()
        backup = Path(result["backup"])
        locations = json.loads((backup / "restore-locations.json").read_text())
        saved = {Path(target).name: (backup / file).read_bytes() for file, target in locations.items()}
        self.assertEqual(saved["config.toml"], original_codex)
        self.assertEqual(saved[".claude.json"], original_claude)
        codex = tomllib.loads(codex_path.read_text())
        self.assertEqual(codex["model"], "chosen-model")
        self.assertEqual(codex["mcp_servers"]["other"], {"command": "other-tool"})
        self.assertIn("# keep this comment", codex_path.read_text())
        claude = json.loads(claude_path.read_text())
        self.assertEqual(claude["theme"], "dark")
        self.assertEqual(claude["mcpServers"]["other"], {"command": "other-tool"})

    def test_conflicting_server_requires_explicit_replacement(self):
        path = self.home / ".codex/config.toml"
        path.parent.mkdir()
        original = b'[mcp_servers.rco]\nurl = "http://127.0.0.1:9999/mcp"\n'
        path.write_bytes(original)
        self.assertTrue(self.inspect()["conflicts"])
        with self.assertRaises(SettingsConflict):
            self.configure()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.environment.values, {})
        self.configure(replace_conflicts=True)
        self.assertTrue(self.inspect()["ready"])

    def test_repeated_setup_does_not_rotate_keys_or_change_settings(self):
        self.configure()
        keys = self.environment.values.copy()
        contents = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        result = self.configure()
        self.assertEqual(result["settings_updated"], 0)
        self.assertIsNone(result["backup"])
        self.assertEqual(self.environment.values, keys)
        self.assertEqual({p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}, contents)

    def test_invalid_settings_or_existing_keys_never_overwritten(self):
        path = self.home / ".claude.json"
        path.write_text("invalid existing settings")
        with self.assertRaises(SetupError):
            self.configure()
        self.assertEqual(path.read_text(), "invalid existing settings")
        path.unlink()
        self.environment.values[ENV_NAMES[0]] = "too-short"
        with self.assertRaises(SetupError):
            self.configure()
        self.assertEqual(self.environment.values[ENV_NAMES[0]], "too-short")
        self.assertFalse((self.home / ".codex").exists())

    def test_malformed_connection_containers_fail_before_any_write(self):
        path = self.home / ".claude.json"
        for malformed in ({"mcpServers": []}, {"projects": []},
                          {"projects": {"project": None}}, {"mcpServers": {"rco": []}}):
            original = json.dumps(malformed)
            path.write_text(original)
            with self.assertRaises(SetupError):
                self.configure()
            self.assertEqual(path.read_text(), original)
            self.assertFalse((self.home / ".codex/config.toml").exists())
            self.assertEqual(self.environment.values, {})

    def test_install_failure_restores_prior_configs(self):
        path = self.home / ".claude.json"
        original = b'{"theme":"dark"}'
        path.write_bytes(original)
        with patch("middleware.desktop.setup_wizard.install_desktop_skills", side_effect=OSError("test")):
            with self.assertRaises(OSError):
                self.configure()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.home / ".codex/config.toml").exists())
        self.assertEqual(self.environment.values, {})

    def test_project_overrides_replaced_only_with_consent(self):
        codex = self.project / ".codex/config.toml"
        codex.parent.mkdir()
        codex.write_text('[mcp_servers.rco]\ncommand = "other-rco"\n')
        mcp = self.project / ".mcp.json"
        mcp.write_text(json.dumps({"mcpServers": {"rco": {"command": "old"}, "other": {"command": "keep"}}}))
        with self.assertRaises(SettingsConflict):
            self.configure()
        self.configure(replace_conflicts=True)
        self.assertTrue(self.inspect()["ready"])
        self.assertEqual(json.loads(mcp.read_text())["mcpServers"]["other"], {"command": "keep"})

    def test_toml_preserves_unrelated_sections_and_refuses_inline_conflict(self):
        original = '# before\n[mcp_servers."rco"]\nurl = "old"\n[mcp_servers.rco.http_headers]\nx = "old"\n[features]\nkeep = true\n'
        updated = _codex_text(original)
        self.assertEqual(tomllib.loads(updated), {"mcp_servers": {"rco": CODEX_SERVER}, "features": {"keep": True}})
        with self.assertRaises(SetupError):
            _codex_text('[mcp_servers]\nrco = { command = "old" }\n')

    def test_runtime_config_state_is_private_to_extracted_folder(self):
        from middleware.desktop.server import make_config
        path = make_config(self.project)
        doc = tomllib.loads(path.read_text())
        self.assertEqual(Path(doc["paths"]["data_dir"]), self.project / ".rco-desktop/data")
        self.assertEqual(path.parent, self.project / ".rco-desktop")


if __name__ == "__main__":
    unittest.main()
