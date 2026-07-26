#!/usr/bin/env python3
"""补充缺失的「内容总结」模块到已有的 Obsidian 笔记（独立固化模块）。

本脚本只负责一类情况，且与其它任何内容写入错误完全隔离：
    「内容总结模板缺失」——即 Obsidian 笔记里没有 ## 内容总结 段落。
它不处理 ASR、飞书回写、网盘备份等任何其它错误。

触发条件（必须全部满足才补充某一篇作品）：
  (a) 识别到该作品笔记缺失 ## 内容总结 模块；
  (b) 该达人配置了内容总结模板（应当补充）；
  (c) 当前具备模型能力（summary_capability == "llm"，即已配置 summary.model）。

缺少模型能力时，脚本会明确拒绝补充并退出，绝不静默跳过或伪装完成。

解析/类型注入逻辑通过复用 run_creator_pipeline 的权威函数完成
（select_summary_template_file / summary_capability / 飞书 creator_type 注入），
保证与主流水线行为完全一致、不漂移。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# 复用主流水线的权威解析函数（脚本与本脚本同处 scripts/ 目录，可直接导入）。
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import run_creator_pipeline as pipe  # noqa: E402

PROJECT_DIR = pipe.PROJECT_DIR
DEFAULT_STATE_DIR = pipe.DEFAULT_STATE_DIR
DEFAULT_MEDIA_DIR = pipe.DEFAULT_MEDIA_DIR
BEIJING_TZ = pipe.BEIJING_TZ
SUMMARY_TEMPLATE_MISSING = pipe.SUMMARY_TEMPLATE_MISSING
SUMMARY_HEADING_RE = re.compile(r"(?m)^##\s*内容总结\s*$")


# --------------------------------------------------------------------------- #
# 笔记检测
# --------------------------------------------------------------------------- #
def note_has_summary(note_path: Path) -> bool:
    if not note_path.is_file():
        return False
    try:
        text = note_path.read_text(encoding="utf-8-sig")
    except OSError:
        return False
    return bool(SUMMARY_HEADING_RE.search(text))


def find_note(original_dir: Path, creator_dir_name: str, aweme_id: str) -> Path | None:
    creator_dir = original_dir / pipe.safe_key(creator_dir_name or "未知达人")
    if not creator_dir.is_dir():
        return None
    matches = sorted(creator_dir.glob(f"*_{aweme_id}_*.md"))
    return matches[0] if matches else None


def index_creator_notes(original_dir: Path, creator_dir_name: str) -> dict[str, Path]:
    """Scan one creator directory once and index notes by aweme id."""
    creator_dir = original_dir / pipe.safe_key(creator_dir_name or "未知达人")
    if not creator_dir.is_dir():
        return {}
    index: dict[str, Path] = {}
    for path in sorted(creator_dir.glob("*.md")):
        parts = path.stem.split("_", 2)
        if len(parts) >= 3 and parts[1]:
            index.setdefault(parts[1], path)
    return index


# --------------------------------------------------------------------------- #
# 类型注入（与主流水线 hydrate_creator_type_from_feishu 行为一致：从飞书读达人类型）
# --------------------------------------------------------------------------- #
def hydrate_creator_type(config: dict[str, Any], creator: dict[str, Any]) -> None:
    """把飞书《达人基础信息表》的「达人类型」注入 creator，使模板解析与主流水线一致。"""
    obsidian = pipe.section(config, "obsidian")
    mappings = obsidian.get("summary_templates_by_creator_type")
    if not isinstance(mappings, dict) or not mappings:
        return
    if not bool(obsidian.get("enabled", True)):
        return
    if creator.get("summary_template_file") not in (None, ""):
        return
    field_name = str(obsidian.get("creator_type_field") or "达人类型").strip()
    if not field_name:
        return
    try:
        command = pipe.creator_fields_command(config, creator, [field_name])
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            return
        payload = json.loads(result.stdout)
        fields = payload.get("fields") if isinstance(payload, dict) else None
        if not isinstance(fields, dict):
            return
        remote_type = pipe.field_text(fields.get(field_name))
        if remote_type:
            creator["creator_type"] = remote_type
    except Exception:
        # 注入失败则保持 creator_type 为空，沿用通用/默认模板（与主流水线 fallback 一致）
        pass


# --------------------------------------------------------------------------- #
# 外部脚本调用
# --------------------------------------------------------------------------- #
def run_script(script_name: str, *args: str, dry_run: bool = False) -> subprocess.CompletedProcess:
    command = [sys.executable, str(SCRIPTS_DIR / script_name), *args]
    if dry_run:
        print(f"    [dry-run] {subprocess.list2cmdline(command)}")
        return subprocess.CompletedProcess(command, 0, "", "")
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
    )


def generate_summary(
    *, transcript: Path, template_file: Path, output: Path,
    creator_name: str, title: str, aweme_id: str,
    summary_cfg: dict[str, Any], dry_run: bool,
) -> bool:
    args = [
        "generate_transcript_summary.py",
        "--transcript", str(transcript),
        "--template-file", str(template_file),
        "--output", str(output),
        "--creator-name", creator_name,
        "--title", title,
        "--aweme-id", aweme_id,
    ]
    model = str(summary_cfg.get("model") or os.environ.get("SUMMARY_LLM_MODEL", "")).strip()
    base_url = str(summary_cfg.get("base_url") or os.environ.get("SUMMARY_LLM_BASE_URL", "")).strip()
    api_key_env = str(summary_cfg.get("api_key_env") or os.environ.get("SUMMARY_LLM_API_KEY_ENV", "OPENAI_API_KEY")).strip()
    if model:
        args += ["--model", model]
    if base_url:
        args += ["--base-url", base_url]
    if api_key_env:
        args += ["--api-key-env", api_key_env]
    if summary_cfg.get("temperature") not in (None, ""):
        args += ["--temperature", str(summary_cfg["temperature"])]
    if summary_cfg.get("max_tokens") not in (None, ""):
        args += ["--max-tokens", str(summary_cfg["max_tokens"])]
    if summary_cfg.get("timeout") not in (None, ""):
        args += ["--timeout", str(summary_cfg["timeout"])]
    if summary_cfg.get("retry_attempts") not in (None, ""):
        args += ["--retry-attempts", str(summary_cfg["retry_attempts"])]
    if summary_cfg.get("thinking") not in (None, ""):
        args += ["--thinking", str(summary_cfg["thinking"])]
    if summary_cfg.get("retry_base_seconds") not in (None, ""):
        args += ["--retry-base-seconds", str(summary_cfg["retry_base_seconds"])]
    result = run_script(*args, dry_run=dry_run)
    if dry_run:
        return True
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip().replace("\n", " ")
        print(f"  ✗ 生成总结失败 {aweme_id}: {err[:200]}", file=sys.stderr)
        return False
    return True


def merge_summary_into_note(
    *, transcript: Path, aweme_id: str, works_file: Path,
    creator_name: str, creator_dir_name: str, original_dir: Path,
    obsidian_template: Path | None, summary_file: Path, dry_run: bool,
) -> bool:
    args = [
        "export_transcript_to_obsidian.py",
        "--transcript", str(transcript),
        "--aweme-id", aweme_id,
        "--works-file", str(works_file),
        "--creator-name", creator_name,
        "--creator-dir-name", creator_dir_name,
        "--obsidian-original-dir", str(original_dir),
        "--summary-file", str(summary_file),
        "--overwrite",
    ]
    if obsidian_template is not None:
        args += ["--template-file", str(obsidian_template)]
    result = run_script(*args, dry_run=dry_run)
    if dry_run:
        return True
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip().replace("\n", " ")
        print(f"  ✗ 合并笔记失败 {aweme_id}: {err[:200]}", file=sys.stderr)
        return False
    return True


def write_feishu_local_status(
    *, table_id: str, aweme_id: str, local_status: str, dry_run: bool,
) -> bool:
    args = [
        "feishu_work_status_writer.py",
        "--table-id", table_id,
        "--work-id", aweme_id,
        "--local-status", local_status,
    ]
    result = run_script(*args, dry_run=dry_run)
    if dry_run:
        return True
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip().replace("\n", " ")
        print(f"  ✗ 回写飞书状态失败 {aweme_id}: {err[:200]}", file=sys.stderr)
        return False
    return True


def title_of(work: dict[str, Any]) -> str:
    raw = work.get("raw") if isinstance(work.get("raw"), dict) else {}
    value = str(raw.get("title") or work.get("title") or work.get("desc") or work.get("aweme_id") or "")
    return value[:60] or str(work.get("aweme_id") or "未命名作品")


# --------------------------------------------------------------------------- #
# 单个达人处理
# --------------------------------------------------------------------------- #
def supplement_creator(
    config: dict[str, Any], creator: dict[str, Any], args: argparse.Namespace,
) -> dict[str, int]:
    counters = {"supplemented": 0, "reconciled": 0, "skipped": 0, "failed": 0}
    key = pipe.creator_key(creator)

    # 触发条件 (b)：该达人应补充（配置了总结模板）。先注入飞书达人类型再解析模板，
    # 行为与主流水线完全一致，避免 creator_type 不在 pipeline.json 时漏判。
    hydrate_creator_type(config, creator)
    summary_template_file = pipe.select_summary_template_file(config, creator)
    if summary_template_file is None:
        print(f"[跳过] 达人 {key}：未配置内容总结模板（属于另一类情况，非「模板缺失」）。")
        return counters
    if not (bool(pipe.section(config, "obsidian").get("enabled", True))
            and bool(pipe.section(config, "summary").get("enabled", True))):
        print(f"[跳过] 达人 {key}：obsidian 或 summary 未启用。")
        return counters

    # 触发条件 (c)：当前具备模型能力
    capability = pipe.summary_capability(config, args)
    if capability != "llm" and args.force:
        print(f"[拒绝] 达人 {key}：当前不具备模型能力（summary.model 未配置），无法补充内容总结。"
              f"请先在 pipeline.json 的 summary 段配置模型后重试。")
        counters["failed"] += 1
        return counters

    works_file = pipe.path_from(creator.get("works_file"))
    if not works_file or not works_file.is_file():
        print(f"[跳过] 达人 {key}：找不到作品文件 {works_file}。")
        return counters

    obsidian = pipe.section(config, "obsidian")
    original_dir = pipe.path_from(obsidian.get("original_dir"))
    if original_dir is None:
        print(f"[跳过] 达人 {key}：obsidian.original_dir 未配置。")
        return counters
    creator_dir_name = str(creator.get("creator_dir_name") or creator.get("creator_name") or "")
    obsidian_template = pipe.path_from(obsidian.get("template_file"))
    summary_cfg = pipe.section(config, "summary")
    state_dir = pipe.path_from(config.get("state_dir"), DEFAULT_STATE_DIR) or DEFAULT_STATE_DIR
    media_dir = pipe.path_from(config.get("media_dir"), DEFAULT_MEDIA_DIR) or DEFAULT_MEDIA_DIR
    provider = str(pipe.chosen(creator, pipe.section(config, "asr"), "provider", "volcengine"))
    table_id = str(creator.get("works_table_id") or "").strip()
    creator_name = str(creator.get("creator_name") or creator_dir_name or "")

    works = pipe.read_json(works_file).get("works") or []
    if not isinstance(works, list):
        print(f"[跳过] 达人 {key}：作品文件缺少 works 数组。")
        return counters

    print(f"[处理] 达人 {key}：共 {len(works)} 篇作品，检查内容总结模块。")
    note_index = index_creator_notes(original_dir, creator_dir_name)
    for work in works:
        if not isinstance(work, dict):
            continue
        aweme_id = str(work.get("aweme_id") or "")
        if not aweme_id:
            continue
        paths = pipe.artifact_paths(media_dir, aweme_id, provider)
        transcript = paths["final"]
        if not transcript.is_file():
            counters["skipped"] += 1
            print(f"  - 跳过 {aweme_id}：缺少最终文案 {transcript.name}，无法生成总结。")
            continue

        # 触发条件 (a)：识别到笔记缺失 ## 内容总结 模块
        note = note_index.get(aweme_id)
        if note is not None and note_has_summary(note) and not args.force:
            state_path = state_dir / key / f"{pipe.safe_key(aweme_id)}.json"
            state = pipe.load_state(state_path, creator, work)
            if pipe.status_of(state, "summarized") != SUMMARY_TEMPLATE_MISSING:
                counters["skipped"] += 1
                continue
            if table_id and not args.no_feishu and not write_feishu_local_status(
                table_id=table_id,
                aweme_id=aweme_id,
                local_status="已写入",
                dry_run=args.dry_run,
            ):
                counters["failed"] += 1
                print(f"  - 对账失败 {aweme_id}：飞书状态回写失败")
                continue
            pipe.set_status(
                state,
                "summarized",
                "success",
                "内容总结模块已存在，状态已对账",
            )
            if not args.dry_run:
                pipe.write_json(state_path, state)
            counters["reconciled"] += 1
            print(f"  = 对账 {aweme_id}：笔记已有内容总结，状态已更新")
            continue

        print(f"  + 补充 {aweme_id}（{title_of(work)}）…", end="", flush=True)
        try:
            if capability != "llm":
                counters["failed"] += 1
                print(f" 补充失败：summary.model 未配置")
                continue
            if not generate_summary(
                transcript=transcript, template_file=summary_template_file, output=paths["summary"],
                creator_name=creator_name, title=title_of(work), aweme_id=aweme_id,
                summary_cfg=summary_cfg, dry_run=args.dry_run,
            ):
                counters["failed"] += 1
                print(" 生成失败")
                continue
            if not merge_summary_into_note(
                transcript=transcript, aweme_id=aweme_id, works_file=works_file,
                creator_name=creator_name, creator_dir_name=creator_dir_name,
                original_dir=original_dir, obsidian_template=obsidian_template,
                summary_file=paths["summary"], dry_run=args.dry_run,
            ):
                counters["failed"] += 1
                print(" 合并失败")
                continue
            # 更新状态文件：summarized -> success
            state_path = state_dir / key / f"{pipe.safe_key(aweme_id)}.json"
            state = pipe.load_state(state_path, creator, work)
            pipe.set_status(state, "summarized", "success", "内容总结模块已补充")
            if not args.dry_run:
                pipe.write_json(state_path, state)
            # 回写飞书「本地知识库状态」（--no-feishu 时跳过，常用于飞书读取被环境权限阻断的场景）
            if table_id and not args.no_feishu:
                if not write_feishu_local_status(
                    table_id=table_id, aweme_id=aweme_id,
                    local_status="已写入", dry_run=args.dry_run,
                ):
                    counters["failed"] += 1
                    print(" 飞书状态回写失败")
                    continue
            counters["supplemented"] += 1
            print(" 完成")
        except Exception as exc:  # 单个作品异常不应中断整轮处理
            counters["failed"] += 1
            print(f" 异常: {exc}", file=sys.stderr)
            continue
    return counters


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="补充缺失的「内容总结」模块到已有 Obsidian 笔记（独立固化模块）")
    parser.add_argument("--config", default="local/pipeline.json", help="流水线配置文件")
    parser.add_argument("--creator", action="append", default=[], help="指定达人 key（可多次）；不填则配合 --all 使用")
    parser.add_argument("--all", action="store_true", help="处理配置中所有启用的达人")
    parser.add_argument("--force", action="store_true", help="即使笔记已有 ## 内容总结 也重新生成")
    parser.add_argument("--no-feishu", action="store_true", help="跳过飞书「本地知识库状态」回写（飞书读取被环境权限阻断时使用）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的命令，不调用模型、不写文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = pipe.path_from(args.config)
    if config_path is None or not config_path.is_file():
        print(f"错误：配置文件不存在: {args.config}", file=sys.stderr)
        return 2
    config = pipe.read_json(config_path)
    creators = [c for c in config.get("creators", []) if isinstance(c, dict) and c.get("enabled", True)]
    if args.creator:
        requested = set(args.creator)
        creators = [c for c in creators if pipe.creator_key(c) in requested]
        if requested - {pipe.creator_key(c) for c in creators}:
            missing = sorted(requested - {pipe.creator_key(c) for c in creators})
            print(f"错误：配置中找不到或未启用达人: {missing}", file=sys.stderr)
            return 2
    elif not args.all:
        print("错误：请通过 --creator <key> 指定达人，或使用 --all 处理全部。", file=sys.stderr)
        return 2
    if not creators:
        print("没有需要处理的达人。")
        return 0

    total = {"supplemented": 0, "reconciled": 0, "skipped": 0, "failed": 0}
    for creator in creators:
        try:
            counters = supplement_creator(config, creator, args)
        except Exception as exc:
            print(f"[异常] 达人 {pipe.creator_key(creator)}：{exc}", file=sys.stderr)
            counters = {"supplemented": 0, "reconciled": 0, "skipped": 0, "failed": 1}
        for k in total:
            total[k] += counters.get(k, 0)
    print(
        f"\n汇总：补充 {total['supplemented']} 篇，对账 {total['reconciled']} 篇，"
        f"跳过 {total['skipped']} 篇，失败 {total['failed']} 篇。"
    )
    return 1 if total["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
