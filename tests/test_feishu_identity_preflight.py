import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_feishu_cli_identity.py"
SPEC = importlib.util.spec_from_file_location("verify_feishu_cli_identity", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class FeishuIdentityPreflightTest(unittest.TestCase):
    def test_isolated_environment_removes_agent_workspace_selectors(self):
        env = MODULE.isolated_lark_env({
            "HERMES_HOME": "wrong",
            "OPENCLAW_HOME": "wrong",
            "LARK_CHANNEL": "wrong",
            "KEEP_ME": "yes",
        })

        self.assertNotIn("HERMES_HOME", env)
        self.assertNotIn("OPENCLAW_HOME", env)
        self.assertNotIn("LARK_CHANNEL", env)
        self.assertEqual(env["KEEP_ME"], "yes")
        self.assertTrue(env["LARKSUITE_CLI_CONFIG_DIR"].endswith(".lark-cli"))

    def test_scoped_command_pins_configured_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "pipeline.json"
            config.write_text(json.dumps({
                "feishu": {"profile": "cli_project"}
            }), encoding="utf-8")

            command = MODULE.scoped_lark_command(
                "lark-cli", ["base", "+record-list"], config_path=config,
            )

        self.assertEqual(command[-2:], ["--profile", "cli_project"])

    @patch.object(MODULE.subprocess, "run")
    def test_user_identity_passes_for_expected_app(self, run: Mock):
        run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "appId": "cli_project",
                "identities": {
                    "bot": {
                        "available": True,
                        "verified": True,
                        "appName": "Project App",
                    },
                    "user": {"available": True, "verified": True, "status": "ready"},
                },
            }),
            stderr="",
        )

        result = MODULE.verify_feishu_identity({
            "lark_cli": "tools/lark-cli/lark-cli.exe",
            "profile": "cli_project",
            "expected_app_id": "cli_project",
            "expected_app_name": "Project App",
            "as_identity": "user",
        }, env={"HERMES_HOME": "wrong"})

        self.assertEqual(result["identity"], "user")
        called_env = run.call_args.kwargs["env"]
        self.assertNotIn("HERMES_HOME", called_env)
        self.assertIn("--profile", run.call_args.args[0])

    @patch.object(MODULE.subprocess, "run")
    def test_mismatched_app_is_rejected_before_pipeline_writes(self, run: Mock):
        run.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "appId": "cli_wrong",
                "identities": {
                    "bot": {
                        "available": True,
                        "verified": True,
                        "appName": "Wrong Agent",
                    },
                    "user": {"available": True, "verified": True, "status": "ready"},
                },
            }),
            stderr="",
        )

        with self.assertRaisesRegex(MODULE.FeishuIdentityError, "Unexpected Feishu App ID"):
            MODULE.verify_feishu_identity({
                "lark_cli": "lark-cli",
                "profile": "cli_project",
                "expected_app_id": "cli_project",
                "expected_app_name": "Project App",
                "as_identity": "user",
            })

    def test_main_reports_safe_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "pipeline.json"
            config.write_text(json.dumps({
                "feishu": {
                    "lark_cli": "lark-cli",
                    "profile": "cli_project",
                    "expected_app_id": "cli_project",
                    "expected_app_name": "Project App",
                    "as_identity": "user",
                }
            }), encoding="utf-8")
            with patch.object(
                MODULE,
                "verify_feishu_identity",
                return_value={
                    "app_id": "cli_project",
                    "app_name": "Project App",
                    "profile": "cli_project",
                    "identity": "user",
                },
            ):
                self.assertEqual(MODULE.main(["--config", str(config)]), 0)


if __name__ == "__main__":
    unittest.main()
