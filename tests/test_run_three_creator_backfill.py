import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_three_creator_backfill.py"
SPEC = importlib.util.spec_from_file_location("run_three_creator_backfill", MODULE_PATH)
BACKFILL = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(BACKFILL)


class ThreeCreatorBackfillTest(unittest.TestCase):
    def test_pending_works_includes_missing_feishu_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            works_file = root / "works.json"
            works_file.write_text(json.dumps({
                "works": [{"aweme_id": "123", "create_time": 1}],
            }), encoding="utf-8")
            media_dir = root / "media"
            media_dir.mkdir()
            (media_dir / "123.final.txt").write_text("??", encoding="utf-8")
            state_dir = root / "state"
            state_path = state_dir / "demo" / "123.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({
                "stages": {
                    "ima_backed_up": {"status": "success"},
                    "kuake_backed_up": {"status": "success"},
                    "obsidian_exported": {"status": "success"},
                },
            }), encoding="utf-8")
            config = {
                "state_dir": str(state_dir),
                "media_dir": str(media_dir),
                "creators": [{"key": "demo", "works_file": str(works_file)}],
            }
            self.assertEqual([item["aweme_id"] for item in BACKFILL.pending_works(config, "demo")], ["123"])

    def test_run_batch_does_not_skip_feishu(self):
        fake_process = Mock()
        fake_process.stdout = io.StringIO("")
        fake_process.wait.return_value = 0
        with patch.object(BACKFILL.subprocess, "Popen", return_value=fake_process) as popen, patch.object(
            BACKFILL, "newest_summary", return_value=None,
        ):
            BACKFILL.run_batch("demo", skip_collect=True, aweme_ids=["123"], handle=io.StringIO())
        command = popen.call_args.args[0]
        self.assertNotIn("--skip-feishu-sync", command)
        self.assertNotIn("--skip-feishu-writeback", command)


if __name__ == "__main__":
    unittest.main()
