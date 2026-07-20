"""Export a completed video transcript into an Obsidian original-copy folder.

This script keeps the generated Obsidian note human-readable first:
- one creator gets one directory under the configured Obsidian `Z_Original` root;
- one video transcript becomes one Markdown note;
- the complete original transcript is kept untouched under `## 原始文案`;
- note properties are written in Chinese for day-to-day reading.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

DEFAULT_OBSIDIAN_ORIGINAL_DIR = Path(r"D:\software\Obsidian\ljr_data\ljr_data\Z_Original")
DEFAULT_OBSIDIAN_TEMPLATE_FILE = Path(r"D:\software\Obsidian\ljr_data\ljr_data\Template\视频原始文案模板.md")
CHINA_TZ = timezone(timedelta(hours=8))

DEFAULT_CLEANING_RULES = """你是短视频口播文案排版工具，只做文本分段美化，不修改原文任何文字、数字、专业知识点，不概括、不删减。
1. 一句话结束（。！？）基础换行；
2. 博主分点讲解（第一/第二/首先/其次/再者/还有一点）单独起一行；
3. 如果同一篇文案出现多个分点，每个分点单独做一个 Markdown 无序列表，用 - 开头；
4. 提问句单独一行；
5. 案例、举例内容自成一段；
6. 观点总结、收尾话术单独分段；
7. 长句内部不强行拆分，只在语义边界切割；
8. 不要添加标题、注释、解释，只输出排版后的原文；
9. 不保留空行。"""

DEFAULT_NOTE_TEMPLATE = """---
平台: {{平台}}
达人: {{达人}}
达人目录: {{达人目录}}
作品标题: {{作品标题}}
作品ID: {{作品ID}}
作品链接: {{作品链接}}
发布时间: {{发布时间}}
达人主页: {{达人主页}}
账号ID: {{账号ID}}
入库时间: {{入库时间}}
tags:
  - 平台/{{平台_文本}}
  - 达人/{{达人目录_文本}}
