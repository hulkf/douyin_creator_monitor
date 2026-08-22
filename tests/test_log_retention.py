import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rotate_runtime_logs.py"
SPEC = importlib.util.spec_from_file_location("rotate_runtime_logs", SCRIPT)
ROTATE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(ROTATE)


class LogRetentionTest(unittest.TestCase):
    def test_rotates_large_task_log_and_removes_only_old_known_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_log = root / "task-launch.log"
            task_log.write_text("x" * 20, encoding="utf-8")
            old_pipeline = root / "pipeline-20200101-000000.log"
            old_pipeline.write_text("old", encoding="utf-8")
            unrelated = root / "keep.txt"
            unrelated.write_text("keep", encoding="utf-8")
            now = 2_000_000_000.0
            old = now - 61 * 86400
            os.utime(old_pipeline, (old, old))
            os.utime(unrelated, (old, old))

            result = ROTATE.rotate_logs(
                root, max_task_log_bytes=10, retention_days=60, now=now,
            )

            self.assertFalse(task_log.exists())
            self.assertTrue((root / result["rotated"]).exists())
            self.assertFalse(old_pipeline.exists())
            self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
