import argparse
import contextlib
import importlib.util
import io
import json
import os
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
SUMMARY = load_module("generate_transcript_summary")
PIPELINE = load_module("run_creator_pipeline")


class SummaryTemplateRenderingTest(unittest.TestCase):
    def test_summary_file_is_rendered_above_original_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note_template = root / "note.md"
            summary_file = root / "summary.md"
            note_template.write_text(
                "# {{作品标题_文本}}\n## 原始文案\n{{原始文案}}\n",
                encoding="utf-8",
            )
            summary_file.write_text("### 📥 归档卡片\n已填写内容。\n", encoding="utf-8")

            note = OBSIDIAN.build_note(
                aweme_id="123",
                transcript="第一，保留原文。第二，继续保留。",
                work={"aweme_id": "123", "desc": "测试作品"},
                profile={},
                creator_name="测试达人",
                creator_dir_name="测试达人",
                transcript_path=root / "123.final.txt",
                template_path=note_template,
                summary_path=summary_file,
            )

        self.assertIn("## 内容总结", note)
        self.assertLess(note.index("### 📥 归档卡片"), note.index("## 原始文案"))
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
            summary_path=None,
            summary_template_path=None,
        )
        self.assertNotIn("## 内容总结", note)
        self.assertIn("## 原始文案\n原始正文", note)

    def test_misplaced_summary_placeholder_is_moved_above_original_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note_template = root / "note.md"
            summary_file = root / "summary.md"
            note_template.write_text(
                "## 原始文案\n{{原始文案}}\n{{内容总结模板}}\n",
                encoding="utf-8",
            )
            summary_file.write_text("专用总结内容", encoding="utf-8")
            note = OBSIDIAN.build_note(
                aweme_id="123",
                transcript="原始正文",
                work={"aweme_id": "123", "desc": "测试作品"},
                profile={},
                creator_name="测试达人",
                creator_dir_name="测试达人",
                transcript_path=root / "123.final.txt",
                template_path=note_template,
                summary_path=summary_file,
            )

        self.assertLess(note.index("专用总结内容"), note.index("## 原始文案"))

    def test_summary_template_file_is_not_rendered_as_summary_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_template = root / "prompt.md"
            summary_template.write_text("## 你的角色与任务\n完整提示词规则", encoding="utf-8")
            note = OBSIDIAN.build_note(
                aweme_id="123",
                transcript="原始正文",
                work={"aweme_id": "123", "desc": "测试作品"},
                profile={},
                creator_name="测试达人",
                creator_dir_name="测试达人",
                transcript_path=root / "123.final.txt",
                template_path=None,
                summary_path=None,
                summary_template_path=summary_template,
            )

        self.assertNotIn("你的角色与任务", note)
        self.assertNotIn("完整提示词规则", note)
        self.assertIn("## 原始文案\n原始正文", note)


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

    def test_obsidian_command_passes_existing_summary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary_file = root / "123.summary.md"
            summary_file.write_text("已填写卡片", encoding="utf-8")
            command = PIPELINE.obsidian_command(
                {"python": "python"},
                {"creator_name": "知了"},
                {"aweme_id": "123"},
                root / "works.json",
                None,
                {"final": root / "123.final.txt", "summary": summary_file},
                False,
            )

        self.assertEqual(command[command.index("--summary-file") + 1], str(summary_file))

    def test_summary_command_uses_selected_template_and_output_path(self):
        command = PIPELINE.summary_command(
            {
                "python": "python",
                "summary": {
                    "model": "gpt-test",
                    "api_key_env": "SUMMARY_KEY",
                    "thinking": "disabled",
                },
            },
            {"creator_name": "知了"},
            {"aweme_id": "123", "desc": "千川素材不消耗，怎么办"},
            {"final": Path("123.final.txt"), "summary": Path("123.summary.md")},
            Path("qianchuan.md"),
        )

        self.assertIn("generate_transcript_summary.py", command[1])
        self.assertEqual(command[command.index("--template-file") + 1], "qianchuan.md")
        self.assertEqual(command[command.index("--output") + 1], "123.summary.md")
        self.assertEqual(command[command.index("--model") + 1], "gpt-test")
        self.assertEqual(command[command.index("--thinking") + 1], "disabled")

    def test_process_downstream_generates_summary_before_obsidian_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = {"key": "a", "creator_name": "达人 A", "creator_type": "巨量千川"}
            work = {"aweme_id": "123", "desc": "测试标题"}
            media_dir = root / "media"
            paths = PIPELINE.artifact_paths(media_dir, "123", "volcengine")
            paths["final"].parent.mkdir(parents=True, exist_ok=True)
            paths["final"].write_text("原文", encoding="utf-8")
            template = root / "qianchuan.md"
            template.write_text("提示词", encoding="utf-8")
            runner = Mock()

            def run(label, command, env, **unused):
                if "内容总结" in label:
                    paths["summary"].write_text("已填写卡片", encoding="utf-8")
                return ""

            runner.run.side_effect = run
            args = Mock(
                dry_run=False,
                resume=False,
                force_stage=set(),
                skip_feishu_writeback=True,
                skip_ima=True,
                skip_kuake=True,
                skip_obsidian=False,
                feishu_only=False,
                overwrite=True,
                backup_workers=1,
                fail_fast=False,
                _delivery_breakers={},
            )

            with patch.dict("os.environ", {"SUMMARY_KEY": "secret"}):
                result = PIPELINE.process_downstream(
                    {
                        "python": "python",
                        "summary": {"model": "gpt-test", "api_key_env": "SUMMARY_KEY"},
                        "obsidian": {"summary_templates_by_creator_type": {"巨量千川": str(template)}},
                    },
                    creator,
                    work,
                    root / "works.json",
                    None,
                    root / "state",
                    media_dir,
                    runner,
                    PIPELINE.Logger(root / "log.txt", persist=False),
                    {},
                    args,
                    {"aweme_id": "123", "title": "测试标题", "stages": {"corrected": "success"}},
                )

        self.assertEqual(result["stages"]["summarized"], "success")
        self.assertEqual(result["stages"]["obsidian_exported"], "success")
        labels = [call.args[0] for call in runner.run.call_args_list]
        self.assertLess(labels.index("内容总结 123"), labels.index("备份 Obsidian 123"))

    def test_summary_capability_requires_model_and_api_key_and_is_stable_for_run(self):
        args = argparse.Namespace()
        config = {"summary": {"model": "gpt-test", "api_key_env": "SUMMARY_KEY"}}
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(PIPELINE.summary_capability(config, args), "none")
        args = argparse.Namespace()
        with patch.dict("os.environ", {"SUMMARY_KEY": "secret"}, clear=True):
            self.assertEqual(PIPELINE.summary_capability(config, args), "llm")
            del os.environ["SUMMARY_KEY"]
            self.assertEqual(PIPELINE.summary_capability(config, args), "llm")

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


