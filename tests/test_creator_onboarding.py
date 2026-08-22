import argparse
import importlib.util
import json
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

    def test_missing_address_fields_do_not_produce_required_field_warnings(self):
        _, warnings = FIELDS.validate_profile_patch({
            "达人主页地址": "https://www.douyin.com/user/sec",
            "账号ID": "demo123",
            "关注数": 1,
            "粉丝数": 2,
            "获赞数": 3,
            "作品数": 4,
            "最近发稿时间": "2026-08-16 11:00:00",
            "账号状态": "正常",
        })

        self.assertFalse(any("IP属地" in warning for warning in warnings))
        self.assertFalse(any("所在地区" in warning for warning in warnings))

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

    def test_supplement_child_scripts_force_utf8_output(self):
        completed = Mock(returncode=0, stdout="", stderr="")
        with patch.object(SUPPLEMENT.subprocess, "run", return_value=completed) as run:
            result = SUPPLEMENT.run_script("feishu_work_status_writer.py")

        self.assertIs(result, completed)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["errors"], "replace")
        self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_existing_summary_reconciles_stale_missing_marker_and_feishu_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            works_file = root / "works.json"
            works_file.write_text(
                json.dumps({"works": [{"aweme_id": "1", "title": "work"}]}),
                encoding="utf-8",
            )
            transcript = root / "1.txt"
            transcript.write_text("transcript", encoding="utf-8")
            summary_output = root / "1-summary.md"
            notes = root / "notes" / "creator-a"
            notes.mkdir(parents=True)
            (notes / "2026-01-01_1_work.md").write_text(
                "# work\n\n## 内容总结\n\n已有总结\n",
                encoding="utf-8",
            )
            state_path = root / "state" / "creator-a" / "1.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps({
                    "aweme_id": "1",
                    "stages": {
                        "summarized": {"status": SUPPLEMENT.SUMMARY_TEMPLATE_MISSING},
                    },
                }),
                encoding="utf-8",
            )
            prompt = root / "prompt.md"
            prompt.write_text("prompt", encoding="utf-8")
            config = {
                "state_dir": str(root / "state"),
                "media_dir": str(root / "media"),
                "obsidian": {
                    "enabled": True,
                    "original_dir": str(root / "notes"),
                },
                "summary": {"enabled": True},
            }
            creator = {
                "key": "creator-a",
                "creator_name": "Creator A",
                "creator_dir_name": "creator-a",
                "works_file": str(works_file),
                "works_table_id": "table-a",
            }
            args = argparse.Namespace(
                force=False,
                dry_run=False,
                no_feishu=False,
                _summary_capability="llm",
            )
            with patch.object(SUPPLEMENT, "hydrate_creator_type"), patch.object(
                SUPPLEMENT.pipe, "select_summary_template_file", return_value=prompt,
            ), patch.object(
                SUPPLEMENT.pipe, "summary_capability", return_value="none",
            ), patch.object(
                SUPPLEMENT.pipe,
                "artifact_paths",
                return_value={"final": transcript, "summary": summary_output},
            ), patch.object(
                SUPPLEMENT, "generate_summary",
            ) as generate, patch.object(
                SUPPLEMENT, "write_feishu_local_status", return_value=True,
            ) as write_status:
                counters = SUPPLEMENT.supplement_creator(config, creator, args)

            self.assertEqual(counters["reconciled"], 1)
            self.assertEqual(counters["skipped"], 0)
            generate.assert_not_called()
            write_status.assert_called_once_with(
                table_id="table-a",
                aweme_id="1",
                local_status="已写入",
                dry_run=False,
            )
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["stages"]["summarized"]["status"], "success")

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
