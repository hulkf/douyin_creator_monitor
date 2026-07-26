import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "web_dashboard.py"
SPEC = importlib.util.spec_from_file_location("web_dashboard", MODULE_PATH)
WEB = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(WEB)


def valid_creator(key="creator-a"):
    return {
        "key": key,
        "enabled": True,
        "creator_url": f"https://www.douyin.com/user/{key}",
        "creator_name": f"达人 {key}",
        "creator_dir_name": key,
        "works_table_id": f"table-{key}",
        "works_file": f"runtime/{key}-works.json",
    }


def valid_config(*, creators=None, max_works=0):
    return {
        "max_works": max_works,
        "feishu": {
            "creator_table_id": "creator-table",
            "work_id_field": "抖音作品ID",
        },
        "creators": list(creators or []),
    }


class WebDashboardConfigTests(unittest.TestCase):
    def test_load_config_merges_template_defaults_without_replacing_creators(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "local").mkdir()
            template = {
                "max_works": 0,
                "collection": {"incremental_enabled": True, "max_count": 200},
                "creators": [{"key": "example"}],
            }
            local = {
                "collection": {"max_count": 50},
                "creators": [{"key": "real", "enabled": True}],
            }
            (root / "config" / "pipeline.example.json").write_text(
                json.dumps(template), encoding="utf-8"
            )
            (root / "local" / "pipeline.json").write_text(
                json.dumps(local), encoding="utf-8"
            )

            loaded = WEB.load_pipeline_config(root)

            self.assertTrue(loaded.exists)
            self.assertEqual(loaded.config["max_works"], 0)
            self.assertTrue(loaded.config["collection"]["incremental_enabled"])
            self.assertEqual(loaded.config["collection"]["max_count"], 50)
            self.assertEqual(loaded.config["creators"], local["creators"])

    def test_validate_config_reports_duplicate_creator_keys_and_bad_workers(self):
        config = valid_config(creators=[valid_creator("same"), valid_creator("same")])
        config.update({
            "collection": {"profile_max_workers": 0},
        })

        errors = WEB.validate_pipeline_config(config)

        self.assertTrue(any("profile_max_workers" in error for error in errors))
        self.assertTrue(any("重复" in error for error in errors))

    def test_validate_config_enforces_data_protection_and_required_creator_fields(self):
        config = valid_config(creators=[valid_creator()])
        config["feishu"]["work_id_field"] = "标题"
        config["creators"][0]["works_table_id"] = ""

        errors = WEB.validate_pipeline_config(config)

        self.assertTrue(any("必须是 抖音作品ID" in error for error in errors))
        self.assertTrue(any("works_table_id" in error for error in errors))

    def test_validate_config_allows_fractional_profile_cache_hours(self):
        config = valid_config()
        config["collection"] = {"profile_ttl_hours": 0.5}

        self.assertEqual(WEB.validate_pipeline_config(config), [])

    def test_save_config_is_atomic_and_keeps_one_recoverable_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "local").mkdir()
            path = root / "local" / "pipeline.json"
            path.write_text('{"version": 1}', encoding="utf-8")

            result = WEB.save_pipeline_config(
                {"version": 2, **valid_config(creators=[valid_creator()])}, root
            )

            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 2)
            backup = root / "local" / "pipeline.backup.json"
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["version"], 1)
            self.assertEqual(result.path, path)
            self.assertEqual(result.backup_path, backup)

    def test_account_pool_status_detects_persisted_profile_cookie(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "local").mkdir()
            config = valid_config()
            config["collection"] = {
                "account_profiles": ["account-a", "account-b"],
                "media_crawler_dir": "MediaCrawler",
            }
            (root / "config" / "pipeline.example.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            (root / "local" / "pipeline.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            cookie = (
                root
                / "MediaCrawler"
                / "browser_data"
                / "cdp_account-a_dy_user_data_dir"
                / "Default"
                / "Network"
                / "Cookies"
            )
            cookie.parent.mkdir(parents=True)
            cookie.write_bytes(b"x" * (WEB.VALID_LOGIN_MIN_BYTES + 1))

            payload = WEB.account_pool_payload(root)

            self.assertEqual(payload["profiles"][0]["status"], "ready")
            self.assertEqual(payload["profiles"][1]["status"], "missing")

    def test_account_login_command_is_isolated_to_selected_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime").mkdir()
            config = valid_config()
            config["collection"] = {
                "account_profiles": ["account-a", "account-b"],
                "media_crawler_dir": "D:/MediaCrawler",
                "media_crawler_python": "D:/MediaCrawler/.venv/Scripts/python.exe",
                "cdp_port_start": 9222,
                "cdp_port_stride": 10,
            }

            command = WEB.build_account_login_command(root, config, "account-b")

            self.assertIn("--browser-profile-key", command)
            self.assertEqual(command[command.index("--browser-profile-key") + 1], "account-b")
            self.assertEqual(command[command.index("--cdp-port") + 1], "9232")
            self.assertIn("--login-only", command)

    def test_account_login_is_reported_as_active_while_process_is_running(self):
        process = Mock()
        process.poll.return_value = None
        WEB._ACCOUNT_LOGIN_PROCESSES["account-a"] = process
        try:
            self.assertEqual(WEB.active_account_login_keys(), ["account-a"])
        finally:
            WEB._ACCOUNT_LOGIN_PROCESSES.clear()

    def test_successful_main_login_immediately_refreshes_creator_replicas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "local").mkdir()
            config = valid_config(creators=[valid_creator("creator-a")])
            config["collection"] = {
                "account_profiles": ["account-a", "account-b"],
                "per_creator_profile_pool": True,
                "media_crawler_dir": str(root / "MediaCrawler"),
            }
            for path in (
                root / "config" / "pipeline.example.json",
                root / "local" / "pipeline.json",
            ):
                path.write_text(json.dumps(config), encoding="utf-8")
            process = Mock()
            process.wait.return_value = 0
            refresh_result = {
                "source_profiles": ["account-a"],
                "refreshed": ["creator-a"],
                "unchanged": [],
                "unavailable_sources": [],
                "busy_profiles": [],
            }

            with patch.object(
                WEB.PIPELINE,
                "refresh_primary_profile_replicas",
                return_value=refresh_result,
            ) as refresh:
                WEB.refresh_replicas_after_account_login(
                    "account-a", process, root,
                )

            refresh.assert_called_once()
            marker = json.loads(
                (root / "runtime" / "account-login" / "account-a"
                 / "login_state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["replica_refresh"]["status"], "success")

    def test_successful_backup_login_does_not_replace_main_replicas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "local").mkdir()
            config = valid_config(creators=[valid_creator("creator-a")])
            config["collection"] = {
                "account_profiles": ["account-a", "account-b"],
            }
            for path in (
                root / "config" / "pipeline.example.json",
                root / "local" / "pipeline.json",
            ):
                path.write_text(json.dumps(config), encoding="utf-8")
            process = Mock()
            process.wait.return_value = 0

            with patch.object(
                WEB.PIPELINE, "refresh_primary_profile_replicas",
            ) as refresh:
                WEB.refresh_replicas_after_account_login(
                    "account-b", process, root,
                )

            refresh.assert_not_called()


class WebDashboardPayloadTests(unittest.TestCase):
    def test_snapshot_payload_converts_dates_and_paths_for_json(self):
        dashboard = WEB.GUI
        now = datetime(2026, 7, 24, 10, 30, tzinfo=timezone.utc)
        snapshot = dashboard.DashboardSnapshot(
            task=dashboard.TaskInfo("Ready", now, now, 0),
            latest_run=dashboard.RunInfo("run-1", "success", now, now, 4.2, Path("run.json")),
            creators=[dashboard.CreatorStatus("a", "达人 A", 3, 1, "success", "", now, now)],
            total_creators=1,
            total_works=3,
            pending_works=1,
            account_profiles_total=2,
            account_profiles_detected=1,
            latest_log=Path("pipeline.log"),
            log_tail="[2026-07-24 18:30:00] 流水线开始",
            refreshed_at=now,
        )

        payload = WEB.snapshot_payload(snapshot)

        self.assertEqual(payload["task"]["state"], "Ready")
        self.assertEqual(payload["latest_run"]["finished_at"], now.isoformat())
        self.assertEqual(payload["creators"][0]["name"], "达人 A")
        # Non-running tasks still expose the latest log and events so the web
        # dashboard can keep showing the last run's results until refreshed.
        self.assertEqual(payload["latest_log"], "pipeline.log")
        self.assertEqual(payload["log_tail"], "[2026-07-24 18:30:00] 流水线开始")
        self.assertEqual(payload["activity_events"][0]["message"], "流水线已启动")
        json.dumps(payload)

        running_payload = WEB.snapshot_payload(snapshot._replace(
            task=dashboard.TaskInfo("Running", now, now, None),
            log_tail="[2026-07-24 18:30:00] 流水线开始",
        ))
        self.assertEqual(running_payload["latest_log"], "pipeline.log")
        self.assertEqual(running_payload["activity_events"][0]["message"], "流水线已启动")

    def test_run_history_summarizes_recent_results_without_raw_tracebacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "local").mkdir()
            runs = root / "runtime" / "pipeline" / "runs"
            runs.mkdir(parents=True)
            config = {**valid_config(), "state_dir": "runtime/pipeline"}
            (root / "config" / "pipeline.example.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            (root / "local" / "pipeline.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            (runs / "20260723-090000.json").write_text(json.dumps({
                "run_id": "20260723-090000",
                "started_at": "2026-07-23T09:00:00+08:00",
                "finished_at": "2026-07-23T09:02:00+08:00",
                "wall_seconds": 120,
                "status": "success",
                "creators": [{
                    "key": "a", "creator_name": "达人 A", "status": "success",
                    "selected_count": 2, "pending_count": 0, "works": [{"status": "success"}],
                }],
            }), encoding="utf-8")
            (runs / "20260724-090000.json").write_text(json.dumps({
                "run_id": "20260724-090000",
                "started_at": "2026-07-24T09:00:00+08:00",
                "finished_at": "2026-07-24T09:03:00+08:00",
                "wall_seconds": 180,
                "status": "partial_failure",
                "creators": [
                    {"key": "a", "creator_name": "达人 A", "status": "success", "selected_count": 0},
                    {
                        "key": "b", "creator_name": "达人 B", "status": "partial_failure",
                        "collection_error": "Traceback ... account blocked ... local secret path",
                    },
                ],
            }), encoding="utf-8")
            (runs / "20260725-090000.json").write_text(json.dumps({
                "run_id": "20260725-090000",
                "status": "planned",
                "dry_run": True,
                "creators": [],
            }), encoding="utf-8")

            payload = WEB.run_history_payload(root)

            self.assertEqual(payload["stats"]["total_runs"], 2)
            self.assertEqual(payload["stats"]["successful_runs"], 1)
            self.assertEqual(payload["runs"][0]["run_id"], "20260724-090000")
            self.assertEqual(payload["runs"][0]["successful_creators"], 1)
            self.assertEqual(payload["runs"][0]["failed_creators"], 1)
            self.assertEqual(payload["runs"][0]["issues"][0]["message"], "抖音账号被风控（account blocked）")
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("Traceback", serialized)
            self.assertNotIn("local secret path", serialized)

    def test_public_error_summary_hides_windows_paths(self):
        summary = WEB.public_error_summary(
            r"作品文件不存在: D:\JR_project\douyin_creator_monitor\runtime\creator-works.json"
        )

        self.assertEqual(summary, "本地作品数据文件不存在")
        self.assertNotIn("D:\\", summary)

    def test_public_error_summary_hides_lowercase_tracebacks_and_posix_paths(self):
        self.assertEqual(
            WEB.public_error_summary("worker failed: traceback at /tmp/runtime/secret.py"),
            "运行组件异常，请查看技术日志",
        )
        self.assertEqual(
            WEB.public_error_summary("worker failed at /home/user/private/config.json"),
            "本地文件或目录异常，请查看技术日志",
        )

    def test_activity_events_classify_creator_completion_statuses(self):
        events = WEB.activity_events(
            "[2026-07-24 18:30:00] ========== 达人结束: 达人 A，状态 success ==========\n"
            "[2026-07-24 18:31:00] ========== 达人结束: 达人 B，状态 partial_failure =========="
        )

        self.assertEqual([event["level"] for event in events], ["success", "failed"])

    def test_history_normalizes_internal_failure_statuses(self):
        summary = WEB.summarize_run({
            "run_id": "blocked",
            "status": "blocked_before_downstream",
            "creators": [{"key": "a", "status": "blocked_before_downstream"}],
        })

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["failed_creators"], 1)