class GenerateTranscriptSummaryTest(unittest.TestCase):
    def test_build_messages_includes_metadata_and_transcript(self):
        messages = SUMMARY.build_messages(
            template="系统提示词",
            transcript="千川没有消耗就一键起量。",
            creator_name="阿笠AIGC电商",
            title="千川素材不消耗，怎么办",
            aweme_id="766",
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], "系统提示词")
        self.assertIn("只输出最终归档卡片", messages[1]["content"])
        self.assertIn("阿笠AIGC电商", messages[1]["content"])
        self.assertIn("千川没有消耗就一键起量。", messages[1]["content"])

    def test_main_writes_model_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "final.txt"
            template = root / "prompt.md"
            output = root / "summary.md"
            transcript.write_text("原文", encoding="utf-8")
            template.write_text("提示词", encoding="utf-8")
            with patch.dict("os.environ", {"SUMMARY_KEY": "secret"}), patch.object(
                SUMMARY, "request_chat_completion", return_value="### 📥 归档卡片\n已填写"
            ):
                code = SUMMARY.main([
                    "--transcript", str(transcript),
                    "--template-file", str(template),
                    "--output", str(output),
                    "--model", "gpt-test",
                    "--api-key-env", "SUMMARY_KEY",
                ])

            self.assertEqual(code, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "### 📥 归档卡片\n已填写\n")

    def test_transient_model_failure_is_retried(self):
        response = Mock()
        response.read.return_value = json.dumps({
            "choices": [{"message": {"content": "重试成功"}}]
        }, ensure_ascii=False).encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(
            SUMMARY.urllib.request, "urlopen",
            side_effect=[SUMMARY.urllib.error.URLError("temporary"), response],
        ) as urlopen, patch.object(SUMMARY.time, "sleep"):
            value = SUMMARY.request_chat_completion(
                messages=[], model="m", api_key="k", base_url="https://example.invalid/v1",
                temperature=0.2, max_tokens=100, timeout=1, max_attempts=3,
            )

        self.assertEqual(value, "重试成功")
        self.assertEqual(urlopen.call_count, 2)

    def test_truncated_reasoning_response_retries_with_more_tokens_and_thinking_disabled(self):
        truncated = Mock()
        truncated.read.return_value = json.dumps({
            "choices": [{
                "finish_reason": "length",
                "message": {"content": "", "reasoning_content": "思考耗尽预算"},
            }]
        }, ensure_ascii=False).encode("utf-8")
        truncated.__enter__ = Mock(return_value=truncated)
        truncated.__exit__ = Mock(return_value=False)
        complete = Mock()
        complete.read.return_value = json.dumps({
            "choices": [{"finish_reason": "stop", "message": {"content": "完整总结"}}]
        }, ensure_ascii=False).encode("utf-8")
        complete.__enter__ = Mock(return_value=complete)
        complete.__exit__ = Mock(return_value=False)

        with patch.object(
            SUMMARY.urllib.request, "urlopen", side_effect=[truncated, complete],
        ) as urlopen, patch.object(SUMMARY.time, "sleep"):
            value = SUMMARY.request_chat_completion(
                messages=[], model="m", api_key="k", base_url="https://example.invalid/v1",
                temperature=0.2, max_tokens=100, timeout=1, max_attempts=3,
                thinking="disabled",
            )

        first_payload = json.loads(urlopen.call_args_list[0].args[0].data)
        second_payload = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertEqual(value, "完整总结")
        self.assertEqual(first_payload["thinking"], {"type": "disabled"})
        self.assertEqual(first_payload["max_tokens"], 100)
        self.assertEqual(second_payload["max_tokens"], 1124)


if __name__ == "__main__":
    unittest.main()
