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
MODULE_PATH = SCRIPTS_DIR / "feishu_work_status_writer.py"
SPEC = importlib.util.spec_from_file_location("feishu_work_status_writer", MODULE_PATH)
WRITER = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(WRITER)


class WorkStatusWriterTest(unittest.TestCase):
    def test_build_patch_writes_all_statuses_and_record_time(self):
        args = argparse.Namespace(
            ima_status="已上传", kuake_status="已上传", local_status="已写入",
            record_time="2026-07-18 12:34:56",
        )
        self.assertEqual(
            WRITER.build_patch(args),
            {
                "记录时间": 1784349296000,
                "ima状态": "已上传",
                "夸克网盘状态": "已上传",
                "本地知识库状态": "已写入",
            },
        )

    def test_build_patch_can_finalize_transcript_and_statuses_together(self):
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "final.txt"
            transcript.write_text("最终文案", encoding="utf-8")
            args = argparse.Namespace(
                ima_status="已上传", kuake_status="已上传", local_status="已写入",
                record_time="2026-07-18 12:34:56", transcript_file=str(transcript),
                transcript_field="语音转写全文",
            )
            patch_value = WRITER.build_patch(args)
        self.assertEqual(patch_value["语音转写全文"], "最终文案")

    def test_manifest_writeback_uses_one_heterogeneous_batch_api_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "final.txt"
            transcript.write_text("最终文案", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "table_id": "tbl1",
                "records": [
                    {"work_id": "1", "record_id": "rec1", "transcript_file": str(transcript)},
                    {"work_id": "2", "record_id": "rec2", "transcript_file": str(transcript)},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            outputs = []
            requests = []

            def capture_batch(cli, command, input_text=None):
                self.assertEqual(command[command.index("--data") + 1], "-")
                self.assertIsNotNone(input_text)
                requests.append(json.loads(input_text))
                return {"data": {"records": [{"record_id": "rec1"}, {"record_id": "rec2"}]}}

            with patch.object(WRITER, "resolve_lark_cli", return_value="lark"), patch.object(
                WRITER, "load_base_token", return_value="token",
            ), patch.object(WRITER, "run_lark", side_effect=capture_batch), patch(
                "builtins.print", side_effect=lambda value: outputs.append(str(value)),
            ):
                exit_code = WRITER.main(["--manifest", str(manifest)])

        payload = json.loads(outputs[-1])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual([item["record_id"] for item in requests[0]["records"]], ["rec1", "rec2"])
        self.assertEqual(requests[0]["records"][0]["fields"]["语音转写全文"], "最终文案")
        self.assertEqual(payload["results"]["1"]["status"], "success")
        self.assertEqual(payload["results"]["2"]["status"], "success")

    def test_manifest_stops_remaining_batches_after_permanent_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "final.txt"
            transcript.write_text("最终文案", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "table_id": "tbl1",
                "records": [
                    {"work_id": "1", "record_id": "rec1", "transcript_file": str(transcript)},
                    {"work_id": "2", "record_id": "rec2", "transcript_file": str(transcript)},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            outputs = []
            with patch.object(WRITER, "BATCH_SIZE", 1), patch.object(
                WRITER, "resolve_lark_cli", return_value="lark",
            ), patch.object(WRITER, "load_base_token", return_value="token"), patch.object(
                WRITER, "run_lark", side_effect=SystemExit("91403 permission denied"),
            ) as run_lark, patch("builtins.print", side_effect=lambda value: outputs.append(str(value))):
                WRITER.main(["--manifest", str(manifest)])

        payload = json.loads(outputs[-1])
        self.assertEqual(run_lark.call_count, 1)
        self.assertEqual(payload["results"]["1"]["status"], "failed")
        self.assertEqual(payload["results"]["2"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
