import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


IMA = load_module("backup_transcripts_to_ima")
KUAKE = load_module("backup_transcripts_to_kuake")
SYNC = load_module("sync_creator_backup_mapping_to_feishu")
WRITER = load_module("feishu_transcript_writer")
OBSIDIAN = load_module("export_transcript_to_obsidian")


class ImaDirectoryMappingTest(unittest.TestCase):
    def test_missing_creator_folder_is_created_and_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mapping = Path(temp_dir) / "mapping.json"
            mapping.write_text(
                json.dumps(
                    {
                        "default": {
                            "knowledge_base_id": "kb-1",
                            "knowledge_base_name": "主知识库",
                        },
                        "creators": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def fake_api(_credentials, path, _body):
                if path.endswith("search_knowledge"):
                    return {"info_list": []}
                if path.endswith("create_folder"):
                    return {"media_id": "folder_123"}
                self.fail(path)

            with patch.object(IMA, "ima_api", side_effect=fake_api):
                target = IMA.ensure_creator_folder(
                    IMA.ImaCredentials("client", "key"), mapping, "知了"
                )

            self.assertTrue(target.folder_created)
            self.assertEqual(target.folder_id, "folder_123")
            saved = json.loads(mapping.read_text(encoding="utf-8"))
            self.assertEqual(saved["creators"][0]["folder_id"], "folder_123")


class KuakeDirectoryMappingTest(unittest.TestCase):
    def test_ensure_dir_returns_new_folder_id(self):
        listings = {
            "/": [],
            "/视频文案备份": [],
        }

        def fake_list(_exe, _credentials, path):
            return listings.get(path, [])

        def fake_run(_exe, _credentials, args):
            name, parent = args[1], args[2]
            path = f"{parent.rstrip('/')}/{name}"
            listings.setdefault(parent, []).append(
                {"dir": True, "file_name": name, "path": path, "fid": f"fid-{name}"}
            )
            return {"success": True}

        with patch.object(KUAKE, "list_dir", side_effect=fake_list), patch.object(
            KUAKE, "run_kuake", side_effect=fake_run
        ):
            result = KUAKE.ensure_dir_details(
                Path("kuake.exe"), KUAKE.KuakeCredentials(cookie="x"), "/视频文案备份/知了"
            )

        self.assertTrue(result.created)
        self.assertEqual(result.path, "/视频文案备份/知了")
        self.assertEqual(result.folder_id, "fid-知了")


class FeishuMappingPatchTest(unittest.TestCase):
    def test_search_falls_back_to_exact_field_list_match(self):
        responses = [
            {"ok": True, "data": {"data": [], "record_id_list": []}},
            {
                "ok": True,
                "data": {
                    "data": [["阿笠AIGC电商"]],
                    "record_id_list": ["rec_aligc"],
                },
            },
        ]
        with patch.object(SYNC, "run_lark", side_effect=responses):
            record_id = SYNC.search_creator_record(
                "lark-cli", "token", "table", "达人昵称", "阿笠AIGC电商", "user"
            )
        self.assertEqual(record_id, "rec_aligc")

    def test_long_secuid_skips_keyword_search_limit(self):
        secuid = "MS4wLjABAAAA" + "x" * 45
        response = {
            "ok": True,
            "data": {
                "data": [[secuid]],
                "record_id_list": ["rec_long"],
                "has_more": False,
            },
        }
        with patch.object(SYNC, "run_lark", return_value=response) as mocked:
            record_id = SYNC.search_creator_record("cli", "token", "table", "SecUID", secuid, "user")
        self.assertEqual(record_id, "rec_long")
        args = mocked.call_args.args[1]
        self.assertIn("+record-list", args)
        self.assertNotIn("+record-search", args)

    def test_creator_search_fallback_paginates_beyond_200_rows(self):
        responses = [
            {"ok": True, "data": {"data": [], "record_id_list": []}},
            {"ok": True, "data": {"data": [["别的达人"]], "record_id_list": ["rec_other"], "has_more": True}},
            {"ok": True, "data": {"data": [["目标达人"]], "record_id_list": ["rec_target"], "has_more": False}},
        ]
        with patch.object(SYNC, "run_lark", side_effect=responses) as mocked:
            record_id = SYNC.search_creator_record("cli", "token", "table", "达人昵称", "目标达人", "user")
        self.assertEqual(record_id, "rec_target")
        args = mocked.call_args_list[2].args[1]
        self.assertEqual(args[args.index("--offset") + 1], "200")

    def test_record_get_matrix_is_converted_to_field_mapping(self):
        payload = {
            "ok": True,
            "data": {
                "fields": ["达人昵称", "IMA同步状态"],
                "data": [["阿笠AIGC电商", ["已映射"]]],
                "record_id_list": ["rec_aligc"],
            },
        }
        with patch.object(SYNC, "run_lark", return_value=payload):
            fields = SYNC.get_record_fields("lark-cli", "token", "table", "rec_aligc", "user")
        self.assertEqual(fields["达人昵称"], "阿笠AIGC电商")
        self.assertEqual(fields["IMA同步状态"], ["已映射"])

    def test_empty_provider_id_does_not_clear_existing_feishu_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            metadata = Path(temp_dir) / "obsidian.json"
            metadata.write_text(
                json.dumps(
                    {
                        "platform": "obsidian",
                        "feishu_fields": {
                            "Obsidian文件夹名称": "知了",
                            "Obsidian文件夹路径": "D:/vault/知了",
                            "Obsidian文件夹ID": "",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            platform, created, fields = SYNC.load_patch(metadata)

        self.assertEqual(platform, "obsidian")
        self.assertFalse(created)
        self.assertNotIn("Obsidian文件夹ID", fields)
        self.assertEqual(fields["Obsidian文件夹名称"], "知了")

    def test_only_changed_fields_are_written(self):
        desired = {"IMA文件夹名称": "知了", "IMA文件夹ID": "folder_123"}
        existing = {"IMA文件夹名称": "知了", "IMA文件夹ID": "folder_old"}
        self.assertEqual(
            SYNC.changed_fields(desired, existing),
            {"IMA文件夹ID": "folder_123"},
        )

    def test_multiple_provider_metadata_files_merge_into_one_patch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ima = root / "ima.json"
            kuake = root / "kuake.json"
            ima.write_text(json.dumps({"platform": "ima", "feishu_fields": {"IMA文件夹ID": "ima-1"}}), encoding="utf-8")
            kuake.write_text(json.dumps({"platform": "kuake", "feishu_fields": {"夸克文件夹ID": "quark-1"}}), encoding="utf-8")
            platforms, created, fields = SYNC.load_patches([ima, kuake])
        self.assertEqual(platforms, ["ima", "kuake"])
        self.assertFalse(created)
        self.assertEqual(fields, {"IMA文件夹ID": "ima-1", "夸克文件夹ID": "quark-1"})


class FeishuTranscriptWriterFallbackTest(unittest.TestCase):
    def test_work_id_search_falls_back_to_exact_field_list_match(self):
        responses = [
            {"ok": True, "data": {"data": [], "record_id_list": []}},
            {
                "ok": True,
                "data": {
                    "data": [["7662026755036761395"]],
                    "record_id_list": ["rec_work"],
                },
            },
        ]
        with patch.object(WRITER, "run_lark", side_effect=responses):
            record_id = WRITER.find_record_by_work_id(
                "lark-cli", "token", "table", "7662026755036761395", "抖音作品ID", "user"
            )
        self.assertEqual(record_id, "rec_work")

    def test_work_search_fallback_paginates_beyond_200_rows(self):
        responses = [
            {"ok": True, "data": {"data": [], "record_id_list": []}},
            {"ok": True, "data": {"data": [["111"]], "record_id_list": ["rec_other"], "has_more": True}},
            {"ok": True, "data": {"data": [["999"]], "record_id_list": ["rec_work"], "has_more": False}},
        ]
        with patch.object(WRITER, "run_lark", side_effect=responses) as mocked:
            record_id = WRITER.find_record_by_work_id("cli", "token", "table", "999", "抖音作品ID", "user")
        self.assertEqual(record_id, "rec_work")
        args = mocked.call_args_list[2].args[1]
        self.assertEqual(args[args.index("--offset") + 1], "200")


class ObsidianDirectoryMappingTest(unittest.TestCase):
    def test_ensure_only_creates_creator_dir_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "Z_Original"
            metadata = Path(temp_dir) / "obsidian.json"
            code = OBSIDIAN.main(
                [
                    "--ensure-creator-dir-only",
                    "--creator-name", "知了-千川推商品",
                    "--creator-dir-name", "知了",
                    "--obsidian-original-dir", str(root),
                    "--metadata-output", str(metadata),
                ]
            )
            data = json.loads(metadata.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(data["directory"]["name"], "知了")
        self.assertEqual(data["feishu_fields"]["Obsidian同步状态"], "已映射")


if __name__ == "__main__":
    unittest.main()
