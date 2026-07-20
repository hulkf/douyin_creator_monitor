import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SYNC = load_module("sync_creator_backup_mapping_to_feishu")
READER = load_module("read_creator_fields_from_feishu")
OBSIDIAN = load_module("export_transcript_to_obsidian")
PIPELINE = load_module("run_creator_pipeline")


class SummaryTemplateRenderingTest(unittest.TestCase):
    def test_selected_summary_template_is_rendered_above_original_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note_template = root / "note.md"
            summary_template = root / "qianchuan.md"
            note_template.write_text(
                "# {{作品标题_文本}}\n## 原始文案\n{{原始文案}}\n",
                encoding="utf-8",
            )
            summary_template.write_text("# 千川归档逻辑\n按 ECPM 与 ROI 归类。\n", encoding="utf-8")

            note = OBSIDIAN.build_note(
                aweme_id="123",
                transcript="第一，保留原文。第二，继续保留。",
                work={"aweme_id": "123", "desc": "测试作品"},
                profile={},
                creator_name="测试达人",
                creator_dir_name="测试达人",
                transcript_path=root / "123.final.txt",
                template_path=note_template,
                summary_template_path=summary_template,
            )

        self.assertIn("## 内容总结模板", note)
        self.assertLess(note.index("# 千川归档逻辑"), note.index("## 原始文案"))
        self.assertLess(note.index("## 原始文案"), note.index("第一，保留原文。"))

    def test_note_without_summary_template_keeps_existing_layout(self):
        note = OBSIDIAN.build_note(
            aweme_id="123",
            transcript="原始正文",
            work={"aweme_id": "123", "desc": "测试作品"},
            profile={},
            creator_name="测试达人",
            creator_dir_name="测试达人",
            transcript_path=Path("123.final.txt"),
            template_path=None,
            summary_template_path=None,
        )
        self.assertNotIn("## 内容总结模板", note)
        self.assertIn("## 原始文案\n原始正文", note)

    def test_misplaced_summary_placeholder_is_moved_above_original_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note_template = root / "note.md"
            summary_template = root / "summary.md"
            note_template.write_text(
                "## 原始文案\n{{原始文案}}\n{{内容总结模板}}\n",
                encoding="utf-8",
            )
            summary_template.write_text("专用总结逻辑", encoding="utf-8")
            note = OBSIDIAN.build_note(
                aweme_id="123",
                transcript="原始正文",
                work={"aweme_id": "123", "desc": "测试作品"},
                profile={},
                creator_name="测试达人",
                creator_dir_name="测试达人",
                transcript_path=root / "123.final.txt",
                template_path=note_template,
                summary_template_path=summary_template,
            )

        self.assertLess(note.index("专用总结逻辑"), note.index("## 原始文案"))


class SummaryTemplateSelectionTest(unittest.TestCase):
    def test_creator_explicit_template_has_highest_priority(self):
        config = {
            "obsidian": {
                "summary_template_file": "default.md",
                "summary_templates_by_creator_type": {"巨量千川": "qianchuan.md"},
            }
        }
        creator = {
            "creator_type": "巨量千川",
            "summary_template_file": "creator-special.md",
        }
        selected = PIPELINE.select_summary_template_file(config, creator)
        self.assertEqual(selected, (PIPELINE.PROJECT_DIR / "creator-special.md").resolve())

    def test_feishu_creator_type_selects_qianchuan_template(self):
        config = {
            "obsidian": {
                "summary_template_file": "default.md",
                "summary_templates_by_creator_type": {"巨量千川": "qianchuan.md"},
            }
        }
        creator = {"creator_type": ["巨量千川"]}
        selected = PIPELINE.select_summary_template_file(config, creator)
        self.assertEqual(selected, (PIPELINE.PROJECT_DIR / "qianchuan.md").resolve())

    def test_obsidian_command_passes_selected_summary_template(self):
        config = {
            "python": "python",
            "obsidian": {
                "summary_templates_by_creator_type": {"巨量千川": "qianchuan.md"},
            },
        }
        creator = {"creator_name": "知了", "creator_type": "巨量千川"}
        command = PIPELINE.obsidian_command(
            config,
            creator,
            {"aweme_id": "123"},
            Path("works.json"),
            None,
            {"final": Path("123.final.txt")},
            False,
        )
        self.assertEqual(
            command[command.index("--summary-template-file") + 1],
            str((PIPELINE.PROJECT_DIR / "qianchuan.md").resolve()),
        )

    def test_feishu_creator_type_hydrates_creator_before_template_selection(self):
        config = {
            "python": "python",
            "feishu": {"creator_table_id": "tbl", "as_identity": "user"},
            "obsidian": {
                "enabled": True,
                "creator_type_field": "达人类型",
                "summary_templates_by_creator_type": {"巨量千川": "qianchuan.md"},
            },
        }
        creator = {"key": "demo", "creator_name": "知了"}
        runner = Mock(dry_run=False)
        runner.run.return_value = json.dumps(
            {"record_id": "rec123", "fields": {"达人类型": [{"text": "巨量千川"}]}},
            ensure_ascii=False,
        )
        logger = Mock()
        args = Mock(skip_obsidian=False)

        info = PIPELINE.hydrate_creator_type_from_feishu(
            config, creator, runner, logger, {}, args,
        )

        self.assertEqual(creator["creator_type"], "巨量千川")
        self.assertEqual(info["creator_type_source"], "feishu")
        self.assertTrue(info["summary_template_file"].endswith("qianchuan.md"))

    def test_feishu_read_failure_clears_stale_local_creator_type(self):
        config = {
            "python": "python",
            "feishu": {"creator_table_id": "tbl"},
            "obsidian": {
                "enabled": True,
                "summary_templates_by_creator_type": {"巨量千川": "qianchuan.md"},
            },
        }
        creator = {"key": "demo", "creator_name": "知了", "creator_type": "巨量千川"}
        runner = Mock(dry_run=False)
        runner.run.side_effect = PIPELINE.PipelineError("Feishu unavailable")

        info = PIPELINE.hydrate_creator_type_from_feishu(
            config, creator, runner, Mock(), {}, Mock(skip_obsidian=False),
        )

        self.assertNotIn("creator_type", creator)
        self.assertEqual(info["creator_type_source"], "default_fallback")
        self.assertEqual(info["summary_template_file"], "")


class FeishuCreatorFieldsReaderTest(unittest.TestCase):
    def test_reader_returns_only_requested_creator_fields(self):
        stdout = io.StringIO()
        with patch.object(READER, "load_base_token", return_value="token"), patch.object(
            READER, "search_creator_record", return_value="rec123"
        ), patch.object(
            READER,
            "get_record_fields",
            return_value={"达人类型": ["巨量千川"], "达人昵称": "知了"},
        ), contextlib.redirect_stdout(stdout):
            code = READER.main(
                [
                    "--table-id", "tbl",
                    "--match-field", "达人昵称",
                    "--match-value", "知了",
                    "--field", "达人类型",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["record_id"], "rec123")
        self.assertEqual(payload["fields"], {"达人类型": ["巨量千川"]})


if __name__ == "__main__":
    unittest.main()
