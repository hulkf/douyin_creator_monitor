import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


PIPELINE = load_module("pipeline_safe_paths", "run_creator_pipeline.py")
OBSIDIAN = load_module("obsidian_safe_paths", "export_transcript_to_obsidian.py")
BACKFILL = load_module("three_creator_backfill", "run_three_creator_backfill.py")


class SafeBackupPathTest(unittest.TestCase):
    def test_external_title_removes_emoji_controls_and_windows_invalid_chars(self):
        work = {"aweme_id": "123", "title": "标题🤣<>:\\/?*|\x01 后半段" + "很长" * 40}
        value = PIPELINE.safe_external_title(work)
        self.assertNotIn("🤣", value)
        self.assertFalse(any(char in value for char in '<>:\\/?*|'))
        self.assertNotIn("\x01", value)
        self.assertLessEqual(len(value), 56)

    def test_obsidian_path_part_removes_non_bmp_and_limits_title(self):
        value = OBSIDIAN.sanitize_path_part("测试🤣标题" + "长" * 100, fallback="fallback", limit=56)
        self.assertNotIn("🤣", value)
        self.assertLessEqual(len(value), 56)

    def test_collection_marker_is_honored(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            collection_dir = state_dir / "collection"
            collection_dir.mkdir()
            (collection_dir / "demo.json").write_text(
                json.dumps({"full_history_collected": True}), encoding="utf-8"
            )
            self.assertTrue(BACKFILL.collection_is_complete("demo", state_dir))

    def test_pending_works_only_returns_missing_final_or_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            media_dir = root / "media"
            works_file = root / "works.json"
            (state_dir / "demo").mkdir(parents=True)
            media_dir.mkdir()
            works_file.write_text(
                json.dumps({"works": [
                    {"aweme_id": "done", "create_time": 2},
                    {"aweme_id": "missing", "create_time": 1},
                ]}),
                encoding="utf-8",
            )
            (media_dir / "done.final.txt").write_text("文案", encoding="utf-8")
            (state_dir / "demo" / "done.json").write_text(
                json.dumps({"stages": {
                    "ima_backed_up": {"status": "success"},
                    "kuake_backed_up": {"status": "success"},
                    "obsidian_exported": {"status": "success"},
                    "feishu_synced": {"status": "success"},
                    "feishu_written_back": {"status": "success"},
                    "backup_statuses_written_back": {"status": "success"},
                }}),
                encoding="utf-8",
            )
            config = {
                "state_dir": str(state_dir),
                "media_dir": str(media_dir),
                "ima": {"enabled": True},
                "kuake": {"enabled": True},
                "obsidian": {"enabled": True},
                "creators": [{"key": "demo", "works_file": str(works_file)}],
            }
            self.assertEqual(
                [work["aweme_id"] for work in BACKFILL.pending_works(config, "demo")],
                ["missing"],
            )


if __name__ == "__main__":
    unittest.main()
