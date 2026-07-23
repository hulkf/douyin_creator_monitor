import argparse
import importlib.util
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ONBOARD = load_module("check_and_onboard_new_creators")
PROFILE = load_module("collect_douyin_creator_profile")
SUPPLEMENT = load_module("supplement_content_summary")
SCHEMA = load_module("work_table_schema")
FIELDS = load_module("creator_table_fields")


class CreatorOnboardingTest(unittest.TestCase):
    def test_profile_sync_creator_filter_does_not_touch_other_creators(self):
        config = {
            "creators": [
                {"key": "a", "creator_url": "https://www.douyin.com/user/sec-a"},
                {"key": "b", "creator_url": "https://www.douyin.com/user/sec-b"},
            ]
        }
        args = argparse.Namespace(base_token=None, lark_cli="lark-cli", creator=["a"])
        records = [
            {"record_id": "rec-a", "fields": {"达人主页地址": "https://www.douyin.com/user/sec-a"}},
            {"record_id": "rec-b", "fields": {"达人主页地址": "https://www.douyin.com/user/sec-b"}},
        ]
        with patch.object(ONBOARD, "load_base_token", return_value="token"), patch.object(
            ONBOARD, "list_creator_records", return_value=records,
        ), patch.object(ONBOARD, "sync_profile_to_feishu", return_value=True) as sync:
            code = ONBOARD.run_sync_profiles(args, config, "tbl", "user")

        self.assertEqual(code, 0)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(sync.call_args.args[4], "a")

    def test_collect_failure_blocks_profile_sync(self):
        config = {"feishu": {"creator_table_id": "tbl"}}
        with patch.object(ONBOARD, "load_config", return_value=config), patch.object(
            ONBOARD, "run_collect_profiles", return_value=1,
        ), patch.object(ONBOARD, "run_sync_profiles") as sync:
            code = ONBOARD.main(["--collect-profiles", "--sync-profiles"])

        self.assertEqual(code, 1)
        sync.assert_not_called()

    def test_missing_login_profile_is_not_reported_as_success(self):
        config = {
            "collection": {"media_crawler_dir": str(Path(__file__).parent)},
            "creators": [{"key": "a", "creator_url": "https://www.douyin.com/user/sec"}],
        }
        with patch.object(ONBOARD, "_resolve_user_data_dir", return_value=None):
            code = ONBOARD.run_collect_profiles(argparse.Namespace(), config)
        self.assertEqual(code, 1)

    def test_embedded_profile_can_be_used_when_dom_profile_is_missing(self):
        embedded = {"达人昵称": "可靠达人", "SecUID": "sec"}
        self.assertEqual(PROFILE.merge_profile_sources(None, embedded), embedded)

    def test_new_work_table_accepts_summary_pending_status(self):
        local_field = next(
            field for field in SCHEMA.CANONICAL_WORK_FIELDS
            if field["name"] == "本地知识库状态"
        )
        self.assertIn("内容总结待补充", {item["name"] for item in local_field["options"]})

    def test_invalid_profile_values_are_removed_before_non_destructive_sync(self):
        patch_value, warnings = FIELDS.validate_profile_patch({
            "达人主页地址": "https://www.douyin.com/user/sec",
            "粉丝数": "not-a-number",
            "账号ID": "MS4wLjABVeryLongSecUidToken",
            "性别": "未知",
        })
        self.assertNotIn("粉丝数", patch_value)
        self.assertNotIn("账号ID", patch_value)
        self.assertTrue(warnings)

    def test_supplement_exception_is_reported_as_failure(self):
        with patch.object(SUPPLEMENT.pipe, "read_json", return_value={"creators": [{"key": "a"}]}), patch.object(
            SUPPLEMENT.pipe, "path_from", return_value=Path(__file__),
        ), patch.object(SUPPLEMENT, "supplement_creator", side_effect=RuntimeError("boom")):
            code = SUPPLEMENT.main(["--config", str(__file__), "--all"])

        self.assertEqual(code, 1)

    def test_supplement_indexes_creator_notes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator_dir = root / "达人"
            creator_dir.mkdir()
            first = creator_dir / "2026-01-01_123_title.md"
            second = creator_dir / "2026-01-02_456_title.md"
            first.write_text("a", encoding="utf-8")
            second.write_text("b", encoding="utf-8")
            index = SUPPLEMENT.index_creator_notes(root, "达人")
        self.assertEqual(index, {"123": first, "456": second})

    def test_pipeline_supplement_propagates_child_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state" / "a"
            state.mkdir(parents=True)
            (state / "1.json").write_text(
                '{"aweme_id":"1","stages":{"summarized":{"status":"summary_template_missing"}}}',
                encoding="utf-8",
            )
            config = {
                "state_dir": str(root / "state"),
                "summary": {"model": "m", "api_key_env": "SUMMARY_KEY"},
                "obsidian": {"summary_template_file": str(root / "prompt.md")},
            }
            runner = Mock()
            runner.run.side_effect = RuntimeError("summary child failed")
            args = argparse.Namespace(_summary_capability="llm")
            with self.assertRaises(RuntimeError):
                SUPPLEMENT.pipe.run_missing_summary_supplement(
                    config, [{"key": "a"}], runner, Mock(), {}, args,
                )

    def test_profile_collection_uses_three_independent_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creators = [
                {"key": f"c{i}", "creator_url": f"https://www.douyin.com/user/sec{i}"}
                for i in range(4)
            ]
            config = {
                "collection": {
                    "media_crawler_dir": str(root),
                    "media_crawler_python": "python",
                    "profile_max_workers": 3,
                },
                "creators": creators,
            }
            active = 0
            maximum = 0
            lock = threading.Lock()

            def fake_run(*unused, **kwargs):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.04)
                with lock:
                    active -= 1
                return Mock(returncode=0)

            args = argparse.Namespace()
            with patch.object(ONBOARD, "_resolve_user_data_dir", return_value=str(root)), patch.object(
                ONBOARD.subprocess, "run", side_effect=fake_run,
            ):
                code = ONBOARD.run_collect_profiles(args, config)

        self.assertEqual(code, 0)
        self.assertEqual(maximum, 3)

    def test_fresh_profile_is_reused_within_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime").mkdir()
            (root / "runtime" / "profile-a-update.json").write_text("{}", encoding="utf-8")
            config = {
                "collection": {
                    "media_crawler_dir": str(root),
                    "media_crawler_python": "python",
                    "profile_ttl_hours": 12,
                },
                "creators": [{"key": "a", "creator_url": "https://www.douyin.com/user/sec"}],
            }
            with patch.object(ONBOARD, "PROJECT_DIR", root), patch.object(
                ONBOARD, "_resolve_user_data_dir", side_effect=AssertionError("fresh cache must not inspect browser state"),
            ), patch.object(ONBOARD.subprocess, "run") as run:
                code = ONBOARD.run_collect_profiles(argparse.Namespace(), config)

        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_creator_profile_path_respects_configured_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(ONBOARD, "PROJECT_DIR", root):
                path = ONBOARD.creator_profile_path({"key": "a", "profile_file": "runtime/custom.json"})
        self.assertEqual(path, root / "runtime" / "custom.json")


if __name__ == "__main__":
    unittest.main()