---
# {{作品标题_文本}}
## 原始文案
{{原始文案}}
<!-- CLEANING_RULES_START
## 正文清洗规则
{{正文清洗规则}}
CLEANING_RULES_END -->
"""


class ObsidianExportError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ObsidianExportError(f"JSON 文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ObsidianExportError(f"JSON 文件格式错误: {path}") from exc
    if not isinstance(data, dict):
        raise ObsidianExportError(f"JSON 顶层必须是对象: {path}")
    return data


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ObsidianExportError(f"文案文件不存在: {path}") from exc


def find_work(works_file: Path, aweme_id: str) -> dict[str, Any]:
    data = read_json(works_file)
    works = data.get("works")
    if not isinstance(works, list):
        raise ObsidianExportError(f"作品 JSON 中缺少 works 列表: {works_file}")
    for work in works:
        if isinstance(work, dict) and str(work.get("aweme_id") or "") == aweme_id:
            return work
    raise ObsidianExportError(f"没有在作品 JSON 中找到作品 ID: {aweme_id}")


def sanitize_path_part(value: str, *, fallback: str, limit: int = 80) -> str:
    value = (value or "").strip()
    value = "".join(
        char for char in value
        if ord(char) <= 0xFFFF and ord(char) >= 32 and not 127 <= ord(char) <= 159
    )
    value = re.sub(r"[\\/:*?\"<>|\r\n\t#]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return value[:limit] or fallback


def split_title_and_tags(desc: str) -> tuple[str, str]:
    desc = (desc or "").strip()
    if not desc:
        return "", ""
    first_tag = desc.find("#")
    if first_tag < 0:
        return desc, ""
    return desc[:first_tag].strip(), desc[first_tag:].strip()


SENTENCE_ENDINGS = "。！？"
SECTION_START_MARKERS = (
    "第一步，",
    "第一，",
    "第二，",
    "第二个就是",
    "首先，",
    "其次，",
    "再者，",
    "还有一点，",
)
MODULE_START_MARKERS = (
    "第一步，",
    "第二个就是设置 ROI 目标。",
    "那刚开始来说，",
    "然后如果说 ROI 值",
    "然后就看调控这边",
    "等这个素材跑的这个值",
    "如果说基础消耗没出来的话，",
    "只要你调控跑的数据",
    "等到基础消耗出来之后，",
    "开始跑起来流速之后，",
    "前期 ROI 目标",
    "通过卡预算",
    "不管你是",
    "然后这边再给大家透露一个消息，",
    "第二点就是8月1号开始，",
    "然后就是到底投成方还是投全域。",
    "一开始成方刚出来的时候，",
    "但是到现在目前来说，",
    "如果说你比较会做内容，",
)


def insert_breaks_before_markers(text: str, markers: tuple[str, ...], break_text: str) -> str:
    positions: list[int] = []
    for marker in markers:
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx < 0:
                break
            if idx > 0:
                positions.append(idx)
            start = idx + len(marker)
    if not positions:
        return text

    result: list[str] = []
    last = 0
    for idx in sorted(set(positions)):
        result.append(text[last:idx])
        if not "".join(result).endswith("\n"):
            result.append(break_text)
        last = idx
    result.append(text[last:])
    return "".join(result)


def split_sentences(text: str) -> str:
    result: list[str] = []
    for index, char in enumerate(text):
        result.append(char)
        if char in SENTENCE_ENDINGS and index < len(text) - 1:
            next_char = text[index + 1]
            if next_char not in "\r\n":
                result.append("\n")
    return "".join(result)


def add_blank_lines_before_modules(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        is_module_start = bool(stripped) and any(stripped.startswith(marker) for marker in MODULE_START_MARKERS)
        if is_module_start and result and result[-1] != "":
            result.append("")
        result.append(line.rstrip())
    return "\n".join(result)


def normalize_blank_lines(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line.strip()).strip("\n")


POINT_LINE_RE = re.compile(r"^(?:第[一二三四五六七八九十百千万0-9]+(?:个|点|步)?|首先|其次|再者|还有一点)")


def add_unordered_list_for_multiple_points(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    point_line_indexes = [
        index
        for index, line in enumerate(lines)
        if POINT_LINE_RE.match(line.strip())
    ]
    if len(point_line_indexes) < 2:
        return text

    point_line_index_set = set(point_line_indexes)
    result: list[str] = []
    for index, line in enumerate(lines):
        if index in point_line_index_set and line.strip() and not line.lstrip().startswith("- "):
            leading_spaces = line[: len(line) - len(line.lstrip())]
            result.append(f"{leading_spaces}- {line.lstrip()}")
        else:
            result.append(line)
    return "\n".join(result)


def wants_rule(cleaning_rules: str, *keywords: str) -> bool:
    return any(keyword in cleaning_rules for keyword in keywords)


def format_transcript_for_reading(transcript: str, cleaning_rules: str = DEFAULT_CLEANING_RULES) -> str:
    """Only add reading layout; never change source transcript characters."""
    formatted = transcript.strip("\ufeff\r\n")
    if wants_rule(cleaning_rules, "一句话结束", "基础换行", "。！？"):
        formatted = split_sentences(formatted)
    if wants_rule(cleaning_rules, "分点", "第一/第二", "首先", "其次", "再者", "还有一点"):
        formatted = insert_breaks_before_markers(formatted, SECTION_START_MARKERS, "\n")
    if wants_rule(cleaning_rules, "无序列表", "无序序列", "用 - 开头"):
        formatted = add_unordered_list_for_multiple_points(formatted)
    return normalize_blank_lines(formatted)

def work_title(work: dict[str, Any], aweme_id: str) -> str:
    desc = str(work.get("desc") or work.get("raw", {}).get("desc") or "").strip()
    raw_title = str(work.get("raw", {}).get("title") or "").strip()
    title, _ = split_title_and_tags(raw_title or desc)
    return title or aweme_id


def format_count(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value)


def timestamp_to_local_text(value: Any) -> str:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ts, tz=CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def yaml_string(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"



def read_note_template(template_path: Path | None) -> str:
    if template_path is None:
        return DEFAULT_NOTE_TEMPLATE
    try:
        if template_path.exists():
            return template_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ObsidianExportError(f"读取 Obsidian 模板失败: {template_path}") from exc
    return DEFAULT_NOTE_TEMPLATE


def extract_cleaning_rules(template: str) -> str:
    match = re.search(r"<!--\s*CLEANING_RULES_START\s*(.*?)\s*CLEANING_RULES_END\s*-->", template, flags=re.S)
    if not match:
        return DEFAULT_CLEANING_RULES
    rules = match.group(1).strip()
    if rules.startswith("## 正文清洗规则"):
        rules = rules.removeprefix("## 正文清洗规则").strip()
    return rules or DEFAULT_CLEANING_RULES


def strip_cleaning_rules_block(template: str) -> str:
    return re.sub(r"\n?<!--\s*CLEANING_RULES_START.*?CLEANING_RULES_END\s*-->\s*", "\n", template, flags=re.S)


def render_note_template(template: str, values: dict[str, str]) -> str:
    rendered = strip_cleaning_rules_block(template)
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    missing = sorted(set(re.findall(r"{{([^{}]+)}}", rendered)))
    if missing:
        raise ObsidianExportError(f"模板中存在未支持的占位符: {', '.join(missing)}")
    return rendered.strip("\ufeff\r\n")

def build_note(
    *,
    aweme_id: str,
    transcript: str,
    work: dict[str, Any],
    profile: dict[str, Any],
    creator_name: str,
    creator_dir_name: str,
    transcript_path: Path,
    template_path: Path | None,
) -> str:
    desc = str(work.get("desc") or work.get("raw", {}).get("desc") or "").strip()
    title = work_title(work, aweme_id)
    publish_time = timestamp_to_local_text(work.get("create_time") or work.get("raw", {}).get("create_time"))
    video_url = str(work.get("url") or work.get("raw", {}).get("aweme_url") or f"https://www.douyin.com/video/{aweme_id}")
    exported_at = datetime.now(tz=CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    creator_profile_url = str(profile.get("达人主页地址") or profile.get("profileUrl") or profile.get("creator_url") or "")
    account_id = str(profile.get("账号ID") or "")
    platform = str(profile.get("所属平台") or "抖音")
    template = read_note_template(template_path)
    cleaning_rules = extract_cleaning_rules(template)
    formatted_transcript = format_transcript_for_reading(transcript, cleaning_rules)

    return render_note_template(
        template,
        {
            "平台": yaml_string(platform),
            "平台_文本": platform,
            "达人": yaml_string(creator_name),
            "达人_文本": creator_name,
            "达人目录": yaml_string(creator_dir_name),
            "达人目录_文本": creator_dir_name,
            "作品标题": yaml_string(title),
            "作品标题_文本": title,
            "作品ID": yaml_string(aweme_id),
            "作品ID_文本": aweme_id,
            "作品链接": yaml_string(video_url),
            "作品链接_文本": video_url,
            "发布时间": yaml_string(publish_time),
            "发布时间_文本": publish_time,
            "达人主页": yaml_string(creator_profile_url),
            "达人主页_文本": creator_profile_url,
            "账号ID": yaml_string(account_id),
            "账号ID_文本": account_id,
            "入库时间": yaml_string(exported_at),
            "入库时间_文本": exported_at,
            "原始文案": formatted_transcript,
            "正文清洗规则": cleaning_rules,
        },
    )

def build_output_path(base_dir: Path, creator_dir_name: str, work: dict[str, Any], aweme_id: str) -> Path:
    title = work_title(work, aweme_id)
    publish_date = timestamp_to_local_text(work.get("create_time") or work.get("raw", {}).get("create_time"))[:10] or "unknown-date"
    safe_title = sanitize_path_part(title, fallback="未命名作品", limit=56)
    return base_dir / sanitize_path_part(creator_dir_name, fallback="未知达人") / f"{publish_date}_{aweme_id}_{safe_title}.md"


def directory_metadata(directory: Path, creator_dir_name: str, created: bool) -> dict[str, Any]:
    return {
        "platform": "obsidian",
        "directory": {
            "name": creator_dir_name,
            "id": "",
            "path": str(directory.resolve()),
            "created": created,
        },
        "feishu_fields": {
            "Obsidian文件夹名称": creator_dir_name,
            "Obsidian文件夹路径": str(directory.resolve()),
            "Obsidian同步状态": "已映射",
        },
    }


def write_metadata(path: Path | None, value: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把火山转写原始文案保存到 Obsidian Z_Original，按达人目录归档")
    parser.add_argument("--transcript", type=Path, help="完整原始文案 TXT 文件")
    parser.add_argument("--aweme-id", help="抖音作品 ID")
    parser.add_argument("--works-file", type=Path, help="MediaCrawler 规范化作品 JSON")
    parser.add_argument("--profile-file", type=Path, default=None, help="达人主页信息 JSON，可选")
    parser.add_argument("--creator-name", default="", help="达人显示名称；不填时尝试从 profile JSON 读取")
    parser.add_argument("--creator-dir-name", default="", help="Obsidian 下的达人目录名；不填时使用 creator-name")
    parser.add_argument("--obsidian-original-dir", type=Path, default=DEFAULT_OBSIDIAN_ORIGINAL_DIR, help="Obsidian 原始文案根目录")
    parser.add_argument("--template-file", type=Path, default=DEFAULT_OBSIDIAN_TEMPLATE_FILE, help="Obsidian 原始文案模板文件；模板内 CLEANING_RULES 区块会作为正文清洗规则来源")
    parser.add_argument("--overwrite", action="store_true", help="目标文件已存在时覆盖")
    parser.add_argument("--ensure-creator-dir-only", action="store_true", help="只确保达人目录存在并输出映射")
    parser.add_argument("--metadata-output", type=Path, help="目录映射 JSON 输出文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        profile = read_json(args.profile_file) if args.profile_file else {}
        creator_name = args.creator_name.strip() or str(profile.get("达人昵称") or profile.get("作品表名称") or "未知达人").strip()
        creator_dir_name = args.creator_dir_name.strip() or creator_name
        creator_dir = args.obsidian_original_dir / sanitize_path_part(creator_dir_name, fallback="未知达人")
        created = not creator_dir.exists()
        creator_dir.mkdir(parents=True, exist_ok=True)
        write_metadata(args.metadata_output, directory_metadata(creator_dir, creator_dir_name, created))
        if args.ensure_creator_dir_only:
            print(f"已确认 Obsidian 达人目录: {creator_dir}")
            return 0
        if not args.transcript or not args.aweme_id or not args.works_file:
            raise ObsidianExportError("正式导出需要同时提供 --transcript、--aweme-id 和 --works-file。")
        work = find_work(args.works_file, args.aweme_id)
        transcript = read_text(args.transcript)
        existing_paths = sorted(creator_dir.glob(f"*_{args.aweme_id}_*.md"))
        output_path = existing_paths[0] if existing_paths else build_output_path(
            args.obsidian_original_dir, creator_dir_name, work, args.aweme_id,
        )
        if output_path.exists() and not args.overwrite:
            print(f"已跳过，目标文件已存在: {output_path}")
            return 0
        note = build_note(
            aweme_id=args.aweme_id,
            transcript=transcript,
            work=work,
            profile=profile,
            creator_name=creator_name,
            creator_dir_name=creator_dir_name,
            transcript_path=args.transcript.resolve(),
            template_path=args.template_file,
        )
        output_path.write_text(note, encoding="utf-8")
        print(output_path)
        return 0
    except ObsidianExportError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