class WebDashboardStaticTests(unittest.TestCase):
    def test_finished_run_uses_structured_creator_status_not_truncated_log_events(self):
        project_dir = Path(__file__).resolve().parents[1]
        javascript = (project_dir / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("const useFinalRunStatus", javascript)
        self.assertIn('creator.status === "success"', javascript)
        self.assertIn("useFinalRunStatus ? creator.detail", javascript)

    def test_config_page_uses_category_navigation_and_one_content_panel(self):
        project_dir = Path(__file__).resolve().parents[1]
        html = (project_dir / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (project_dir / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="config-category-nav"', html)
        self.assertIn('id="config-category-content"', html)
        self.assertIn("function switchConfigCategory", javascript)
        self.assertIn("function syncVisibleConfigToState", javascript)

    def test_main_navigation_separates_overview_current_task_and_history(self):
        project_dir = Path(__file__).resolve().parents[1]
        html = (project_dir / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (project_dir / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn('data-tab-target="overview"', html)
        self.assertIn('data-tab-target="current"', html)
        self.assertIn('data-tab-target="history"', html)
        self.assertIn('id="tab-current"', html)
        self.assertIn('id="tab-history"', html)
        self.assertNotIn("当次 / 最近结果", html)
        self.assertIn("当前没有正在执行的任务", javascript)

    def test_all_backup_destinations_share_the_backup_strategy_category(self):
        project_dir = Path(__file__).resolve().parents[1]
        javascript = (project_dir / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn('["ima.enabled"', javascript)
        self.assertIn('["kuake.enabled"', javascript)
        self.assertIn('["obsidian.enabled"', javascript)
        self.assertNotIn('key: "ima"', javascript)
        self.assertNotIn('key: "kuake"', javascript)
        self.assertNotIn('key: "obsidian"', javascript)

    def test_account_pool_ui_explains_ordered_primary_and_backup_roles(self):
        project_dir = Path(__file__).resolve().parents[1]
        javascript = (project_dir / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("列表第一项永远是主账号", javascript)
        self.assertIn("新的第一项自动成为主账号", javascript)
        self.assertIn('"账号槽位名称 · 主账号"', javascript)
        self.assertIn("`账号槽位名称 · 备用账号 ${index}`", javascript)


class WebDashboardBrowserLaunchTests(unittest.TestCase):
    def test_open_dashboard_closes_only_new_blank_windows_in_existing_user_chrome(self):
        opener = Mock(return_value=True)
        with patch.object(
            WEB, "find_unrelated_user_chrome_pids", return_value={10, 20},
        ) as find_pids, patch.object(
            WEB, "visible_blank_chrome_windows", return_value={100: 10},
        ) as list_blanks, patch.object(
            WEB, "close_new_blank_chrome_windows", return_value={101},
        ) as close_blanks:
            result = WEB.open_dashboard_without_blank("http://127.0.0.1:8765/", opener=opener)

        self.assertTrue(result)
        find_pids.assert_called_once_with()
        list_blanks.assert_called_once_with({10, 20})
        opener.assert_called_once_with("http://127.0.0.1:8765/")
        close_blanks.assert_called_once_with({100}, {10, 20})


class WebDashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "local").mkdir()
        (self.root / "web").mkdir()
        (self.root / "config" / "pipeline.example.json").write_text(
            json.dumps(valid_config()), encoding="utf-8"
        )
        (self.root / "local" / "pipeline.json").write_text(
            json.dumps(valid_config(max_works=3)), encoding="utf-8"
        )
        (self.root / "web" / "index.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
        self.snapshot_builder = Mock(side_effect=RuntimeError("status unavailable in test"))
        self.task_starter = Mock(return_value="started")
        self.account_status_provider = Mock(return_value={"profiles": []})
        self.account_login_starter = Mock(return_value={"message": "login started"})
        self.history_provider = Mock(return_value={"stats": {"total_runs": 0}, "runs": []})
        handler = WEB.make_handler(
            self.root,
            snapshot_builder=self.snapshot_builder,
            task_starter=self.task_starter,
            account_status_provider=self.account_status_provider,
            account_login_starter=self.account_login_starter,
            history_provider=self.history_provider,
        )
        self.server = WEB.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, *, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
            return response.status, json.loads(body) if "json" in content_type else body.decode()

    def test_config_api_reads_and_saves_pipeline_json(self):
        status, payload = self.request("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["max_works"], 3)

        status, payload = self.request(
            "/api/config",
            method="PUT",
            payload=valid_config(max_works=8, creators=[valid_creator("new")]),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["max_works"], 8)
        saved = json.loads((self.root / "local" / "pipeline.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["creators"][0]["key"], "new")

    def test_run_api_only_calls_task_scheduler_adapter(self):
        status, payload = self.request("/api/run", method="POST", payload={})

        self.assertEqual(status, 202)
        self.assertEqual(payload["message"], "started")
        self.task_starter.assert_called_once_with(WEB.GUI.TASK_NAME)

    def test_account_login_api_starts_selected_profile(self):
        status, payload = self.request(
            "/api/accounts/login",
            method="POST",
            payload={"profile_key": "account-a"},
        )

        self.assertEqual(status, 202)
        self.assertEqual(payload["message"], "login started")
        self.account_login_starter.assert_called_once_with("account-a", self.root)

    def test_history_api_returns_plain_language_run_summaries(self):
        status, payload = self.request("/api/history")

        self.assertEqual(status, 200)
        self.assertEqual(payload["stats"]["total_runs"], 0)
        self.history_provider.assert_called_once_with(self.root)

    def test_invalid_config_returns_400_without_overwriting_file(self):
        before = (self.root / "local" / "pipeline.json").read_text(encoding="utf-8")

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "/api/config",
                method="PUT",
                payload=valid_config(
                    creators=[valid_creator("duplicate"), valid_creator("duplicate")]
                ),
            )

        self.assertEqual(caught.exception.code, 400)
        self.assertEqual((self.root / "local" / "pipeline.json").read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
