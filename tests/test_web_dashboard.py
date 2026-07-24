import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "web_dashboard.py"
SPEC = importlib.util.spec_from_file_location("web_dashboard", MODULE_PATH)
WEB = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(WEB)


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
        config = {
            "collection": {"profile_max_workers": 0},
            "creators": [
                {"key": "same", "enabled": True},
                {"key": "same", "enabled": False},
            ],
        }

        errors = WEB.validate_pipeline_config(config)

        self.assertTrue(any("profile_max_workers" in error for error in errors))
        self.assertTrue(any("重复" in error for error in errors))

    def test_save_config_is_atomic_and_keeps_one_recoverable_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "local").mkdir()
            path = root / "local" / "pipeline.json"
            path.write_text('{"version": 1}', encoding="utf-8")

            result = WEB.save_pipeline_config(
                {"version": 2, "creators": [{"key": "creator-a"}]}, root
            )

            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 2)
            backup = root / "local" / "pipeline.backup.json"
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["version"], 1)
            self.assertEqual(result.path, path)
            self.assertEqual(result.backup_path, backup)


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
            log_tail="done",
            refreshed_at=now,
        )

        payload = WEB.snapshot_payload(snapshot)

        self.assertEqual(payload["task"]["state"], "Ready")
        self.assertEqual(payload["latest_run"]["finished_at"], now.isoformat())
        self.assertEqual(payload["creators"][0]["name"], "达人 A")
        self.assertEqual(payload["latest_log"], "pipeline.log")
        json.dumps(payload)


class WebDashboardHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "local").mkdir()
        (self.root / "web").mkdir()
        (self.root / "config" / "pipeline.example.json").write_text(
            json.dumps({"max_works": 0, "creators": []}), encoding="utf-8"
        )
        (self.root / "local" / "pipeline.json").write_text(
            json.dumps({"max_works": 3, "creators": []}), encoding="utf-8"
        )
        (self.root / "web" / "index.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
        self.snapshot_builder = Mock(side_effect=RuntimeError("status unavailable in test"))
        self.task_starter = Mock(return_value="started")
        handler = WEB.make_handler(
            self.root,
            snapshot_builder=self.snapshot_builder,
            task_starter=self.task_starter,
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
            payload={"max_works": 8, "creators": [{"key": "new"}]},
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

    def test_invalid_config_returns_400_without_overwriting_file(self):
        before = (self.root / "local" / "pipeline.json").read_text(encoding="utf-8")

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request(
                "/api/config",
                method="PUT",
                payload={"creators": [{"key": "duplicate"}, {"key": "duplicate"}]},
            )

        self.assertEqual(caught.exception.code, 400)
        self.assertEqual((self.root / "local" / "pipeline.json").read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
