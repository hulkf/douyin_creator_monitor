import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
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

    def test_captured_creator_response_updates_profile_without_erasing_known_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "creator-profile-raw.json"
            output = root / "profile-demo-update.json"
            output.write_text(
                json.dumps({"账号简介": "保留旧简介", "粉丝数": 10}, ensure_ascii=False),
                encoding="utf-8",
            )
            capture.write_text(
                json.dumps(
                    {
                        "user": {
                            "nickname": "测试达人",
                            "unique_id": "demo123",
                            "uid": "12345678",
                            "sec_uid": "sec-demo",
                            "ip_location": "IP属地：广东",
                            "province": "广东",
                            "city": "深圳",
                            "following_count": 0,
                            "follower_count": 123,
                            "total_favorited": "456",
                            "aweme_count": 7,
                            "signature": "",
                            "gender": 2,
                            "avatar_larger": {"url_list": ["https://example.com/avatar.jpg"]},
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = COLLECTOR.update_profile_from_capture(
                capture,
                output,
                "https://www.douyin.com/user/sec-demo",
            )
            profile = json.loads(output.read_text(encoding="utf-8"))

            self.assertTrue(result["updated"])
            self.assertEqual(profile["达人昵称"], "测试达人")
            self.assertEqual(profile["账号ID"], "demo123")
            self.assertEqual(profile["抖音UID"], "12345678")
            self.assertEqual(profile["IP属地"], "广东")
            self.assertEqual(profile["所在地区"], "广东·深圳")
            self.assertEqual(profile["性别"], "女")
            self.assertEqual(profile["关注数"], 0)
            self.assertEqual(profile["粉丝数"], 123)
            self.assertEqual(profile["获赞数"], 456)
            self.assertEqual(profile["作品数"], 7)
            self.assertEqual(profile["账号简介"], "保留旧简介")
            self.assertEqual(profile["头像URL"], "https://example.com/avatar.jpg")
            self.assertEqual(profile["主页采集状态"], "正常")
            self.assertEqual(profile["账号状态"], "正常")

    def test_ip_location_fills_region_when_profile_has_no_province_or_city(self):
        payload = {
            "user": {
                "nickname": "测试达人",
                "unique_id": "demo123",
                "uid": "12345678",
                "sec_uid": "sec-demo",
                "ip_location": "IP属地：广东",
                "following_count": 0,
                "follower_count": 123,
                "total_favorited": 456,
                "aweme_count": 7,
            }
        }

        profile = COLLECTOR.normalize_creator_profile_response(
            payload,
            "https://www.douyin.com/user/sec-demo",
            captured_at="2026-07-23 09:00:00",
        )

        self.assertIsNotNone(profile)
        self.assertEqual(profile["IP属地"], "广东")
        self.assertEqual(profile["所在地区"], "广东")

    def test_partial_profile_capture_does_not_refresh_official_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "creator-profile-raw.json"
            output = root / "profile-demo-update.json"
            original = {
                    "达人昵称": "旧昵称", "关注数": 1, "粉丝数": 99,
                    "获赞数": 2, "作品数": 3, "最近检查时间": "2026-01-01 00:00:00",
                }
            output.write_text(
                json.dumps(original, ensure_ascii=False),
                encoding="utf-8",
            )
            capture.write_text(
                json.dumps({"user": {
                    "nickname": "新昵称", "following_count": 4,
                    "total_favorited": 5, "aweme_count": 6,
                }}, ensure_ascii=False),
                encoding="utf-8",
            )

            result = COLLECTOR.update_profile_from_capture(
                capture, output, "https://www.douyin.com/user/sec-demo",
            )
            profile = json.loads(output.read_text(encoding="utf-8"))
            diagnostic = output.with_suffix(".partial.json")

            self.assertFalse(result["updated"])
            self.assertTrue(result["partial"])
            self.assertEqual(profile, original)
            self.assertIn("粉丝数", result["missing_core_fields"])
            self.assertTrue(diagnostic.is_file())
            self.assertEqual(
                json.loads(diagnostic.read_text(encoding="utf-8"))["达人昵称"],
                "新昵称",
            )

    def test_collection_mode_requires_matching_completed_baseline(self):
        works = [{"aweme_id": "1"}]
        state = {"creator_id": "creator-1", "full_history_collected": True}
        self.assertEqual(COLLECTOR.determine_collection_mode(state, "creator-1", works, False), "incremental")
        self.assertEqual(COLLECTOR.determine_collection_mode(state, "creator-2", works, False), "full")
        self.assertEqual(COLLECTOR.determine_collection_mode({}, "creator-1", works, False), "full")
        self.assertEqual(COLLECTOR.determine_collection_mode(state, "creator-1", works, True), "full")
        self.assertEqual(COLLECTOR.determine_collection_mode(state, "creator-1", [], False), "full")

    def test_collect_refreshes_profile_from_the_same_mediacrawler_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "run"
            works_dir = source / "douyin" / "jsonl"
            works_dir.mkdir(parents=True)
            (works_dir / "creator_contents_test.jsonl").write_text(
                json.dumps({
                    "aweme_id": "work-1", "create_time": 100,
                    "digg_count": 1, "comment_count": 2,
                    "collect_count": 3, "share_count": 4,
                }) + "\n",
                encoding="utf-8",
            )
            (source / "_creator_profile_raw.json").write_text(
                json.dumps({"user": {
                    "nickname": "同次采集达人", "unique_id": "same-run",
                    "sec_uid": "creator-1", "ip_location": "IP属地：广东",
                    "province": "广东", "city": "深圳", "following_count": 5,
                    "follower_count": 6, "total_favorited": 7, "aweme_count": 8,
                }}, ensure_ascii=False),
                encoding="utf-8",
            )
            profile_output = root / "profile.json"
            args = argparse.Namespace(
                output_file=str(root / "works.json"),
                collection_state_file=str(root / "collection-state.json"),
                creator_url="https://www.douyin.com/user/creator-1",
                min_publish_date=None,
                mark_existing_full=False,
                force_full_collect=False,
                normalize_only=False,
                media_output_dir=str(root / "media-output"),
                media_crawler_dir=None,
                expect_min_count=1,
                profile_output_file=str(profile_output),
            )

            with patch.object(
                COLLECTOR, "run_mediacrawler",
                return_value=(source, {"mode": "full", "stop_reason": "history_exhausted"}),
            ):
                result = COLLECTOR.collect(args)

            profile = json.loads(profile_output.read_text(encoding="utf-8"))
            self.assertEqual(result["profile_update"]["updated"], True)
            self.assertEqual(profile["达人昵称"], "同次采集达人")
            self.assertEqual(profile["粉丝数"], 6)
            self.assertEqual(profile["获赞数"], 7)

    def test_normalize_only_does_not_present_old_capture_as_fresh_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_output = root / "media-output"
            older = media_output / "runs" / "older"
            latest = media_output / "runs" / "latest"
            works_dir = media_output / "douyin" / "jsonl"
            older.mkdir(parents=True)
            latest.mkdir(parents=True)
            works_dir.mkdir(parents=True)
            (works_dir / "creator_contents_test.jsonl").write_text(
                json.dumps({
                    "aweme_id": "work-1", "create_time": 100,
                    "digg_count": 1, "comment_count": 2,
                    "collect_count": 3, "share_count": 4,
                }) + "\n",
                encoding="utf-8",
            )
            for path, nickname in ((older, "旧捕获"), (latest, "最新捕获")):
                capture = path / "_creator_profile_raw.json"
                capture.write_text(json.dumps({"user": {
                    "nickname": nickname, "following_count": 1, "follower_count": 2,
                    "total_favorited": 3, "aweme_count": 4,
                }}, ensure_ascii=False), encoding="utf-8")
            profile_output = root / "profile.json"
            args = argparse.Namespace(
                output_file=str(root / "works.json"), collection_state_file=None,
                creator_url="https://www.douyin.com/user/creator-1",
                min_publish_date=None, mark_existing_full=False, force_full_collect=False,
                normalize_only=True, media_output_dir=str(media_output), media_crawler_dir=None,
                expect_min_count=1, profile_output_file=str(profile_output),
            )

            result = COLLECTOR.collect(args)

            self.assertFalse(result["profile_update"]["updated"])
            self.assertTrue(result["profile_update"]["skipped"])
            self.assertFalse(profile_output.exists())


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
                    min_publish_date="2025-01-01",
                    headless=False,
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
            self.assertTrue(all("requested_headless = False" in text for text in bootstrap_texts))
            self.assertTrue(all("config.HEADLESS = requested_headless and not interactive_login" in text for text in bootstrap_texts))
            self.assertTrue(all("config.CDP_HEADLESS = requested_headless and not interactive_login" in text for text in bootstrap_texts))
            self.assertTrue(all("'--headless', 'true' if requested_headless and not interactive_login else 'false'" in text for text in bootstrap_texts))
            self.assertTrue(all("--no-startup-window" in text for text in bootstrap_texts))
            self.assertTrue(all("_close_new_blank_chrome_windows" in text for text in bootstrap_texts))
            self.assertTrue(all("subprocess.CREATE_NO_WINDOW" in text for text in bootstrap_texts))
            self.assertTrue(all("INTERACTIVE_LOGIN_EXIT_CODE" in text for text in bootstrap_texts))
            self.assertTrue(all("kwargs.setdefault('wait_until', 'domcontentloaded')" in text for text in bootstrap_texts))
            self.assertTrue(all("_page_goto_with_retry" in text for text in bootstrap_texts))
            self.assertTrue(all("_capture_get_user_info" in text for text in bootstrap_texts))
            self.assertTrue(all("_creator_profile_raw.json" in text for text in bootstrap_texts))
            self.assertTrue(all("cutoff_timestamp = 1735660800" in text for text in bootstrap_texts))
            self.assertTrue(all("stop_reason = 'min_publish_date'" in text for text in bootstrap_texts))

    def test_mediacrawler_command_bypasses_relocated_venv_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "MediaCrawler"
            greenlet_dir = media / ".venv" / "Lib" / "site-packages" / "greenlet"
            greenlet_dir.mkdir(parents=True)
            abi_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
            (greenlet_dir / f"_greenlet.{abi_tag}-win_amd64.pyd").write_bytes(b"")
            bootstrap = media / "bootstrap.py"

            with patch.object(COLLECTOR.os, "name", "nt"):
                command = COLLECTOR.build_mediacrawler_command(
                    media, str(media / ".venv" / "Scripts" / "python.exe"), bootstrap,
                )

            self.assertEqual(command[:3], [sys.executable, "-S", "-c"])
            self.assertEqual(command[-1], str(bootstrap))
            self.assertIn(repr(str(media / ".venv" / "Lib" / "site-packages")), command[3])

    def test_mediacrawler_failure_still_closes_its_isolated_chrome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "MediaCrawler"
            media.mkdir()
            (media / "main.py").write_text("", encoding="utf-8")
            args = argparse.Namespace(
                media_crawler_dir=str(media),
                media_crawler_python=sys.executable,
                media_output_dir=str(root / "output"),
                clean_media_output=False,
                creator_url="creator-a",
                max_count=200,
                save_data_option="jsonl",
                login_type="qrcode",
                incremental_probe_count=3,
                browser_profile_key="creator-a",
                cdp_port=9222,
                min_publish_date="2025-01-01",
            )

            with patch.object(
                COLLECTOR.subprocess, "run", return_value=argparse.Namespace(returncode=1)
            ), patch.object(COLLECTOR, "cleanup_mediacrawler_chrome") as cleanup:
                with self.assertRaises(SystemExit):
                    COLLECTOR.run_mediacrawler(args, mode="incremental", known_ids=set())

            cleanup.assert_called_once_with("creator-a", 9222)

    def test_browser_profile_lock_rejects_a_second_user_of_the_same_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "MediaCrawler"
            media.mkdir()

            with COLLECTOR.browser_profile_lock(media, "account-a"):
                with self.assertRaises(SystemExit):
                    with COLLECTOR.browser_profile_lock(media, "account-a"):
                        pass

    def test_mediacrawler_reopens_visibly_only_when_login_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "MediaCrawler"
            media.mkdir()
            (media / "main.py").write_text("", encoding="utf-8")
            args = argparse.Namespace(
                media_crawler_dir=str(media),
                media_crawler_python=sys.executable,
                media_output_dir=str(root / "output"),
                clean_media_output=False,
                creator_url="creator-a",
                max_count=200,
                save_data_option="jsonl",
                login_type="qrcode",
                incremental_probe_count=3,
                browser_profile_key="creator-a",
                cdp_port=9222,
                min_publish_date="2025-01-01",
            )
            calls = []

            def run_external(command, **kwargs):
                calls.append(kwargs["env"].get(COLLECTOR.INTERACTIVE_LOGIN_ENV))
                code = COLLECTOR.INTERACTIVE_LOGIN_EXIT_CODE if len(calls) == 1 else 0
                return argparse.Namespace(returncode=code)

            with patch.object(COLLECTOR.subprocess, "run", side_effect=run_external), patch.object(
                COLLECTOR, "cleanup_mediacrawler_chrome",
            ) as cleanup:
                COLLECTOR.run_mediacrawler(args, mode="incremental", known_ids=set())

            self.assertEqual(calls, [None, "1"])
            self.assertEqual(cleanup.call_count, 2)
            cleanup.assert_called_with("creator-a", 9222)

    def test_chrome_cleanup_does_not_close_unrelated_user_browser(self):
        class FakeProcess:
            def __init__(self, pid, command_line):
                self.pid = pid
                self.info = {"pid": pid, "name": "chrome.exe", "cmdline": command_line}
                self.terminated = False

            def children(self, recursive=False):
                return []

            def terminate(self):
                self.terminated = True

            def kill(self):
                raise AssertionError("graceful termination should be enough")

        project_chrome = FakeProcess(
            101,
            ["chrome.exe", "--user-data-dir=D:/MediaCrawler/browser_data/cdp_creator-a_dy_user_data_dir"],
        )
        user_chrome = FakeProcess(202, ["chrome.exe", "--profile-directory=Default"])
        fake_psutil = argparse.Namespace(
            NoSuchProcess=RuntimeError,
            AccessDenied=PermissionError,
            process_iter=lambda attrs: [project_chrome, user_chrome],
            wait_procs=lambda processes, timeout: (processes, []),
        )

        with patch.object(COLLECTOR.os, "name", "nt"), patch.dict(
            sys.modules, {"psutil": fake_psutil}
        ):
            closed = COLLECTOR.cleanup_mediacrawler_chrome("creator-a", 9222)

        self.assertEqual(closed, [101])
        self.assertTrue(project_chrome.terminated)
        self.assertFalse(user_chrome.terminated)

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
                '''    async def get_user_info(self, sec_user_id):\n'''
                '''        return {"user": {"nickname": "同次采集达人", "unique_id": "same-run",\n'''
                '''                "sec_uid": sec_user_id, "ip_location": "IP属地：广东",\n'''
                '''                "province": "广东", "city": "深圳",\n'''
                '''                "following_count": 5, "follower_count": 6,\n'''
                '''                "total_favorited": 7, "aweme_count": 8}}\n'''
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
            (package / "login.py").write_text(
                "class DouYinLogin:\n    async def begin(self):\n        return None\n",
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
                '''        await self.dy_client.get_user_info("creator")\n'''
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
            captured_profile = json.loads(
                (output_dir / "_creator_profile_raw.json").read_text(encoding="utf-8")
            )
            self.assertEqual(captured_profile["user"]["nickname"], "同次采集达人")

    def test_cli_returns_two_when_current_profile_is_partial(self):
        payload = {
            "count": 1, "collection_mode": "full", "new_count": 1, "pending_count": 1,
            "profile_update": {
                "updated": False, "partial": True, "missing_core_fields": ["粉丝数"],
            },
        }
        argv = [
            "collector", "--creator-url", "creator",
            "--profile-output-file", "profile.json",
        ]
        with patch.object(COLLECTOR, "collect", return_value=payload), patch.object(sys, "argv", argv):
            self.assertEqual(COLLECTOR.main(), 2)


if __name__ == "__main__":
    unittest.main()
