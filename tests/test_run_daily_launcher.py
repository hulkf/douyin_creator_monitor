import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
RUN_DAILY = PROJECT_DIR / "run_daily.bat"
RUN_SCHEDULED = PROJECT_DIR / "run_scheduled.bat"
REGISTER_TASK = PROJECT_DIR / "scripts" / "register_scheduled_task.ps1"


@unittest.skipUnless(os.name == "nt", "run_daily.bat is Windows-specific")
class RunDailyLauncherTest(unittest.TestCase):
    def parse_args(self, *args: str) -> tuple[str, str]:
        source_lines = RUN_DAILY.read_text(encoding="ascii").splitlines()
        parser_lines = [
            line for line in source_lines
            if line.startswith('set "RAW=') or line.startswith('set "PYARGS=')
        ]
        self.assertEqual(len(parser_lines), 2)
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "probe.bat"
            probe.write_text(
                "@echo off\r\n"
                + "\r\n".join(parser_lines)
                + "\r\necho RAW=[%RAW%]\r\necho PYARGS=[%PYARGS%]\r\n",
                encoding="ascii",
            )
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(probe), *args],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        values = {}
        for line in result.stdout.splitlines():
            key, value = line.split("=", 1)
            values[key] = value[1:-1]
        return values["RAW"], values["PYARGS"]

    def test_no_arguments_do_not_create_a_fake_no_pause_argument(self):
        _, forwarded = self.parse_args()
        self.assertEqual(forwarded.strip(), "")

    def test_launcher_flag_is_consumed_and_pipeline_flags_keep_separator(self):
        _, forwarded = self.parse_args("--no-pause", "--creator", "zhiliao")
        self.assertEqual(forwarded, " --creator zhiliao")

    def test_feishu_agent_environment_is_cleared_before_identity_preflight(self):
        source = RUN_DAILY.read_text(encoding="ascii")

        preflight = source.index("verify_feishu_cli_identity.py")
        reconcile = source.index("check_and_onboard_new_creators.py")
        self.assertLess(source.index('set "HERMES_HOME="'), preflight)
        self.assertLess(source.index('set "OPENCLAW_HOME="'), preflight)
        self.assertLess(source.index('set "LARK_CHANNEL="'), preflight)
        self.assertLess(preflight, reconcile)
        self.assertIn("exit /b %FEISHU_PREFLIGHT_EL%", source)

    def test_scheduled_task_catches_up_after_a_missed_start(self):
        source = REGISTER_TASK.read_text(encoding="utf-8-sig")
        self.assertIn("-StartWhenAvailable", source)

    def test_scheduled_launcher_rotates_logs_before_appending(self):
        source = RUN_SCHEDULED.read_text(encoding="utf-8-sig")
        self.assertIn("rotate_runtime_logs.py", source)
        self.assertLess(source.index("rotate_runtime_logs.py"), source.index("task-launch.log"))


if __name__ == "__main__":
    unittest.main()
