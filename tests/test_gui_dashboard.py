import importlib.util
import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gui_dashboard.py"
SPEC = importlib.util.spec_from_file_location("gui_dashboard", MODULE_PATH)
GUI = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(GUI)


class DashboardDataTest(unittest.TestCase):
    def test_build_snapshot_reads_task_run_creator_and_account_pool_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "local"
            runs_dir = root / "runtime" / "pipeline" / "runs"
            logs_dir = root / "logs"
            media_crawler = root / "MediaCrawler"
            config_dir.mkdir(parents=True)
            runs_dir.mkdir(parents=True)
            logs_dir.mkdir(parents=True)

            works_a = root / "runtime" / "a-works.json"
            works_a.parent.mkdir(parents=True, exist_ok=True)
            works_a.write_text(
                json.dumps({
                    "works": [
                        {"aweme_id": "2", "create_time": 2},
                        {"aweme_id": "1", "create_time": 1},
                    ],
                    "pending_aweme_ids": ["2"],
                }),
                encoding="utf-8",
            )
            config = {
                "state_dir": "runtime/pipeline",
                "log_dir": "logs",
                "collection": {
                    "media_crawler_dir": str(media_crawler),
                    "account_profiles": ["account-a", "account-b"],
                },
                "creators": [
                    {
                        "key": "a",
                        "creator_name": "达人 A",
                        "works_file": str(works_a),
                        "account_profiles": ["account-c"],
                    },
                    {
                        "key": "b",
                        "creator_name": "达人 B",
                        "works_file": "runtime/b-works.json",
                    },
                ],
            }
            (config_dir / "pipeline.json").write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )
            for profile, size in (
                ("account-a", 40_000),
                ("account-b", 1_000),
                ("account-c", 40_000),
            ):
                cookie_file = (
                    media_crawler
                    / "browser_data"
                    / f"cdp_{profile}_dy_user_data_dir"
                    / "Default"
                    / "Network"
                    / "Cookies"
                )
                cookie_file.parent.mkdir(parents=True, exist_ok=True)
                cookie_file.write_bytes(b"x" * size)

            run_summary = {
                "run_id": "20260724-200000",
                "started_at": "2026-07-24T20:00:00+08:00",
                "finished_at": "2026-07-24T20:02:30+08:00",
                "wall_seconds": 150.0,
                "status": "partial_failure",
                "creators": [
                    {"key": "a", "status": "success", "selected_count": 1},
                    {
                        "key": "b",
                        "status": "partial_failure",
                        "collection_error": "account blocked",
                        "account_pool_attempts": [
                            {"profile_key": "account-a", "status": "blocked"},
                        ],
                    },
                ],
            }
            (runs_dir / "20260724-200000.json").write_text(
                json.dumps(run_summary, ensure_ascii=False),
                encoding="utf-8",
            )
            log_file = logs_dir / "pipeline-20260724-200000.log"
            log_file.write_text("[2026-07-24 20:02:30] 流水线结束，状态=partial_failure\n", encoding="utf-8")

            task = GUI.TaskInfo(
                state="Ready",
                last_run_time=datetime(2026, 7, 24, 20, 0, 0),
                next_run_time=datetime(2026, 7, 25, 3, 0, 0),
                last_result=1,
            )
            snapshot = GUI.build_dashboard_snapshot(
                root,
                task_provider=lambda _name: task,
            )

            self.assertEqual(snapshot.total_creators, 2)
            self.assertEqual(snapshot.total_works, 2)
            self.assertEqual(snapshot.pending_works, 1)
            self.assertEqual(snapshot.latest_run.status, "partial_failure")
            self.assertEqual(snapshot.latest_run.wall_seconds, 150.0)
            self.assertEqual(snapshot.account_profiles_total, 3)
            self.assertEqual(snapshot.account_profiles_detected, 2)
            self.assertEqual(snapshot.latest_log, log_file)
            self.assertEqual(snapshot.creators[0].status, "success")
            self.assertEqual(snapshot.creators[0].works_count, 2)
            self.assertEqual(snapshot.creators[1].status, "partial_failure")
            self.assertEqual(
                snapshot.creators[1].detail,
                "抖音账号被风控（account blocked）",
            )

    def test_summarize_error_turns_long_known_failures_into_readable_labels(self):
        self.assertEqual(
            GUI.summarize_error("Traceback: account blocked after repeated requests"),
            "抖音账号被风控（account blocked）",
        )
        self.assertEqual(
            GUI.summarize_error("Lark error: Hermes context detected, NOT_CONFIGURED"),
            "飞书 CLI 未绑定当前运行身份",
        )
        self.assertEqual(GUI.summarize_error("x" * 120), ("x" * 89) + "…")

    def test_start_scheduled_task_uses_windows_task_scheduler(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='SUCCESS: Attempted to run the scheduled task "DouyinCreatorMonitor".',
            stderr="",
        )
        runner = Mock(return_value=completed)

        message = GUI.start_scheduled_task("DouyinCreatorMonitor", runner=runner)

        self.assertIn("SUCCESS", message)
        command = runner.call_args.args[0]
        self.assertEqual(
            command,
            ["schtasks", "/run", "/tn", "DouyinCreatorMonitor"],
        )
        self.assertEqual(runner.call_args.kwargs["cwd"], GUI.PROJECT_DIR)

    def test_start_scheduled_task_reports_scheduler_failure(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="ERROR: The system cannot find the file specified.",
        )

        with self.assertRaisesRegex(GUI.DashboardError, "cannot find"):
            GUI.start_scheduled_task(
                "DouyinCreatorMonitor",
                runner=Mock(return_value=completed),
            )


if __name__ == "__main__":
    unittest.main()
