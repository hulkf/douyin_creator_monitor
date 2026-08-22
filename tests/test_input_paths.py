import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "input_paths.py"
SPEC = importlib.util.spec_from_file_location("input_paths", MODULE_PATH)
assert SPEC and SPEC.loader
INPUT_PATHS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INPUT_PATHS)


class InputPathsTests(unittest.TestCase):
    def test_accepts_single_file_with_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one file.txt"
            path.write_text("ok", encoding="utf-8")
            self.assertEqual(INPUT_PATHS.iter_input_files(f'"{path}"', pattern="*.txt"), [path])

    def test_accepts_directory_and_only_direct_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "folder input"
            nested = root / "nested"
            root.mkdir()
            nested.mkdir()
            first = root / "first.txt"
            second = root / "second.txt"
            deep = nested / "deep.txt"
            for path in (first, second, deep):
                path.write_text("ok", encoding="utf-8")
            self.assertEqual(INPUT_PATHS.iter_input_files(f"'{root}'", pattern="*.txt"), [first, second])

    def test_non_matching_single_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.mp4"
            path.write_bytes(b"video")
            self.assertEqual(INPUT_PATHS.iter_input_files(path, pattern="*.txt"), [])


if __name__ == "__main__":
    unittest.main()
