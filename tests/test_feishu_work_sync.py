import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
MODULE_PATH = SCRIPTS_DIR / "sync_douyin_works_to_feishu.py"
SPEC = importlib.util.spec_from_file_location("sync_douyin_works_to_feishu", MODULE_PATH)
SYNC = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(SYNC)


class FeishuWorkSyncTest(unittest.TestCase):
    def test_work_patch_only_uses_canonical_schema_fields(self):
        patch_value = SYNC.build_patch(
            {
                "aweme_id": "123",
                "desc": "测试 #千川",
                "digg_count": 10,
                "comment_count": 2,
                "collect_count": 3,
                "share_count": 4,
                "create_time": 1,
            },
            "2026-07-18 12:00:00",
        )
        self.assertNotIn("页面可见数字", patch_value)
        self.assertNotIn("页面可见数字含义", patch_value)
        self.assertTrue(set(patch_value).issubset(SYNC.CANONICAL_WORK_FIELD_NAMES))

    def test_existing_record_scan_reads_every_page(self):
        first = {
            "data": {
                "fields": ["抖音作品ID", "作品标题"],
                "data": [["1", "第一条"]],
                "record_id_list": ["rec1"],
                "has_more": True,
            }
        }
        second = {
            "data": {
                "fields": ["抖音作品ID", "作品标题"],
                "data": [["2", "第二条"]],
                "record_id_list": ["rec2"],
                "has_more": False,
            }
        }
        with patch.object(SYNC, "run_lark", side_effect=[first, second]) as mocked:
            records = SYNC.load_existing_records("cli", "base", "table")
        self.assertEqual(set(records), {"1", "2"})
        second_args = mocked.call_args_list[1].args[1]
        self.assertEqual(second_args[second_args.index("--offset") + 1], "200")

    def test_existing_record_scan_projects_only_sync_fields(self):
        response = {
            "data": {
                "fields": ["抖音作品ID"], "data": [["1"]],
                "record_id_list": ["rec1"], "has_more": False,
            }
        }
        with patch.object(SYNC, "run_lark", return_value=response) as mocked:
            SYNC.load_existing_records("cli", "base", "table")
        command = mocked.call_args.args[1]
        requested = [command[index + 1] for index, value in enumerate(command) if value == "--field-id"]
        self.assertIn("抖音作品ID", requested)
        self.assertIn("点赞数", requested)
        self.assertIn("原始作品数据", requested)
        self.assertNotIn("语音转写全文", requested)
        self.assertNotIn("IMA状态", requested)
        self.assertNotIn("摘要", requested)

    def test_unchanged_content_does_not_trigger_update(self):
        patch_value = {"抖音作品ID": "123", "作品标题": "相同标题", "点赞数": 10}
        existing = {"record_id": "rec123", "fields": dict(patch_value)}
        self.assertFalse(SYNC.has_content_changes(existing, patch_value))
        self.assertTrue(SYNC.has_content_changes(existing, {**patch_value, "点赞数": 11}))

    def test_sync_plan_separates_new_changed_and_unchanged_records(self):
        works = [
            {"aweme_id": "same", "desc": "相同", "create_time": 1, "digg_count": 1, "comment_count": 1, "collect_count": 1, "share_count": 1},
            {"aweme_id": "changed", "desc": "新标题", "create_time": 1, "digg_count": 2, "comment_count": 1, "collect_count": 1, "share_count": 1},
            {"aweme_id": "new", "desc": "新增", "create_time": 1, "digg_count": 3, "comment_count": 1, "collect_count": 1, "share_count": 1},
        ]
        same_patch = SYNC.build_patch(works[0], "old")
        changed_patch = SYNC.build_patch(works[1], "old")
        existing = {
            "same": {"record_id": "rec-same", "fields": same_patch},
            "changed": {"record_id": "rec-changed", "fields": {**changed_patch, "点赞数": 1}},
        }
        plan = SYNC.plan_sync(works, existing, "2026-07-18 12:00:00")
        self.assertEqual([item["work_id"] for item in plan["create"]], ["new"])
        self.assertEqual([item["work_id"] for item in plan["update"]], ["changed"])
        self.assertEqual([item["work_id"] for item in plan["skip"]], ["same"])

    def test_schema_validation_reports_missing_and_mismatched_fields(self):
        live_fields = [
            {"name": "抖音作品ID", "type": "number", "style": {"type": "plain"}},
            {"name": "作品标题", "type": "text", "style": {"type": "plain"}},
        ]
        problems = SYNC.schema_mismatches(live_fields)
        self.assertIn("抖音作品ID: type expected text, got number", problems)
        self.assertIn("missing: 夸克网盘状态", problems)

    def test_sync_batches_new_records_and_returns_record_ids(self):
        work = {
            "aweme_id": "new",
            "desc": "新增作品",
            "create_time": 1,
            "digg_count": 1,
            "comment_count": 2,
            "collect_count": 3,
            "share_count": 4,
        }
        with tempfile.TemporaryDirectory() as directory:
            works_file = Path(directory) / "works.json"
            works_file.write_text(json.dumps({"works": [work]}, ensure_ascii=False), encoding="utf-8")
            args = argparse.Namespace(
                base_token="token", works_file=str(works_file), skip_schema_validation=True,
                lark_cli="cli", table_id="table", dry_run=False,
            )
            create_response = {"data": {"record_id_list": ["rec-new"]}}
            verify_response = {
                "data": {
                    "fields": list(SYNC.CORE_VERIFY_FIELDS),
                    "data": [["new", "1970-01-01 08:00:01", 1, 2, 3, 4]],
                    "record_id_list": ["rec-new"],
                }
            }
            with patch.object(SYNC, "load_existing_records", return_value={}), patch.object(
                SYNC, "run_lark", side_effect=[create_response, verify_response]
            ) as mocked:
                result = SYNC.sync(args)
        self.assertEqual(result["record_ids"], {"new": "rec-new"})
        self.assertEqual(result["created"], 1)
        self.assertIn("+record-batch-create", mocked.call_args_list[0].args[1])
        self.assertIn("+record-get", mocked.call_args_list[1].args[1])
        self.assertEqual(result["verified"], 1)

    def test_create_batches_limits_json_size_and_preserves_order(self):
        items = [
            {
                "work_id": str(index),
                "patch": {"抖音作品ID": str(index), "原始作品数据": "x" * 8_000},
            }
            for index in range(5)
        ]

        batches = SYNC.create_batches(items)

        self.assertGreater(len(batches), 1)
        self.assertEqual(
            [item["work_id"] for batch, _ in batches for item in batch],
            [str(index) for index in range(5)],
        )
        self.assertTrue(all(len(batch) <= SYNC.MAX_BATCH_CREATE_RECORDS for batch, _ in batches))
        self.assertTrue(all(len(payload) <= SYNC.MAX_BATCH_CREATE_JSON_CHARS for _, payload in batches))

    def test_sync_splits_large_creates_and_maps_record_ids_in_work_order(self):
        works = [
            {
                "aweme_id": str(index),
                "desc": "long transcript " + "x" * 7_000,
                "create_time": index + 1,
                "digg_count": index,
                "comment_count": index,
                "collect_count": index,
                "share_count": index,
            }
            for index in range(4)
        ]
        create_payloads = []

        def fake_run_lark(cli, command):
            self.assertIn("+record-batch-create", command)
            payload_text = command[command.index("--json") + 1]
            self.assertLessEqual(len(payload_text), SYNC.MAX_BATCH_CREATE_JSON_CHARS)
            payload = json.loads(payload_text)
            create_payloads.append(payload)
            work_id_index = payload["fields"].index("抖音作品ID")
            work_ids = [str(row[work_id_index]) for row in payload["rows"]]
            return {"data": {"record_id_list": [f"rec-{work_id}" for work_id in work_ids]}}

        with tempfile.TemporaryDirectory() as directory:
            works_file = Path(directory) / "works.json"
            works_file.write_text(json.dumps({"works": works}, ensure_ascii=False), encoding="utf-8")
            args = argparse.Namespace(
                base_token="token", works_file=str(works_file), skip_schema_validation=True,
                lark_cli="cli", table_id="table", dry_run=False,
            )
            with patch.object(SYNC, "load_existing_records", return_value={}), patch.object(
                SYNC, "run_lark", side_effect=fake_run_lark,
            ), patch.object(SYNC, "verify_written_records", return_value=len(works)):
                result = SYNC.sync(args)

        self.assertGreater(len(create_payloads), 1)
        self.assertEqual(
            result["record_ids"],
            {str(index): f"rec-{index}" for index in range(4)},
        )
        self.assertEqual(
            [action["work_id"] for action in result["actions"]],
            [str(index) for index in range(4)],
        )
        self.assertEqual(result["verified"], len(works))

    def test_unchanged_rows_refresh_recent_collection_time_in_one_batch(self):
        work = {
            "aweme_id": "same", "desc": "相同", "create_time": 1,
            "digg_count": 1, "comment_count": 2, "collect_count": 3, "share_count": 4,
        }
        with tempfile.TemporaryDirectory() as directory:
            works_file = Path(directory) / "works.json"
            works_file.write_text(json.dumps({"works": [work]}, ensure_ascii=False), encoding="utf-8")
            args = argparse.Namespace(
                base_token="token", works_file=str(works_file), skip_schema_validation=True,
                lark_cli="cli", table_id="table", dry_run=False,
            )
            existing_patch = SYNC.build_patch(work, "old")
            existing = {"same": {"record_id": "rec-same", "fields": existing_patch}}
            with patch.object(SYNC, "load_existing_records", return_value=existing), patch.object(
                SYNC, "run_lark", return_value={"data": {}}
            ) as mocked:
                result = SYNC.sync(args)
        command = mocked.call_args.args[1]
        self.assertIn("+record-batch-update", command)
        payload = json.loads(command[command.index("--json") + 1])
        self.assertEqual(payload["record_id_list"], ["rec-same"])
        self.assertEqual(set(payload["patch"]), {"最近采集时间"})
        self.assertEqual(result["recent_collection_refreshed"], 1)

    def test_readback_rejects_missing_or_mismatched_core_fields(self):
        expected = {
            "new": {
                "抖音作品ID": "new", "发布时间": "1970-01-01 08:00:01",
                "点赞数": 1, "评论数": 2, "收藏数": 3, "分享数": 4,
            }
        }
        response = {
            "data": {
                "fields": list(SYNC.CORE_VERIFY_FIELDS),
                "data": [["new", "1970-01-01 08:00:01", 1, 2, None, 4]],
                "record_id_list": ["rec-new"],
            }
        }
        with patch.object(SYNC, "run_lark", return_value=response):
            with self.assertRaises(SystemExit):
                SYNC.verify_written_records("cli", "base", "table", {"new": "rec-new"}, expected)

    def test_readback_accepts_structured_record_get_response(self):
        expected = {
            "new": {
                "抖音作品ID": "new", "发布时间": "1970-01-01 08:00:01",
                "点赞数": 1, "评论数": 2, "收藏数": 3, "分享数": 4,
            }
        }
        response = {"data": {"items": [{"record_id": "rec-new", "fields": expected["new"]}]}}
        with patch.object(SYNC, "run_lark", return_value=response):
            verified = SYNC.verify_written_records(
                "cli", "base", "table", {"new": "rec-new"}, expected,
            )
        self.assertEqual(verified, 1)


if __name__ == "__main__":
    unittest.main()
