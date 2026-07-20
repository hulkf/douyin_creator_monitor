import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "collect_douyin_creator_with_mediacrawler.py"
SPEC = importlib.util.spec_from_file_location("incremental_collector", MODULE_PATH)
COLLECTOR = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(COLLECTOR)


class IncrementalCollectionTest(unittest.TestCase):
    @staticmethod
    def page(*ids: str):
        return [
            {"aweme_id": work_id, "create_time": 100 - index}
            for index, work_id in enumerate(ids)
        ]

    def test_known_first_work_still_probes_three_then_stops(self):
        selected, checked, stopped, boundary = COLLECTOR.select_incremental_page(
            self.page("known-1", "known-2", "known-3", "known-4"),
            {"known-1", "known-2", "known-3", "known-4"},
            0,
            3,
        )
        self.assertEqual([item["aweme_id"] for item in selected], ["known-1", "known-2", "known-3"])
        self.assertEqual(checked, 3)
        self.assertTrue(stopped)
        self.assertEqual(boundary, "known-1")

    def test_new_prefix_continues_until_first_known_work(self):
        selected, checked, stopped, boundary = COLLECTOR.select_incremental_page(
            self.page("new-1", "new-2", "new-3", "known-1", "known-2"),
            {"known-1", "known-2"},
            0,
            3,
        )
        self.assertEqual(
            [item["aweme_id"] for item in selected],
            ["new-1", "new-2", "new-3", "known-1"],
        )
        self.assertEqual(checked, 4)
        self.assertTrue(stopped)
        self.assertEqual(boundary, "known-1")

    def test_page_without_known_work_does_not_stop(self):
        selected, checked, stopped, boundary = COLLECTOR.select_incremental_page(
            self.page("new-1", "new-2", "new-3"),
            {"older"},
            0,
            3,
        )
        self.assertEqual(len(selected), 3)
        self.assertEqual(checked, 3)
        self.assertFalse(stopped)
        self.assertIsNone(boundary)

    def test_merge_refreshes_metrics_without_erasing_missing_fields(self):
        existing = [{
            "aweme_id": "1",
            "create_time": 10,
            "desc": "old title",
            "cover_url": "old cover",
            "digg_count": 1,
        }]
        current = [{
            "aweme_id": "1",
            "create_time": 10,
            "desc": "",
            "cover_url": None,
            "digg_count": 9,
        }]
        merged = COLLECTOR.merge_works(existing, current)
        self.assertEqual(merged[0]["desc"], "old title")
        self.assertEqual(merged[0]["cover_url"], "old cover")
        self.assertEqual(merged[0]["digg_count"], 9)

    def test_collection_mode_requires_matching_completed_baseline(self):
        works = [{"aweme_id": "1"}]
        state = {"creator_id": "creator-1", "full_history_collected": True}
        self.assertEqual(COLLECTOR.determine_collection_mode(state, "creator-1", works, False), "incremental")
        self.assertEqual(COLLECTOR.determine_collection_mode(state, "creator-2", works, False), "full")
        self.assertEqual(COLLECTOR.determine_collection_mode({}, "creator-1", works, False), "full")
        self.assertEqual(COLLECTOR.determine_collection_mode(state, "creator-1", works, True), "full")
        self.assertEqual(COLLECTOR.determine_collection_mode(state, "creator-1", [], False), "full")


    def test_parallel_mediacrawler_runs_use_independent_bootstrap_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "MediaCrawler"
            media.mkdir()
            (media / "main.py").write_text("", encoding="utf-8")
            commands = []

            def run_external(command, **kwargs):
                commands.append(command)
                return argparse.Namespace(returncode=0)

            def invoke(key):
                args = argparse.Namespace(
                    media_crawler_dir=str(media),
                    media_crawler_python=sys.executable,
                    media_output_dir=str(root / f"output-{key}"),
                    clean_media_output=False,
                    creator_url=f"creator-{key}",
                    max_count=200,
                    save_data_option="jsonl",
                    login_type="qrcode",
                    incremental_probe_count=3,
                    browser_profile_key=f"creator-{key}",
                    cdp_port=9222 if key == "a" else 9232,
                )
                return COLLECTOR.run_mediacrawler(args, mode="incremental", known_ids=set())

            with patch.object(COLLECTOR, "PROJECT_DIR", root), patch.object(
                COLLECTOR, "RUNTIME_DIR", root / "runtime",
            ), patch.object(COLLECTOR.subprocess, "run", side_effect=run_external):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(invoke, ("a", "b")))

            bootstrap_paths = [Path(command[1]) for command in commands]
            self.assertEqual(len(set(bootstrap_paths)), 2)
            self.assertEqual(
                {path.parent for path in bootstrap_paths},
                {output_dir for output_dir, _ in results},
            )
            bootstrap_texts = [path.read_text(encoding="utf-8") for path in bootstrap_paths]
            self.assertEqual(
                {line for text in bootstrap_texts for line in text.splitlines() if line.startswith("config.CDP_DEBUG_PORT")},
                {"config.CDP_DEBUG_PORT = 9222", "config.CDP_DEBUG_PORT = 9232"},
            )
            self.assertEqual(
                {line for text in bootstrap_texts for line in text.splitlines() if line.startswith("config.USER_DATA_DIR")},
                {
                    "config.USER_DATA_DIR = 'creator-a_%s_user_data_dir'",
                    "config.USER_DATA_DIR = 'creator-b_%s_user_data_dir'",
                },
            )
            self.assertTrue(all("config.CDP_CONNECT_EXISTING = False" in text for text in bootstrap_texts))
            self.assertTrue(all("kwargs.setdefault('wait_until', 'domcontentloaded')" in text for text in bootstrap_texts))
            self.assertTrue(all("_page_goto_with_retry" in text for text in bootstrap_texts))

    def test_generated_mediacrawler_patch_stops_at_known_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "MediaCrawler"
            package = media / "media_platform" / "douyin"
            package.mkdir(parents=True)
            (media / "media_platform" / "__init__.py").write_text("", encoding="utf-8")
            (package / "__init__.py").write_text("", encoding="utf-8")
            (media / "config.py").write_text(
                "DY_CREATOR_ID_LIST=[]\nCRAWLER_MAX_NOTES_COUNT=0\nSAVE_DATA_OPTION='jsonl'\nSAVE_DATA_PATH=''\nMAX_CONCURRENCY_NUM=2\n",
                encoding="utf-8",
            )
            (package / "client.py").write_text(
                '''class DouYinClient:\n'''
                '''    def __init__(self):\n        self.calls = 0\n'''
                '''    async def get_user_aweme_posts(self, sec_user_id, max_cursor=""):\n'''
                '''        pages = [[\n'''
                '''            {"aweme_id": "new-3", "create_time": 103},\n'''
                '''            {"aweme_id": "new-2", "create_time": 102},\n'''
                '''            {"aweme_id": "new-1", "create_time": 101},\n'''
                '''            {"aweme_id": "known-1", "create_time": 100},\n'''
                '''            {"aweme_id": "known-2", "create_time": 99},\n'''
                '''        ]]\n'''
                '''        page = pages[self.calls] if self.calls < len(pages) else []\n'''
                '''        self.calls += 1\n'''
                '''        return {"has_more": 0, "max_cursor": "", "aweme_list": page}\n'''
                '''    async def get_all_user_aweme_posts(self, sec_user_id, callback=None):\n'''
                '''        response = await self.get_user_aweme_posts(sec_user_id)\n'''
                '''        items = response["aweme_list"]\n'''
                '''        if callback:\n            await callback(items)\n'''
                '''        return items\n''',
                encoding="utf-8",
            )
            (package / "core.py").write_text(
                '''import asyncio\nimport json\nfrom pathlib import Path\nimport config\n'''
                '''from media_platform.douyin.client import DouYinClient\n'''
                '''class _Page:\n'''
                '''    async def wait_for_load_state(self, *args, **kwargs):\n        return None\n'''
                '''class DouYinCrawler:\n'''
                '''    def __init__(self):\n        self.dy_client = DouYinClient()\n        self.context_page = _Page()\n'''
                '''    async def create_douyin_client(self, httpx_proxy):\n        return self.dy_client\n'''
                '''    async def get_aweme_detail(self, aweme_id, semaphore):\n'''
                '''        times = {"new-3": 103, "new-2": 102, "new-1": 101, "known-1": 100, "known-2": 99}\n'''
                '''        return {"aweme_id": aweme_id, "create_time": times[aweme_id], "desc": aweme_id,\n'''
                '''                "digg_count": 1, "comment_count": 2, "collect_count": 3, "share_count": 4}\n'''
                '''    async def fetch_creator_video_detail(self, video_list):\n'''
                '''        target = Path(config.SAVE_DATA_PATH) / "douyin" / "jsonl" / "creator_contents_test.jsonl"\n'''
                '''        target.parent.mkdir(parents=True, exist_ok=True)\n'''
                '''        semaphore = asyncio.Semaphore(2)\n'''
                '''        with target.open("a", encoding="utf-8") as handle:\n'''
                '''            for item in video_list:\n'''
                '''                detail = await self.get_aweme_detail(item["aweme_id"], semaphore)\n'''
                '''                if detail is not None:\n                    handle.write(json.dumps(detail) + "\\n")\n'''
                '''    async def run(self):\n'''
                '''        await self.dy_client.get_all_user_aweme_posts("creator", callback=self.fetch_creator_video_detail)\n''',
                encoding="utf-8",
            )
            (media / "main.py").write_text(
                '''import asyncio\nfrom media_platform.douyin.core import DouYinCrawler\n'''
                '''asyncio.run(DouYinCrawler().run())\n''',
                encoding="utf-8",
            )
            args = argparse.Namespace(
                media_crawler_dir=str(media),
                media_crawler_python=sys.executable,
                media_output_dir=str(root / "output"),
                clean_media_output=False,
                creator_url="creator",
                max_count=200,
                save_data_option="jsonl",
                login_type="qrcode",
                incremental_probe_count=3,
            )

            output_dir, report = COLLECTOR.run_mediacrawler(
                args, mode="incremental", known_ids={"known-1", "known-2"},
            )
            records, _ = COLLECTOR.load_records_from_output(output_dir)

            self.assertEqual(
                [item["aweme_id"] for item in COLLECTOR.dedupe_works(records)],
                ["new-3", "new-2", "new-1", "known-1"],
            )
            self.assertEqual(report["checked_count"], 4)
            self.assertEqual(report["known_boundary_aweme_id"], "known-1")
            self.assertEqual(report["stop_reason"], "known_boundary")


if __name__ == "__main__":
    unittest.main()
