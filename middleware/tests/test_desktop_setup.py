import json
from pathlib import Path
import tempfile
import unittest

from middleware.rco_bridge.desktop_setup import desktop_connection_metadata, install_desktop_skills


class DesktopSetupTests(unittest.TestCase):
    def test_upgrade_backs_up_skills_preserves_metadata_and_auth_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            codex = home / ".agents/skills/middleware"
            claude = home / ".claude/skills/middleware"
            for target in (codex, claude):
                target.mkdir(parents=True)
                (target / "SKILL.md").write_text("old registration skill", encoding="utf-8")
            (codex / "agents").mkdir()
            metadata = "interface:\n  display_name: My connection\npolicy:\n  allow_implicit_invocation: false\n"
            (codex / "agents/openai.yaml").write_text(metadata, encoding="utf-8")
            auth = home / ".claude.json"
            auth.write_text('{"unchanged":"private"}', encoding="utf-8")
            first = install_desktop_skills(user_home=home)
            self.assertEqual(len(first["updated"]), 2)
            self.assertEqual((Path(first["backup"]) / "claude/SKILL.md").read_text(), "old registration skill")
            self.assertEqual((codex / "agents/openai.yaml").read_text(), metadata)
            self.assertEqual(auth.read_text(), '{"unchanged":"private"}')
            second = install_desktop_skills(user_home=home)
            self.assertEqual(second["updated"], [])
            self.assertIsNone(second["backup"])

    def test_metadata_does_not_report_tokens_and_is_not_runtime_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            project = home / "project"
            project.mkdir()
            (home / ".codex").mkdir()
            (home / ".codex/config.toml").write_text(
                '[mcp_servers.rco]\nurl="http://127.0.0.1:8799/mcp"\nbearer_token_env_var="RCO_TOKEN_O"\n'
                '[mcp_servers.rco.http_headers]\nAuthorization="codex-secret-value"\n', encoding="utf-8")
            alternate_project = str(project).replace("\\", "/")
            (home / ".claude.json").write_text(json.dumps({"projects": {str(project): {}, alternate_project: {"mcpServers": {"rco": {
                "type": "http", "url": "http://127.0.0.1:8799/mcp", "headers": {"Authorization": "claude-secret-value"}
            }}}}}), encoding="utf-8")
            report = desktop_connection_metadata(project, user_home=home)
            self.assertTrue(report["clients"]["codex"]["configured"])
            self.assertTrue(report["clients"]["claude"]["configured"])
            self.assertFalse(report["runtime_verified"])
            self.assertNotIn("secret-value", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
