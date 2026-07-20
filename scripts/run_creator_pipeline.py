#!/usr/bin/env python3
"""Run the end-to-end Douyin creator transcript pipeline.

Stages: collect -> Feishu sync -> ASR -> correction -> Feishu writeback
-> IMA / Quark / Obsidian backup.

Existing CLI modules remain the source of truth. This file only coordinates
those modules and records resumable runtime state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

PROJECT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_DIR / "scripts"
DEFAULT_CONFIG = PROJECT_DIR / "local" / "pipeline.json"
DEFAULT_STATE_DIR = PROJECT_DIR / "runtime" / "pipeline"
DEFAULT_MEDIA_DIR = PROJECT_DIR / "runtime" / "media"
DEFAULT_LOG_DIR = PROJECT_DIR / "logs"
BEIJING_TZ = timezone(timedelta(hours=8))
HASHTAG_RE = re.compile(r"#([^#\s]+)")
INVALID_PATH_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
STAGES = (
    "collected", "feishu_synced", "transcribed", "corrected",
    "feishu_written_back", "ima_backed_up", "kuake_backed_up",
    "obsidian_exported", "backup_statuses_written_back",
)


class PipelineError(RuntimeError):
    pass


class Logger:
    def __init__(self, path: Path, *, persist: bool = True) -> None:
        self.path = path
        self.persist = persist
        self._lock = threading.Lock()
        if persist:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, message: str) -> None:
        line = f"[{datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        with self._lock:
            if self.persist:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            print_console(line)


class Runner:
    def __init__(self, logger: Logger, dry_run: bool) -> None:
        self.logger = logger
        self.dry_run = dry_run
        self.metrics: list[dict[str, Any]] = []
        self._metrics_lock = threading.Lock()

    @staticmethod
    def display(command: list[str], sensitive: Iterable[str]) -> str:
        hidden = set(sensitive)
        result: list[str] = []
        mask_next = False
        for part in command:
            if mask_next:
                result.append("<redacted>")
                mask_next = False
            else:
                result.append(part)
                mask_next = part in hidden
        return subprocess.list2cmdline(result)

    def run(
        self,
        label: str,
        command: list[str],
        env: dict[str, str],
        *,
        sensitive: Iterable[str] = (),
    ) -> str:
        shown = self.display(command, sensitive)
        if self.dry_run:
            self.logger.write(f"[DRY-RUN] {label}: {shown}")
            with self._metrics_lock:
                self.metrics.append({"label": label, "seconds": 0.0, "status": "planned"})
            return ""
        self.logger.write(f"开始 {label}: {shown}")
        started = time.perf_counter()
        try:
            result = subprocess.run(
                command,
                cwd=PROJECT_DIR,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except Exception:
            elapsed = round(time.perf_counter() - started, 3)
            with self._metrics_lock:
                self.metrics.append({"label": label, "seconds": elapsed, "status": "failed"})
            raise
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if stdout:
            self.logger.write(f"{label} stdout: {truncate(stdout)}")
        if stderr:
            self.logger.write(f"{label} stderr: {truncate(stderr)}")
        elapsed = round(time.perf_counter() - started, 3)
        status = "failed" if result.returncode else "success"
        with self._metrics_lock:
            self.metrics.append({"label": label, "seconds": elapsed, "status": status})
        if result.returncode:
            raise PipelineError(f"{label} 失败，退出码 {result.returncode}: {stderr or stdout}")
        self.logger.write(f"完成 {label}")
        return stdout


def truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f" ... <省略 {len(text) - limit} 个字符>"


def print_console(text: str, *, file: Any = None) -> None:
    stream = file or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe_text, file=stream)


def now_text() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise PipelineError(f"文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineError(f"JSON 格式错误: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"JSON 顶层必须是对象: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def path_from(value: Any, default: Path | None = None) -> Path | None:
    if value in (None, ""):
        return default
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


def section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name)
    return value if isinstance(value, dict) else {}


def chosen(creator: dict[str, Any], defaults: dict[str, Any], key: str, fallback: Any = None) -> Any:
    return creator.get(key) if creator.get(key) not in (None, "") else defaults.get(key, fallback)


def safe_key(value: str) -> str:
    return INVALID_PATH_RE.sub("_", value).strip(" ._") or "creator"


def creator_key(creator: dict[str, Any]) -> str:
    value = str(creator.get("key") or creator.get("creator_dir_name") or creator.get("creator_name") or "").strip()
    if not value:
        raise PipelineError("达人配置缺少 key、creator_dir_name 和 creator_name。")
    return safe_key(value)


def append_option(command: list[str], option: str, value: Any) -> None:
    if value not in (None, ""):
        command.extend([option, str(value)])


def py(config: dict[str, Any], script: str, *args: Any) -> list[str]:
    return [str(config.get("python") or sys.executable), str(SCRIPTS_DIR / script), *map(str, args)]


def nonempty(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def raw_work(work: dict[str, Any]) -> dict[str, Any]:
    value = work.get("raw")
    return value if isinstance(value, dict) else {}


def audio_url(work: dict[str, Any]) -> str:
    raw = raw_work(work)
    for value in (
        work.get("music_download_url"), raw.get("music_download_url"),
        work.get("video_download_url"), raw.get("video_download_url"),
    ):
        if isinstance(value, list):
            value = next((item for item in value if isinstance(item, str) and item.strip()), "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def fallback_work_text(work: dict[str, Any]) -> str:
    raw = raw_work(work)
    value = str(raw.get("desc") or raw.get("title") or work.get("desc") or work.get("title") or "")
    return value.strip()


def should_fallback_to_work_text(asr_failure_detail: str) -> bool:
    permanent_markers = (
        "Normal silence audio",
        "no valid speech",
        "Invalid audio URI",
        "audio download failed",
    )
    folded = asr_failure_detail.casefold()
    return any(marker.casefold() in folded for marker in permanent_markers)


def title_of(work: dict[str, Any]) -> str:
    raw = raw_work(work)
    value = str(raw.get("title") or work.get("title") or work.get("desc") or work.get("aweme_id") or "")
    value = HASHTAG_RE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120] or str(work.get("aweme_id") or "未命名作品")


def safe_external_title(work: dict[str, Any], limit: int = 56) -> str:
    """Return a Windows/CLI-safe short title for external backup tools."""

    value = title_of(work)
    value = "".join(
        char for char in value
        if ord(char) <= 0xFFFF and ord(char) >= 32 and not 127 <= ord(char) <= 159
    )
    value = INVALID_PATH_RE.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return value[:limit] or str(work.get("aweme_id") or "未命名作品")


def date_of(work: dict[str, Any]) -> str:
    try:
        return datetime.fromtimestamp(float(work.get("create_time")), timezone.utc).astimezone(BEIJING_TZ).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")


def artifact_paths(media_dir: Path, work_id: str, provider: str) -> dict[str, Path]:
    provider_name = "volc-url" if provider == "volcengine" else "bailian-url"
    return {
        "raw_json": media_dir / f"{work_id}.{provider_name}.json",
        "raw_text": media_dir / f"{work_id}.{provider_name}.txt",
        "final": media_dir / f"{work_id}.final.txt",
        "report": media_dir / f"{work_id}.correction-report.json",
    }


def load_works(path: Path) -> list[dict[str, Any]]:
    works = read_json(path).get("works")
    if not isinstance(works, list):
        raise PipelineError(f"作品文件缺少 works 数组: {path}")
    return [item for item in works if isinstance(item, dict) and item.get("aweme_id")]


def select_works(works: list[dict[str, Any]], ids: set[str], maximum: int) -> list[dict[str, Any]]:
    selected = [w for w in works if not ids or str(w.get("aweme_id")) in ids]
    if ids:
        missing = ids - {str(w.get("aweme_id")) for w in selected}
        if missing:
            raise PipelineError(f"作品文件中找不到指定作品 ID: {sorted(missing)}")
    selected.sort(key=lambda w: int(w.get("create_time") or 0), reverse=True)
    return selected[:maximum] if maximum > 0 else selected


def backfill_stage_requirements(config: dict[str, Any], args: argparse.Namespace) -> tuple[str, ...]:
    """Return the local stages that must be complete for historical backfill."""

    required = ["corrected"]
    for section_name, option_name, stage in (
        ("ima", "skip_ima", "ima_backed_up"),
        ("kuake", "skip_kuake", "kuake_backed_up"),
        ("obsidian", "skip_obsidian", "obsidian_exported"),
    ):
        if not getattr(args, option_name, False) and bool(section(config, section_name).get("enabled", True)):
            required.append(stage)
    return tuple(required)


def work_needs_backfill(
    config: dict[str, Any], creator: dict[str, Any], work: dict[str, Any],
    state_dir: Path, media_dir: Path, args: argparse.Namespace,
) -> bool:
    """Check whether a local work still lacks its final transcript or an enabled backup."""

    work_id = str(work["aweme_id"])
    state_path = state_dir / creator_key(creator) / f"{safe_key(work_id)}.json"
    state = load_state(state_path, creator, work)
    provider = str(chosen(creator, section(config, "asr"), "provider", "volcengine"))
    paths = artifact_paths(media_dir, work_id, provider)
    if not nonempty(paths["final"]):
        return True
    for stage in backfill_stage_requirements(config, args):
        if stage == "corrected":
            continue
        if status_of(state, stage) != "success":
            return True
    return False


def select_backfill_works(
    config: dict[str, Any], creator: dict[str, Any], works: list[dict[str, Any]],
    state_dir: Path, media_dir: Path, args: argparse.Namespace, maximum: int,
) -> list[dict[str, Any]]:
    selected = [
        work for work in works
        if work_needs_backfill(config, creator, work, state_dir, media_dir, args)
    ]
    selected.sort(key=lambda work: int(work.get("create_time") or 0), reverse=True)
    return selected[:maximum] if maximum > 0 else selected


def write_selected_works_file(source_file: Path, selected: list[dict[str, Any]], output_file: Path) -> Path:
    payload = read_json(source_file)
    payload["works"] = selected
    payload["count"] = len(selected)
    payload["source_file"] = str(source_file)
    payload["selected_at"] = now_text()
    write_json(output_file, payload)
    return output_file


def collection_state_path(state_dir: Path, creator: dict[str, Any]) -> Path:
    return state_dir / "collection" / f"{safe_key(creator_key(creator))}.json"


def pending_collection_ids(state_file: Path, works_file: Path) -> list[str]:
    payload: dict[str, Any] = {}
    if state_file.exists():
        try:
            payload = read_json(state_file)
        except PipelineError:
            payload = {}
    if not payload and works_file.exists():
        payload = read_json(works_file)
    values = payload.get("pending_aweme_ids", [])
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def complete_collection_pending(state_file: Path, works_file: Path, completed_ids: Iterable[str]) -> None:
    completed = {str(value) for value in completed_ids}
    if not completed:
        return
    for path in (state_file, works_file):
        if not path.exists():
            continue
        payload = read_json(path)
        pending = payload.get("pending_aweme_ids", [])
        if not isinstance(pending, list):
            continue
        remaining = [str(value) for value in pending if str(value) not in completed]
        payload["pending_aweme_ids"] = remaining
        payload["pending_count"] = len(remaining)
        payload["last_pipeline_completed_at"] = now_text()
        write_json(path, payload)


def load_state(path: Path, creator: dict[str, Any], work: dict[str, Any]) -> dict[str, Any]:
    try:
        state = read_json(path) if path.exists() else {}
    except PipelineError:
        state = {}
    state.setdefault("creator_key", creator_key(creator))
    state.setdefault("creator_name", str(creator.get("creator_name") or creator.get("creator_dir_name") or ""))
    state.setdefault("aweme_id", str(work.get("aweme_id")))
    state.setdefault("title", title_of(work))
    state.setdefault("stages", {})
    return state


def status_of(state: dict[str, Any], stage: str) -> str:
    value = state.get("stages", {}).get(stage, {})
    return str(value.get("status") or "") if isinstance(value, dict) else ""


def set_status(
    state: dict[str, Any], stage: str, status: str, detail: str = "", inferred: bool = False,
    duration_seconds: float | None = None,
) -> None:
    item: dict[str, Any] = {"status": status, "updated_at": now_text()}
    if detail:
        item["detail"] = detail
    if inferred:
        item["inferred"] = True
    if duration_seconds is not None:
        item["duration_seconds"] = round(duration_seconds, 3)
    state.setdefault("stages", {})[stage] = item
    state["updated_at"] = now_text()


def set_combined_finalizer_status(
    state: dict[str, Any], *, transcript_included: bool, status: str,
    duration_seconds: float, detail: str = "", operation_id: str | None = None,
) -> None:
    """Record one remote operation once while keeping both logical stages resumable."""
    operation_id = operation_id or f"finalizer-{time.time_ns()}"
    if transcript_included:
        set_status(state, "feishu_written_back", status, detail, duration_seconds=duration_seconds)
        set_status(state, "backup_statuses_written_back", status, detail, duration_seconds=0.0)
    else:
        set_status(state, "backup_statuses_written_back", status, detail, duration_seconds=duration_seconds)
    for stage in (("feishu_written_back", "backup_statuses_written_back") if transcript_included else ("backup_statuses_written_back",)):
        state["stages"][stage]["operation_id"] = operation_id


def persist_work_failure(
    state_path: Path, creator: dict[str, Any], work: dict[str, Any],
    stage: str, detail: str, *, blocked_stages: Iterable[str] = (),
) -> None:
    state = load_state(state_path, creator, work)
    set_status(state, stage, "failed", detail)
    for blocked_stage in blocked_stages:
        set_status(state, blocked_stage, "blocked", detail)
    write_json(state_path, state)


def should_skip(
    state: dict[str, Any],
    stage: str,
    resume: bool,
    force: set[str],
    required: Iterable[Path] = (),
) -> bool:
    if not resume or "all" in force or stage in force or status_of(state, stage) != "success":
        return False
    return all(nonempty(path) for path in required)


def execute_stage(
    stage: str,
    label: str,
    state: dict[str, Any],
    state_path: Path,
    args: argparse.Namespace,
    logger: Logger,
    action: Callable[[], None],
    required: Iterable[Path] = (),
) -> str:
    files = tuple(required)
    if should_skip(state, stage, args.resume, args.force_stage, files):
        logger.write(f"跳过 {label}：状态已完成")
        return "skipped"
    started = time.perf_counter()
    try:
        action()
        if not args.dry_run:
            missing = [str(path) for path in files if not nonempty(path)]
            if missing:
                raise PipelineError(f"{label} 未生成预期文件: {missing}")
            set_status(state, stage, "success", duration_seconds=time.perf_counter() - started)
            write_json(state_path, state)
        return "planned" if args.dry_run else "success"
    except Exception as exc:  # Keep other backups and works running.
        logger.write(f"失败 {label}: {exc}")
        if not args.dry_run:
            set_status(state, stage, "failed", str(exc), duration_seconds=time.perf_counter() - started)
            write_json(state_path, state)
        return "failed"


def child_env(config: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    feishu = section(config, "feishu")
    source_name = str(feishu.get("base_token_env") or "FEISHU_BASE_TOKEN")
    token = env.get(source_name, "").strip()
    if not token:
        ids_file = path_from(feishu.get("ids_file"), PROJECT_DIR / "local" / "feishu-ids.md")
        if ids_file and ids_file.exists():
            match = re.search(r"base_token:\s*([A-Za-z0-9]+)", ids_file.read_text(encoding="utf-8-sig"))
            token = match.group(1) if match else ""
    if token:
        env["FEISHU_BASE_TOKEN"] = token
    return env


def collection_slot(config: dict[str, Any], creator: dict[str, Any]) -> int:
    """Return the creator's stable zero-based slot in configured order."""

    target = creator_key(creator)
    configured = config.get("creators", [])
    if isinstance(configured, list):
        for index, item in enumerate(configured):
            if isinstance(item, dict) and creator_key(item) == target:
                return index
    return 0


def collect_command(
    config: dict[str, Any], creator: dict[str, Any], works_file: Path,
    media_output: Path, collection_state_file: Path, normalize_only: bool,
    force_full_collect: bool = False,
) -> list[str]:
    defaults = section(config, "collection")
    creator_url = str(creator.get("creator_url") or "").strip()
    if not creator_url:
        raise PipelineError(f"达人 {creator_key(creator)} 缺少 creator_url。")
    profile_key = str(chosen(creator, defaults, "browser_profile_key", creator_key(creator)))
    try:
        port_start = int(chosen(creator, defaults, "cdp_port_start", 9222))
        port_stride = int(chosen(creator, defaults, "cdp_port_stride", 10))
        cdp_port = int(chosen(creator, defaults, "cdp_port", port_start + collection_slot(config, creator) * port_stride))
    except (TypeError, ValueError) as exc:
        raise PipelineError("collection.cdp_port_start/cdp_port_stride/cdp_port 必须是整数。") from exc
    if port_stride < 1:
        raise PipelineError("collection.cdp_port_stride 必须至少为 1。")
    if not 1 <= cdp_port <= 65535:
        raise PipelineError(f"达人 {creator_key(creator)} 的 CDP 端口超出 1-65535: {cdp_port}")
    command = py(
        config, "collect_douyin_creator_with_mediacrawler.py",
        "--creator-url", creator_url,
        "--media-output-dir", media_output,
        "--output-file", works_file,
        "--collection-state-file", collection_state_file,
        "--incremental-probe-count", chosen(creator, defaults, "incremental_probe_count", 3),
        "--max-count", chosen(creator, defaults, "max_count", 200),
        "--expect-min-count", chosen(creator, defaults, "expect_min_count", 1),
        "--login-type", chosen(creator, defaults, "login_type", "qrcode"),
        "--browser-profile-key", profile_key,
        "--cdp-port", cdp_port,
        "--save-data-option", chosen(creator, defaults, "save_data_option", "jsonl"),
    )
    append_option(command, "--media-crawler-dir", chosen(creator, defaults, "media_crawler_dir"))
    append_option(command, "--media-crawler-python", chosen(creator, defaults, "media_crawler_python"))
    if chosen(creator, defaults, "clean_media_output", False):
        command.append("--clean-media-output")
    incremental_enabled = bool(chosen(creator, defaults, "incremental_enabled", True))
    if force_full_collect or not incremental_enabled:
        command.append("--force-full-collect")
    if normalize_only:
        command.append("--normalize-only")
    return command


def sync_command(config: dict[str, Any], creator: dict[str, Any], works_file: Path) -> list[str]:
    table = str(creator.get("works_table_id") or "").strip()
    if not table:
        raise PipelineError(f"达人 {creator_key(creator)} 缺少 works_table_id。")
    command = py(config, "sync_douyin_works_to_feishu.py", "--works-file", works_file, "--table-id", table)
    append_option(command, "--lark-cli", section(config, "feishu").get("lark_cli"))
    return command


def transcribe_command(
    config: dict[str, Any], creator: dict[str, Any], url: str, paths: dict[str, Path],
) -> list[str]:
    defaults = section(config, "asr")
    command = py(
        config, "transcribe_douyin_audio_url.py",
        "--audio-url", url,
        "--provider", chosen(creator, defaults, "provider", "volcengine"),
        "--mode", chosen(creator, defaults, "mode", "direct-url"),
        "--json-output", paths["raw_json"],
        "--text-output", paths["raw_text"],
    )
    for option, key in (
        ("--ffmpeg", "ffmpeg"), ("--fallback-upload-command", "fallback_upload_command"),
        ("--model", "model"), ("--endpoint", "endpoint"),
    ):
        append_option(command, option, chosen(creator, defaults, key))
    return command


def correction_command(config: dict[str, Any], creator: dict[str, Any], paths: dict[str, Path]) -> list[str]:
    defaults = section(config, "correction")
    command = py(
        config, "correct_transcript.py", paths["raw_text"],
        "--output", paths["final"], "--report-output", paths["report"],
    )
    glossary = chosen(creator, defaults, "glossary")
    append_option(command, "--glossary", path_from(glossary) if glossary else None)
    domains = creator.get("correction_domains") or creator.get("correction_domain") or defaults.get("domains") or defaults.get("domain")
    if isinstance(domains, str):
        domains = [domains]
    if isinstance(domains, list):
        for domain in domains:
            append_option(command, "--domain", domain)
    return command


def writeback_command(
    config: dict[str, Any], creator: dict[str, Any], work: dict[str, Any], paths: dict[str, Path],
    record_id: str | None = None,
) -> list[str]:
    table = str(creator.get("works_table_id") or "").strip()
    if not table:
        raise PipelineError(f"达人 {creator_key(creator)} 缺少 works_table_id。")
    feishu = section(config, "feishu")
    command = py(
        config, "feishu_transcript_writer.py", "--table-id", table,
        "--work-id", work["aweme_id"], "--corrected-transcript-file", paths["final"],
    )
    append_option(command, "--work-id-field", feishu.get("work_id_field"))
    append_option(command, "--transcript-field", feishu.get("transcript_field"))
    append_option(command, "--lark-cli", feishu.get("lark_cli"))
    append_option(command, "--as", feishu.get("as_identity"))
    append_option(command, "--record-id", record_id)
    return command


def final_backup_status(outcome: str, success_value: str) -> str:
    if outcome in {"success", "skipped", "planned"}:
        return success_value
    if outcome == "disabled":
        return "跳过"
    return "失败"


def finalizer_needed(
    stages: dict[str, Any], transcript_file: Path | None, prior_status: str, resume: bool,
) -> bool:
    if transcript_file is not None or not resume or prior_status != "success":
        return True
    return any(
        stages.get(stage) not in {"skipped", "disabled"}
        for stage in ("ima_backed_up", "kuake_backed_up", "obsidian_exported")
    )


def status_writeback_command(
    config: dict[str, Any], creator: dict[str, Any], work: dict[str, Any], stages: dict[str, Any],
    record_id: str | None = None, transcript_file: Path | None = None,
) -> list[str]:
    table = str(creator.get("works_table_id") or "").strip()
    if not table:
        raise PipelineError(f"达人 {creator_key(creator)} 缺少 works_table_id。")
    feishu = section(config, "feishu")
    command = py(
        config, "feishu_work_status_writer.py", "--table-id", table,
        "--work-id", work["aweme_id"],
        "--ima-status", final_backup_status(str(stages.get("ima_backed_up", "blocked")), "已上传"),
        "--kuake-status", final_backup_status(str(stages.get("kuake_backed_up", "blocked")), "已上传"),
        "--local-status", final_backup_status(str(stages.get("obsidian_exported", "blocked")), "已写入"),
    )
    append_option(command, "--work-id-field", feishu.get("work_id_field"))
    append_option(command, "--lark-cli", feishu.get("lark_cli"))
    append_option(command, "--as", feishu.get("as_identity"))
    append_option(command, "--record-id", record_id)
    append_option(command, "--transcript-file", transcript_file)
    append_option(command, "--transcript-field", feishu.get("transcript_field"))
    return command


def status_writeback_batch_command(
    config: dict[str, Any], creator: dict[str, Any], manifest: Path,
) -> list[str]:
    table = str(creator.get("works_table_id") or "").strip()
    if not table:
        raise PipelineError(f"达人 {creator_key(creator)} 缺少 works_table_id。")
    feishu = section(config, "feishu")
    command = py(
        config, "feishu_work_status_writer.py",
        "--table-id", table, "--manifest", str(manifest),
    )
    append_option(command, "--work-id-field", feishu.get("work_id_field"))
    append_option(command, "--lark-cli", feishu.get("lark_cli"))
    append_option(command, "--as", feishu.get("as_identity"))
    append_option(command, "--transcript-field", feishu.get("transcript_field"))
    return command


def parse_sync_record_ids(output: str) -> dict[str, str]:
    if not output.strip():
        return {}
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {}
    values = payload.get("record_ids") if isinstance(payload, dict) else None
    if not isinstance(values, dict):
        return {}
    return {
        str(work_id): str(record_id)
        for work_id, record_id in values.items()
        if str(work_id).strip() and str(record_id).startswith("rec")
    }


def ima_command(config: dict[str, Any], creator: dict[str, Any], paths: dict[str, Path]) -> list[str]:
    ima = section(config, "ima")
    name = str(creator.get("ima_creator_name") or creator.get("creator_name") or creator.get("creator_dir_name") or "").strip()
    if not name:
        raise PipelineError("缺少 IMA 达人名称。")
    command = py(config, "backup_transcripts_to_ima.py")
    append_option(command, "--mapping", path_from(ima.get("mapping"), PROJECT_DIR / "local" / "ima_creator_mapping.json"))
    command.extend(["upload", "--creator-name", name, "--file", str(paths["final"]), "--on-duplicate", str(ima.get("on_duplicate") or "skip")])
    return command


def ima_ensure_command(config: dict[str, Any], creator: dict[str, Any], metadata: Path) -> list[str]:
    ima = section(config, "ima")
    name = str(creator.get("ima_creator_name") or creator.get("creator_name") or creator.get("creator_dir_name") or "").strip()
    if not name:
        raise PipelineError("缺少 IMA 达人名称。")
    command = py(config, "backup_transcripts_to_ima.py")
    append_option(command, "--mapping", path_from(ima.get("mapping"), PROJECT_DIR / "local" / "ima_creator_mapping.json"))
    command.extend(["ensure-folder", "--creator-name", name, "--metadata-output", str(metadata)])
    return command


def kuake_command(config: dict[str, Any], creator: dict[str, Any], work: dict[str, Any], paths: dict[str, Path]) -> list[str]:
    kuake = section(config, "kuake")
    name = str(creator.get("creator_dir_name") or creator.get("creator_name") or "").strip()
    if not name:
        raise PipelineError("缺少夸克达人目录名称。")
    command = py(config, "backup_transcripts_to_kuake.py")
    append_option(command, "--local-env", path_from(kuake.get("local_env"), PROJECT_DIR / "local" / "kuake.env.json"))
    if kuake.get("kuake_exe"):
        append_option(command, "--kuake-exe", path_from(kuake["kuake_exe"]))
    command.extend([
        "upload", "--file", str(paths["final"]), "--creator-name", name,
        "--video-date", date_of(work), "--video-id", str(work["aweme_id"]),
        "--title", safe_external_title(work), "--create-dir",
    ])
    append_option(command, "--base-dir", kuake.get("base_dir"))
    append_option(command, "--remote-dir", creator.get("kuake_remote_dir"))
    return command


def kuake_batch_command(
    config: dict[str, Any], creator: dict[str, Any], manifest: Path,
) -> list[str]:
    kuake = section(config, "kuake")
    name = str(creator.get("creator_dir_name") or creator.get("creator_name") or "").strip()
    if not name:
        raise PipelineError("缺少夸克达人目录名称。")
    command = py(config, "backup_transcripts_to_kuake.py")
    append_option(command, "--local-env", path_from(kuake.get("local_env"), PROJECT_DIR / "local" / "kuake.env.json"))
    if kuake.get("kuake_exe"):
        append_option(command, "--kuake-exe", path_from(kuake["kuake_exe"]))
    command.extend(["upload-manifest", "--manifest", str(manifest), "--creator-name", name])
    append_option(command, "--base-dir", kuake.get("base_dir"))
    append_option(command, "--remote-dir", creator.get("kuake_remote_dir"))
    return command


def kuake_ensure_command(config: dict[str, Any], creator: dict[str, Any], metadata: Path) -> list[str]:
    kuake = section(config, "kuake")
    name = str(creator.get("creator_dir_name") or creator.get("creator_name") or "").strip()
    if not name:
        raise PipelineError("缺少夸克达人目录名称。")
    command = py(config, "backup_transcripts_to_kuake.py")
    append_option(command, "--local-env", path_from(kuake.get("local_env"), PROJECT_DIR / "local" / "kuake.env.json"))
    if kuake.get("kuake_exe"):
        append_option(command, "--kuake-exe", path_from(kuake["kuake_exe"]))
    command.extend(["ensure-creator-dir", "--creator-name", name, "--metadata-output", str(metadata)])
    append_option(command, "--base-dir", kuake.get("base_dir"))
    append_option(command, "--remote-dir", creator.get("kuake_remote_dir"))
    return command


def obsidian_command(
    config: dict[str, Any], creator: dict[str, Any], work: dict[str, Any],
    works_file: Path, profile_file: Path | None, paths: dict[str, Path], overwrite: bool,
) -> list[str]:
    obsidian = section(config, "obsidian")
    command = py(
        config, "export_transcript_to_obsidian.py", "--transcript", paths["final"],
        "--aweme-id", work["aweme_id"], "--works-file", works_file,
    )
    append_option(command, "--profile-file", profile_file)
    append_option(command, "--creator-name", creator.get("creator_name"))
    append_option(command, "--creator-dir-name", creator.get("creator_dir_name"))
    append_option(command, "--obsidian-original-dir", path_from(obsidian.get("original_dir")))
    append_option(command, "--template-file", path_from(obsidian.get("template_file")))
    if overwrite:
        command.append("--overwrite")
    return command


def obsidian_ensure_command(
    config: dict[str, Any], creator: dict[str, Any], profile_file: Path | None, metadata: Path,
) -> list[str]:
    obsidian = section(config, "obsidian")
    command = py(config, "export_transcript_to_obsidian.py", "--ensure-creator-dir-only", "--metadata-output", metadata)
    append_option(command, "--profile-file", profile_file)
    append_option(command, "--creator-name", creator.get("creator_name"))
    append_option(command, "--creator-dir-name", creator.get("creator_dir_name"))
    append_option(command, "--obsidian-original-dir", path_from(obsidian.get("original_dir")))
    return command


def creator_match(creator: dict[str, Any]) -> tuple[str, str]:
    explicit_field = str(creator.get("feishu_match_field") or "").strip()
    explicit_value = str(creator.get("feishu_match_value") or "").strip()
    if explicit_field and explicit_value:
        return explicit_field, explicit_value
    creator_url = str(creator.get("creator_url") or "").strip().rstrip("/")
    if creator_url:
        sec_uid = creator_url.rsplit("/", 1)[-1].split("?", 1)[0].strip()
        if sec_uid:
            return "SecUID", sec_uid
    name = str(creator.get("creator_name") or "").strip()
    if name:
        return "达人昵称", name
    raise PipelineError("无法确定飞书达人基础表的匹配字段和值。")


def mapping_sync_command(
    config: dict[str, Any], creator: dict[str, Any], metadata: Path | list[Path],
) -> list[str]:
    feishu = section(config, "feishu")
    table_id = str(feishu.get("creator_table_id") or "").strip()
    if not table_id or table_id == "REPLACE_WITH_FEISHU_CREATOR_TABLE_ID":
        raise PipelineError("feishu.creator_table_id 未配置达人基础信息表 ID。")
    match_field, match_value = creator_match(creator)
    command = py(
        config, "sync_creator_backup_mapping_to_feishu.py",
        "--table-id", table_id, "--match-field", match_field, "--match-value", match_value,
    )
    metadata_files = metadata if isinstance(metadata, list) else [metadata]
    for metadata_file in metadata_files:
        command.extend(["--metadata-file", str(metadata_file)])
    append_option(command, "--lark-cli", feishu.get("lark_cli"))
    append_option(command, "--as", feishu.get("as_identity"))
    return command


def ensure_and_sync_mapping(
    runner: Runner, label: str, ensure_command: list[str], sync_command: list[str], env: dict[str, str],
) -> str:
    runner.run(f"确认{label}达人目录", ensure_command, env)
    output = runner.run(
        f"回写{label}目录映射到飞书", sync_command, env,
        sensitive=("--table-id", "--base-token", "--match-value"),
    )
    if runner.dry_run:
        return "planned"
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return "success"
    status = str(payload.get("status") or "success")
    return status if status in {"skipped", "updated", "success"} else "success"


def sync_creator_backup_mappings(
    config: dict[str, Any], creator: dict[str, Any], profile_file: Path | None,
    state_dir: Path, runner: Runner, logger: Logger, env: dict[str, str], args: argparse.Namespace,
) -> dict[str, str]:
    mapping_dir = state_dir / "mappings" / creator_key(creator)
    tasks: list[tuple[str, bool, list[str], Path]] = [
        (
            "IMA", args.skip_ima or not bool(section(config, "ima").get("enabled", True)),
            ima_ensure_command(config, creator, mapping_dir / "ima.json"), mapping_dir / "ima.json",
        ),
        (
            "夸克", args.skip_kuake or not bool(section(config, "kuake").get("enabled", True)),
            kuake_ensure_command(config, creator, mapping_dir / "kuake.json"), mapping_dir / "kuake.json",
        ),
        (
            "Obsidian", args.skip_obsidian or not bool(section(config, "obsidian").get("enabled", True)),
            obsidian_ensure_command(config, creator, profile_file, mapping_dir / "obsidian.json"),
            mapping_dir / "obsidian.json",
        ),
    ]
    enabled_metadata = [metadata for _, disabled, _, metadata in tasks if not disabled]
    marker_path = mapping_dir / "feishu-sync.json"
    expected_names = {
        "ima": str(creator.get("ima_creator_name") or creator.get("creator_name") or creator.get("creator_dir_name") or "").strip(),
        "kuake": str(creator.get("creator_dir_name") or creator.get("creator_name") or "").strip(),
        "obsidian": str(creator.get("creator_dir_name") or creator.get("creator_name") or "").strip(),
    }
    cache_ttl = float(section(config, "backups").get("mapping_cache_ttl_hours", 24))
    if not args.refresh_mappings and mapping_cache_is_fresh(
        enabled_metadata, cache_ttl, marker_path=marker_path,
        expected_creator_name=expected_names, expected_creator_key=creator_key(creator),
    ):
        logger.write(f"复用达人目录映射缓存，有效期 {cache_ttl:g} 小时")
        return {
            label.casefold(): ("disabled" if disabled else "cached")
            for label, disabled, _, _ in tasks
        }
    outcomes: dict[str, str] = {
        label.casefold(): "disabled" for label, disabled, _, _ in tasks if disabled
    }
    enabled_tasks = [item for item in tasks if not item[1]]
    ensure_actions = [
        (
            label.casefold(),
            lambda label=label, command=ensure_command: runner.run(
                f"确认{label}达人目录", command, env,
            ),
        )
        for label, _, ensure_command, _ in enabled_tasks
    ]
    ensure_results = run_independent_actions(
        ensure_actions, backup_worker_count(config, args, len(ensure_actions)), args.dry_run,
    )
    ready_metadata: list[Path] = []
    for label, _, _, metadata in enabled_tasks:
        key = label.casefold()
        item = ensure_results.get(key, {"status": "failed", "error": "missing result"})
        if item.get("status") == "failed":
            outcomes[key] = "failed"
            logger.write(f"{label} 达人目录确认失败: {item.get('error', '')}")
        else:
            outcomes[key] = str(item.get("status") or "success")
            ready_metadata.append(metadata)
    if ready_metadata and args.skip_feishu_writeback:
        logger.write("跳过飞书达人目录映射回写；本地目录确认结果继续有效")
        if not args.dry_run:
            write_json(marker_path, {
                "creator_key": creator_key(creator),
                "signature": mapping_cache_signature(ready_metadata),
                "confirmed_at": now_text(),
                "feishu_writeback": "disabled",
            })
    elif ready_metadata:
        try:
            output = runner.run(
                "合并回写达人目录映射到飞书",
                mapping_sync_command(config, creator, ready_metadata), env,
                sensitive=("--table-id", "--base-token", "--match-value"),
            )
            sync_status = "planned" if args.dry_run else "success"
            if output:
                try:
                    sync_status = str(json.loads(output).get("status") or sync_status)
                except json.JSONDecodeError:
                    pass
            for label, _, _, metadata in enabled_tasks:
                key = label.casefold()
                if metadata in ready_metadata:
                    outcomes[key] = sync_status
            if not args.dry_run and sync_status in {"success", "updated", "skipped"}:
                write_json(marker_path, {
                    "creator_key": creator_key(creator),
                    "signature": mapping_cache_signature(ready_metadata),
                    "synced_at": now_text(),
                })
        except Exception as exc:
            logger.write(f"达人目录映射合并回写失败: {exc}")
            for label, _, _, metadata in enabled_tasks:
                if metadata in ready_metadata:
                    outcomes[label.casefold()] = "failed"
            if args.fail_fast:
                raise
    return outcomes


def mapping_cache_signature(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item).casefold()):
        digest.update(path.stem.casefold().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def mapping_cache_is_fresh(
    paths: list[Path], ttl_hours: float, now_epoch: float | None = None, *,
    marker_path: Path | None = None,
    expected_creator_name: str | dict[str, str] | None = None,
    expected_creator_key: str | None = None,
) -> bool:
    if not paths or ttl_hours <= 0:
        return False
    now_value = time.time() if now_epoch is None else now_epoch
    maximum_age = ttl_hours * 3600
    for path in paths:
        if not path.is_file() or path.stat().st_size <= 2:
            return False
        if now_value - path.stat().st_mtime > maximum_age:
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return False
        expected_platform = path.stem.casefold()
        directory = payload.get("directory") if isinstance(payload, dict) else None
        fields = payload.get("feishu_fields") if isinstance(payload, dict) else None
        expected_name = (
            expected_creator_name.get(expected_platform, "")
            if isinstance(expected_creator_name, dict)
            else expected_creator_name
        )
        if (
            not isinstance(payload, dict)
            or str(payload.get("platform") or "").casefold() != expected_platform
            or not isinstance(directory, dict)
            or not str(directory.get("path") or "").strip()
            or (expected_name is not None and str(directory.get("name") or "").strip() != str(expected_name).strip())
            or not isinstance(fields, dict)
            or not fields
        ):
            return False
    if marker_path is None or not marker_path.is_file():
        return False
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        if expected_creator_key is not None and marker.get("creator_key") != expected_creator_key:
            return False
        if marker.get("feishu_writeback") == "disabled":
            return False
        if marker.get("signature") != mapping_cache_signature(paths):
            return False
    except (OSError, json.JSONDecodeError):
        return False
    return True


def summarize_metrics(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total_calls": len(metrics),
        "total_seconds": round(sum(float(item.get("seconds") or 0) for item in metrics), 3),
        "failed_calls": sum(1 for item in metrics if item.get("status") == "failed"),
        "calls": list(metrics),
    }


def backup_worker_count(config: dict[str, Any], args: argparse.Namespace, action_count: int) -> int:
    configured = args.backup_workers
    if configured is None:
        configured = section(config, "backups").get("max_workers", 3)
    try:
        workers = int(configured)
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"backups.max_workers 必须是整数: {configured}") from exc
    if workers < 1:
        raise PipelineError("备份并发数必须至少为 1。")
    return min(workers, max(1, action_count))


def run_independent_actions(
    actions: list[tuple[str, Callable[[], None]]], max_workers: int, dry_run: bool,
) -> dict[str, dict[str, Any]]:
    def invoke(action: Callable[[], None]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            action()
        except Exception as exc:
            return {
                "status": "failed",
                "error": str(exc),
                "duration_seconds": round(time.perf_counter() - started, 3),
            }
        return {
            "status": "planned" if dry_run else "success",
            "duration_seconds": round(time.perf_counter() - started, 3),
        }

    if not actions:
        return {}
    outcomes: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(actions)), thread_name_prefix="backup") as executor:
        futures = {executor.submit(invoke, action): stage for stage, action in actions}
        for future in as_completed(futures):
            outcomes[futures[future]] = future.result()
    return outcomes


def permanent_delivery_error(error: str) -> bool:
    text = str(error or "").lower()
    transient = (
        "timeout", "timed out", "429", "too many requests", "500", "502", "503", "504",
        "dns", "connection reset", "connection aborted", "temporarily unavailable", "网络超时",
        "连接中断", "临时",
    )
    if any(marker in text for marker in transient):
        return False
    permanent = (
        "credential", "cookie", "token invalid", "unauthorized", "forbidden", "permission denied",
        "access denied", "not configured", "missing config", "knowledge base", "folder mapping",
        "code 200005", "请求超量", "quota exceeded",
        "凭证", "登录态", "未配置", "配置缺失", "权限", "知识库不存在", "目录映射错误",
    )
    return any(marker in text for marker in permanent)

def prepare_work(
    config: dict[str, Any], creator: dict[str, Any], work: dict[str, Any],
    works_file: Path, profile_file: Path | None, state_dir: Path, media_dir: Path,
    runner: Runner, logger: Logger, env: dict[str, str], args: argparse.Namespace,
) -> dict[str, Any]:
    work_id = str(work["aweme_id"])
    state_path = state_dir / creator_key(creator) / f"{safe_key(work_id)}.json"
    state = load_state(state_path, creator, work)
    provider = str(chosen(creator, section(config, "asr"), "provider", "volcengine"))
    paths = artifact_paths(media_dir, work_id, provider)
    media_dir.mkdir(parents=True, exist_ok=True)

    if not args.dry_run:
        changed = False
        if status_of(state, "transcribed") != "success" and nonempty(paths["raw_text"]):
            set_status(state, "transcribed", "success", "检测到已有原始转写文件", True)
            changed = True
        if status_of(state, "corrected") != "success" and nonempty(paths["final"]):
            set_status(state, "corrected", "success", "检测到已有纠正后文案", True)
            changed = True
        if changed:
            write_json(state_path, state)

    logger.write(f"处理作品 {work_id}: {title_of(work)}")
    result: dict[str, Any] = {"aweme_id": work_id, "title": title_of(work), "stages": {}}
    source_url = audio_url(work)
    fallback_text = fallback_work_text(work)
    if not args.dry_run and not source_url and not nonempty(paths["raw_text"]) and fallback_text:
        paths["raw_text"].parent.mkdir(parents=True, exist_ok=True)
        paths["raw_text"].write_text(fallback_text, encoding="utf-8")
        paths["raw_json"].write_text(json.dumps({
            "strategy": "work-text-fallback",
            "aweme_id": work_id,
            "text": fallback_text,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        set_status(state, "transcribed", "success", "作品无可用音轨，使用发布文案", True)
        write_json(state_path, state)
        logger.write(f"作品 {work_id} 无可用音轨，使用发布文案作为原始文案")

    if args.skip_transcribe:
        result["stages"]["transcribed"] = "disabled"
    elif not source_url and not nonempty(paths["raw_text"]):
        message = "作品数据中没有 music_download_url"
        logger.write(f"转写失败 {work_id}: {message}")
        if not args.dry_run:
            set_status(state, "transcribed", "failed", message)
            write_json(state_path, state)
        result["stages"]["transcribed"] = "failed"
    else:
        result["stages"]["transcribed"] = execute_stage(
            "transcribed", f"音频转写 {work_id}", state, state_path, args, logger,
            lambda: runner.run(
                f"音频转写 {work_id}", transcribe_command(config, creator, source_url, paths), env,
                sensitive=("--audio-url", "--base-token"),
            ),
            (paths["raw_text"], paths["raw_json"]),
        )

    # Some Douyin posts are genuinely silent, while older signed audio URLs can
    # expire before a historical backfill reaches them. After ASR returns one of
    # these permanent source errors, use the published post text instead of
    # retrying the same unprocessable audio forever.
    if (
        result["stages"]["transcribed"] == "failed"
        and fallback_text
        and bool(section(config, "asr").get("fallback_to_work_text_on_source_error", True))
    ):
        failure_detail = str(state.get("stages", {}).get("transcribed", {}).get("detail") or "")
        if should_fallback_to_work_text(failure_detail):
            paths["raw_text"].parent.mkdir(parents=True, exist_ok=True)
            paths["raw_text"].write_text(fallback_text, encoding="utf-8")
            paths["raw_json"].write_text(json.dumps({
                "strategy": "work-text-fallback-after-asr-source-error",
                "aweme_id": work_id,
                "text": fallback_text,
                "asr_error": failure_detail,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            set_status(
                state,
                "transcribed",
                "success",
                "ASR source unavailable; used published work text",
                True,
            )
            write_json(state_path, state)
            result["stages"]["transcribed"] = "success"
            logger.write(f"Work {work_id}: ASR source unavailable; used published work text")

    transcribe_outcome = result["stages"]["transcribed"]
    if args.skip_correction:
        result["stages"]["corrected"] = "disabled"
    elif transcribe_outcome in {"failed", "blocked"}:
        result["stages"]["corrected"] = "blocked"
    elif transcribe_outcome == "disabled" and not nonempty(paths["raw_text"]):
        result["stages"]["corrected"] = "blocked"
    elif not args.dry_run and not nonempty(paths["raw_text"]):
        result["stages"]["corrected"] = "blocked"
    else:
        result["stages"]["corrected"] = execute_stage(
            "corrected", f"文案纠正 {work_id}", state, state_path, args, logger,
            lambda: runner.run(f"文案纠正 {work_id}", correction_command(config, creator, paths), env),
            (paths["final"], paths["report"]),
        )

    return result


def process_downstream(
    config: dict[str, Any], creator: dict[str, Any], work: dict[str, Any],
    works_file: Path, profile_file: Path | None, state_dir: Path, media_dir: Path,
    runner: Runner, logger: Logger, env: dict[str, str], args: argparse.Namespace,
    result: dict[str, Any], record_id: str | None = None,
) -> dict[str, Any]:
    work_id = str(work["aweme_id"])
    state_path = state_dir / creator_key(creator) / f"{safe_key(work_id)}.json"
    state = load_state(state_path, creator, work)
    provider = str(chosen(creator, section(config, "asr"), "provider", "volcengine"))
    paths = artifact_paths(media_dir, work_id, provider)
    correction_outcome = result["stages"].get("corrected", "blocked")
    final_available = (args.dry_run or nonempty(paths["final"])) and correction_outcome not in {"failed", "blocked"}
    if correction_outcome == "disabled":
        final_available = nonempty(paths["final"])
    transcript_file: Path | None = None
    if args.skip_feishu_writeback:
        result["stages"]["feishu_written_back"] = "disabled"
    elif not final_available:
        result["stages"]["feishu_written_back"] = "blocked"
    elif not getattr(args, "feishu_only", False) and should_skip(
        state, "feishu_written_back", args.resume, args.force_stage,
    ):
        logger.write(f"跳过 回写飞书 {work_id}：状态已完成")
        result["stages"]["feishu_written_back"] = "skipped"
    else:
        result["stages"]["feishu_written_back"] = "pending"
        transcript_file = paths["final"]

    backups: list[tuple[str, bool, str, Callable[[], None]]] = [
        (
            "ima_backed_up",
            args.skip_ima or getattr(args, "feishu_only", False) or not bool(section(config, "ima").get("enabled", True)),
            f"备份 IMA {work_id}",
            lambda: runner.run(f"备份 IMA {work_id}", ima_command(config, creator, paths), env),
        ),
        (
            "kuake_backed_up",
            args.skip_kuake or getattr(args, "feishu_only", False) or not bool(section(config, "kuake").get("enabled", True)),
            f"备份夸克 {work_id}",
            lambda: runner.run(f"备份夸克 {work_id}", kuake_command(config, creator, work, paths), env),
        ),
        (
            "obsidian_exported",
            args.skip_obsidian or getattr(args, "feishu_only", False) or not bool(section(config, "obsidian").get("enabled", True)),
            f"备份 Obsidian {work_id}",
            lambda: runner.run(
                f"备份 Obsidian {work_id}",
                obsidian_command(config, creator, work, works_file, profile_file, paths, args.overwrite), env,
            ),
        ),
    ]
    runnable: list[tuple[str, Callable[[], None]]] = []
    breakers = getattr(args, "_delivery_breakers", {})
    for stage, disabled, label, action in backups:
        if disabled:
            if getattr(args, "feishu_only", False):
                persisted = status_of(state, stage)
                result["stages"][stage] = (
                    "skipped" if persisted == "success"
                    else "failed" if persisted == "failed"
                    else "disabled"
                )
            else:
                result["stages"][stage] = "disabled"
        elif not final_available:
            result["stages"][stage] = "blocked"
        elif should_skip(state, stage, args.resume, args.force_stage):
            logger.write(f"跳过 {label}：状态已完成")
            result["stages"][stage] = "skipped"
        elif stage == "kuake_backed_up" and getattr(args, "_defer_kuake", False):
            result["stages"][stage] = "pending"
        elif stage in breakers:
            detail = str(breakers[stage])
            logger.write(f"熔断 {label}: {detail}")
            result["stages"][stage] = "failed"
            if not args.dry_run:
                set_status(state, stage, "failed", detail)
                write_json(state_path, state)
        else:
            runnable.append((stage, action))

    outcomes = run_independent_actions(
        runnable, backup_worker_count(config, args, len(runnable)), args.dry_run,
    )
    state_changed = False
    for stage, outcome in outcomes.items():
        status = str(outcome["status"])
        result["stages"][stage] = status
        if status == "failed":
            detail = str(outcome.get("error") or "")
            logger.write(f"失败 {stage}: {detail}")
            if permanent_delivery_error(detail):
                breakers[stage] = detail
        if not args.dry_run:
            set_status(
                state, stage, status, str(outcome.get("error") or ""),
                duration_seconds=float(outcome.get("duration_seconds") or 0),
            )
            state_changed = True
    if state_changed:
        write_json(state_path, state)
    if args.fail_fast and any(item.get("status") == "failed" for item in outcomes.values()):
        raise PipelineError(f"作品 {work_id} 存在备份失败阶段。")
    if getattr(args, "_defer_feishu_writeback", False):
        if args.skip_feishu_writeback:
            result["stages"]["backup_statuses_written_back"] = "disabled"
        elif finalizer_needed(
            result["stages"], transcript_file,
            status_of(state, "backup_statuses_written_back"), args.resume,
        ):
            result["stages"]["backup_statuses_written_back"] = "pending"
        else:
            result["stages"]["backup_statuses_written_back"] = "skipped"
        result["_batch"] = {
            "state_path": str(state_path),
            "transcript_file": str(transcript_file) if transcript_file is not None else "",
            "record_id": record_id or "",
        }
        return result
    if args.skip_feishu_writeback:
        result["stages"]["backup_statuses_written_back"] = "disabled"
        return result
    if not finalizer_needed(
        result["stages"], transcript_file,
        status_of(state, "backup_statuses_written_back"), args.resume,
    ):
        result["stages"]["backup_statuses_written_back"] = "skipped"
        return result

    finalizer_started = time.perf_counter()
    try:
        runner.run(
            f"合并回写文案与备份状态 {work_id}",
            status_writeback_command(
                config, creator, work, result["stages"], record_id, transcript_file,
            ),
            env,
            sensitive=("--table-id", "--base-token"),
        )
        finalizer_seconds = time.perf_counter() - finalizer_started
        if transcript_file is not None:
            result["stages"]["feishu_written_back"] = "planned" if args.dry_run else "success"
        result["stages"]["backup_statuses_written_back"] = "planned" if args.dry_run else "success"
        if not args.dry_run:
            set_combined_finalizer_status(
                state, transcript_included=transcript_file is not None,
                status="success", duration_seconds=finalizer_seconds,
            )
            write_json(state_path, state)
    except Exception as exc:
        if transcript_file is not None:
            result["stages"]["feishu_written_back"] = "failed"
        result["stages"]["backup_statuses_written_back"] = "failed"
        logger.write(f"回写备份状态失败 {work_id}: {exc}")
        if not args.dry_run:
            finalizer_seconds = time.perf_counter() - finalizer_started
            set_combined_finalizer_status(
                state, transcript_included=transcript_file is not None,
                status="failed", duration_seconds=finalizer_seconds, detail=str(exc),
            )
            write_json(state_path, state)
        if args.fail_fast:
            raise
    return result


def process_work(
    config: dict[str, Any], creator: dict[str, Any], work: dict[str, Any],
    works_file: Path, profile_file: Path | None, state_dir: Path, media_dir: Path,
    runner: Runner, logger: Logger, env: dict[str, str], args: argparse.Namespace,
) -> dict[str, Any]:
    """Run one work end-to-end; retained as the single-worker interface."""
    result = prepare_work(
        config, creator, work, works_file, profile_file, state_dir, media_dir,
        runner, logger, env, args,
    )
    return process_downstream(
        config, creator, work, works_file, profile_file, state_dir, media_dir,
        runner, logger, env, args, result, None,
    )


def asr_worker_count(config: dict[str, Any], args: argparse.Namespace, work_count: int) -> int:
    configured = args.asr_workers
    if configured is None:
        configured = section(config, "asr").get("max_workers", 4)
    try:
        workers = int(configured)
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"asr.max_workers 必须是整数: {configured}") from exc
    if workers < 1:
        raise PipelineError("ASR 并发数必须至少为 1。")
    return min(workers, max(1, work_count))


def collect_creator_phase(
    config: dict[str, Any], creator: dict[str, Any], runner: Runner,
    logger: Logger, env: dict[str, str], args: argparse.Namespace,
) -> dict[str, Any]:
    phase_started = time.perf_counter()
    key = creator_key(creator)
    name = str(creator.get("creator_name") or creator.get("creator_dir_name") or key)
    logger.write(f"========== 达人采集开始: {name} ({key}) ==========")
    works_file = path_from(creator.get("works_file"), PROJECT_DIR / "runtime" / f"{key}-works-from-mediacrawler.json")
    profile_file = path_from(creator.get("profile_file"))
    media_output = path_from(creator.get("media_output_dir"), PROJECT_DIR / "runtime" / f"mediacrawler-output-{key}")
    state_dir = path_from(config.get("state_dir"), DEFAULT_STATE_DIR) or DEFAULT_STATE_DIR
    media_dir = path_from(config.get("media_dir"), DEFAULT_MEDIA_DIR) or DEFAULT_MEDIA_DIR
    if works_file is None or media_output is None:
        raise PipelineError(f"达人 {key} 的路径配置无效。")
    collection_state_file = collection_state_path(state_dir, creator)
    result: dict[str, Any] = {
        "key": key, "creator_name": name, "works_file": str(works_file),
        "works": [], "phase_timings": {},
    }

    collection_ok = True
    collection_attempted = not args.skip_collect
    if args.skip_collect:
        logger.write(f"跳过采集 {name}")
    else:
        try:
            runner.run(
                f"采集 {name}",
                collect_command(
                    config, creator, works_file, media_output, collection_state_file,
                    args.normalize_only, args.force_full_collect,
                ),
                env,
            )
        except Exception as exc:
            collection_ok = False
            result["collection_error"] = str(exc)
            logger.write(f"采集失败 {name}: {exc}")
            if args.fail_fast:
                raise

    if not works_file.exists():
        if args.dry_run:
            logger.write(f"作品文件不存在，DRY-RUN 只展示到采集阶段: {works_file}")
            result["status"] = "planned_collection_only"
            result["phase_timings"]["collection_and_sync_seconds"] = round(
                time.perf_counter() - phase_started, 3,
            )
            return {"creator": creator, "result": result, "terminal": True, "name": name}
        raise PipelineError(f"作品文件不存在: {works_file}")

    all_works = load_works(works_file)
    maximum = args.max_works if args.max_works is not None else int(config.get("max_works") or 0)
    requested_ids = set(args.aweme_id)
    pending_selection = False
    if requested_ids:
        selected = select_works(all_works, requested_ids, maximum)
    elif args.backfill_existing:
        selected = select_backfill_works(
            config, creator, all_works, state_dir, media_dir, args, maximum,
        )
        result["backfill_remaining_before_run"] = len(select_backfill_works(
            config, creator, all_works, state_dir, media_dir, args, 0,
        ))
    elif collection_attempted:
        pending_selection = True
        pending_ids = pending_collection_ids(collection_state_file, works_file)
        selected = select_works(all_works, set(pending_ids), maximum) if pending_ids else []
        result["pending_count"] = len(pending_ids)
    else:
        selected = select_works(all_works, set(), maximum)
    result["selected_count"] = len(selected)
    logger.write(f"{name} 本轮选择 {len(selected)} / {len(all_works)} 条作品")

    if not selected:
        result["backup_mappings"] = {}
        result["status"] = "success" if collection_ok else "partial_failure"
        reason = "没有待处理的新作品" if pending_selection else "没有待补录的历史作品"
        logger.write(f"{name} {reason}，跳过飞书、ASR 和备份阶段")
        result["phase_timings"]["collection_and_sync_seconds"] = round(
            time.perf_counter() - phase_started, 3,
        )
        return {"creator": creator, "result": result, "terminal": True, "name": name}

    if not args.dry_run:
        for work in selected:
            state_path = state_dir / key / f"{safe_key(str(work['aweme_id']))}.json"
            state = load_state(state_path, creator, work)
            inferred = args.skip_collect or not collection_ok
            set_status(
                state, "collected", "success",
                "使用已有作品文件" if inferred else "本轮采集作品文件已加载", inferred,
            )
            write_json(state_path, state)

    sync_ok = True
    synced_record_ids: dict[str, str] = {}
    sync_works_file = works_file
    should_limit_sync = bool(requested_ids) or maximum > 0 or pending_selection
    if should_limit_sync and not args.dry_run:
        sync_works_file = write_selected_works_file(
            works_file, selected, state_dir / "selected_works" / f"{key}.json",
        )
    if should_limit_sync:
        logger.write(f"飞书作品同步使用本轮选择作品文件: {sync_works_file}")
    if args.skip_feishu_sync:
        logger.write(f"跳过飞书作品同步 {name}")
    else:
        try:
            sync_output = runner.run(
                f"飞书作品同步 {name}", sync_command(config, creator, sync_works_file), env,
                sensitive=("--table-id", "--base-token"),
            )
            synced_record_ids = parse_sync_record_ids(sync_output)
        except Exception as exc:
            sync_ok = False
            result["feishu_sync_error"] = str(exc)
            logger.write(f"飞书作品同步失败 {name}: {exc}")
            if args.fail_fast:
                raise

    if not args.dry_run and not args.skip_feishu_sync:
        for work in selected:
            state_path = state_dir / key / f"{safe_key(str(work['aweme_id']))}.json"
            state = load_state(state_path, creator, work)
            set_status(state, "feishu_synced", "success" if sync_ok else "failed", result.get("feishu_sync_error", ""))
            write_json(state_path, state)

    result["phase_timings"]["collection_and_sync_seconds"] = round(
        time.perf_counter() - phase_started, 3,
    )
    logger.write(f"========== 达人采集结束: {name}，进入文案队列 ==========")
    return {
        "creator": creator,
        "key": key,
        "name": name,
        "works_file": works_file,
        "profile_file": profile_file,
        "state_dir": state_dir,
        "media_dir": media_dir,
        "collection_state_file": collection_state_file,
        "result": result,
        "selected": selected,
        "collection_ok": collection_ok,
        "sync_ok": sync_ok,
        "synced_record_ids": synced_record_ids,
        "terminal": False,
    }


def parse_batch_results(output: str) -> dict[str, dict[str, Any]]:
    if not output.strip():
        return {}
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {}
    values = payload.get("results") if isinstance(payload, dict) else None
    return values if isinstance(values, dict) else {}


def read_batch_results(path: Path, expected_batch_id: str = "") -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = read_json(path)
    except PipelineError:
        return {}
    if expected_batch_id and str(payload.get("batch_id") or "") != expected_batch_id:
        return {}
    values = payload.get("results") if isinstance(payload, dict) else None
    return values if isinstance(values, dict) else {}


def work_has_failed_stage(stages: dict[str, Any], *, feishu_only: bool = False) -> bool:
    if feishu_only:
        if stages.get("downstream") == "failed":
            return True
        return any(
            stages.get(stage) not in {"success", "planned"}
            for stage in ("feishu_written_back", "backup_statuses_written_back")
        )
    return "failed" in stages.values()


def finalize_creator_batches(
    config: dict[str, Any], creator: dict[str, Any], selected: list[dict[str, Any]],
    delivery_results: dict[str, dict[str, Any]], state_dir: Path, runner: Runner,
    logger: Logger, env: dict[str, str], args: argparse.Namespace,
) -> dict[str, float]:
    batch_dir = state_dir / "batches" / creator_key(creator)
    work_by_id = {str(work["aweme_id"]): work for work in selected}
    timings: dict[str, float] = {}

    kuake_ids = [
        work_id for work_id, item in delivery_results.items()
        if item.get("stages", {}).get("kuake_backed_up") == "pending"
    ]
    if kuake_ids:
        manifest_path = batch_dir / "kuake-upload.json"
        result_path = batch_dir / "kuake-upload-result.json"
        payload = {
            "creator_name": str(creator.get("creator_dir_name") or creator.get("creator_name") or ""),
            "files": [
                {
                    "aweme_id": work_id,
                    "path": delivery_results[work_id]["_batch"]["transcript_file"],
                    "video_date": date_of(work_by_id[work_id]),
                    "title": safe_external_title(work_by_id[work_id]),
                }
                for work_id in kuake_ids
            ],
        }
        batch_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        payload["batch_id"] = batch_id
        payload["result_file"] = str(result_path)
        if not args.dry_run:
            write_json(manifest_path, payload)
        batch_started = time.perf_counter()
        batch_results: dict[str, dict[str, Any]] = {}
        batch_error = ""
        try:
            output = runner.run(
                f"夸克达人级批量上传 {creator_key(creator)} ({len(kuake_ids)}条)",
                kuake_batch_command(config, creator, manifest_path), env,
            )
            batch_results = parse_batch_results(output) or read_batch_results(result_path, batch_id)
        except Exception as exc:
            batch_error = str(exc)
            batch_results = read_batch_results(result_path, batch_id)
            logger.write(f"夸克达人级批量上传失败: {batch_error}")
        batch_seconds = time.perf_counter() - batch_started
        timings["kuake_batch_seconds"] = round(batch_seconds, 3)
        duration_share = batch_seconds / len(kuake_ids)
        operation_id = f"kuake-batch-{time.time_ns()}"
        for work_id in kuake_ids:
            outcome = batch_results.get(work_id, {})
            if args.dry_run:
                status = "planned"
            elif outcome.get("status") == "success":
                status = "success"
            else:
                status = "failed"
            detail = str(outcome.get("error") or batch_error or "批量上传未返回该作品结果")
            delivery_results[work_id]["stages"]["kuake_backed_up"] = status
            if not args.dry_run:
                state_path = Path(delivery_results[work_id]["_batch"]["state_path"])
                state = load_state(state_path, creator, work_by_id[work_id])
                set_status(state, "kuake_backed_up", status, detail, duration_seconds=duration_share)
                state["stages"]["kuake_backed_up"]["operation_id"] = operation_id
                state["stages"]["kuake_backed_up"]["batch_duration_seconds"] = round(batch_seconds, 3)
                write_json(state_path, state)
        if batch_error and args.fail_fast:
            raise PipelineError(batch_error)

    feishu_ids = [
        work_id for work_id, item in delivery_results.items()
        if item.get("stages", {}).get("backup_statuses_written_back") == "pending"
    ]
    if feishu_ids:
        manifest_path = batch_dir / "feishu-final-writeback.json"
        result_path = batch_dir / "feishu-final-writeback-result.json"
        payload = {
            "table_id": str(creator.get("works_table_id") or ""),
            "records": [
                {
                    "work_id": work_id,
                    "record_id": delivery_results[work_id]["_batch"].get("record_id") or "",
                    "transcript_file": delivery_results[work_id]["_batch"].get("transcript_file") or "",
                    "ima_status": final_backup_status(str(delivery_results[work_id]["stages"].get("ima_backed_up", "blocked")), "已上传"),
                    "kuake_status": final_backup_status(str(delivery_results[work_id]["stages"].get("kuake_backed_up", "blocked")), "已上传"),
                    "local_status": final_backup_status(str(delivery_results[work_id]["stages"].get("obsidian_exported", "blocked")), "已写入"),
                }
                for work_id in feishu_ids
            ],
        }
        batch_id = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        payload["batch_id"] = batch_id
        payload["result_file"] = str(result_path)
        if not args.dry_run:
            write_json(manifest_path, payload)
        batch_started = time.perf_counter()
        batch_results = {}
        batch_error = ""
        try:
            output = runner.run(
                f"飞书最终结果批量写回 {creator_key(creator)} ({len(feishu_ids)}条)",
                status_writeback_batch_command(config, creator, manifest_path), env,
                sensitive=("--table-id", "--base-token"),
            )
            batch_results = parse_batch_results(output) or read_batch_results(result_path, batch_id)
        except Exception as exc:
            batch_error = str(exc)
            batch_results = read_batch_results(result_path, batch_id)
            logger.write(f"飞书最终结果批量写回失败: {batch_error}")
        batch_seconds = time.perf_counter() - batch_started
        timings["feishu_batch_seconds"] = round(batch_seconds, 3)
        duration_share = batch_seconds / len(feishu_ids)
        operation_id = f"feishu-batch-{time.time_ns()}"
        for work_id in feishu_ids:
            outcome = batch_results.get(work_id, {})
            if args.dry_run:
                status = "planned"
            elif outcome.get("status") == "success":
                status = "success"
            else:
                status = "failed"
            detail = str(outcome.get("error") or batch_error or "批量写回未返回该作品结果")
            transcript_included = bool(delivery_results[work_id]["_batch"].get("transcript_file"))
            delivery_results[work_id]["stages"]["backup_statuses_written_back"] = status
            if transcript_included:
                delivery_results[work_id]["stages"]["feishu_written_back"] = status
            if not args.dry_run:
                state_path = Path(delivery_results[work_id]["_batch"]["state_path"])
                state = load_state(state_path, creator, work_by_id[work_id])
                set_combined_finalizer_status(
                    state, transcript_included=transcript_included, status=status,
                    duration_seconds=duration_share, detail=detail, operation_id=operation_id,
                )
                target_stage = "feishu_written_back" if transcript_included else "backup_statuses_written_back"
                state["stages"][target_stage]["batch_duration_seconds"] = round(batch_seconds, 3)
                write_json(state_path, state)
        if batch_error and args.fail_fast:
            raise PipelineError(batch_error)

    for item in delivery_results.values():
        item.pop("_batch", None)
    return timings


def process_creator_phase(
    config: dict[str, Any], context: dict[str, Any], runner: Runner,
    logger: Logger, env: dict[str, str], args: argparse.Namespace,
) -> dict[str, Any]:
    phase_started = time.perf_counter()
    creator = context["creator"]
    result = context["result"]
    name = str(context.get("name") or creator.get("creator_name") or creator_key(creator))
    logger.write(f"========== 达人文案处理开始: {name} ==========")
    if context.get("terminal"):
        result.setdefault("phase_timings", {})["transcript_processing_seconds"] = 0.0
        logger.write(f"========== 达人结束: {name}，状态 {result['status']} ==========")
        return result

    works_file = context["works_file"]
    profile_file = context["profile_file"]
    state_dir = context["state_dir"]
    media_dir = context["media_dir"]
    collection_state_file = context["collection_state_file"]
    selected = context["selected"]
    collection_ok = bool(context["collection_ok"])
    sync_ok = bool(context["sync_ok"])
    synced_record_ids = context["synced_record_ids"]
    work_by_id = {str(work["aweme_id"]): work for work in selected}
    delivery_results: dict[str, dict[str, Any]] = {}
    args._defer_kuake = True
    args._defer_feishu_writeback = True
    args._delivery_breakers = {}

    def failed_preparation(work_id: str, exc: Exception) -> dict[str, Any]:
        logger.write(f"作品预处理异常 {work_id}: {exc}")
        detail = str(exc)
        if not args.dry_run:
            state_path = state_dir / creator_key(creator) / f"{safe_key(work_id)}.json"
            persist_work_failure(
                state_path, creator, work_by_id[work_id], "transcribed", detail,
                blocked_stages=("corrected",),
            )
        return {
            "aweme_id": work_id,
            "title": title_of(work_by_id[work_id]),
            "stages": {"transcribed": "failed", "corrected": "blocked"},
            "error": detail,
        }

    def sync_mappings_timed() -> tuple[dict[str, str] | None, float, Exception | None]:
        started = time.perf_counter()
        try:
            mappings = sync_creator_backup_mappings(
                config, creator, profile_file, state_dir, runner, logger, env, args,
            )
            return mappings, time.perf_counter() - started, None
        except Exception as exc:
            return None, time.perf_counter() - started, exc

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="creator-mapping") as mapping_executor:
        mapping_future = mapping_executor.submit(sync_mappings_timed)
        mappings_ready = False

        def wait_for_mappings() -> None:
            nonlocal mappings_ready
            if mappings_ready:
                return
            mappings, mapping_seconds, mapping_error = mapping_future.result()
            result.setdefault("phase_timings", {})["backup_mapping_seconds"] = round(mapping_seconds, 3)
            mappings_ready = True
            if mapping_error is None:
                result["backup_mappings"] = mappings or {}
                return
            logger.write(f"达人目录映射确认失败 {name}: {mapping_error}")
            result["backup_mappings"] = {"mapping_sync": "failed"}
            result["backup_mapping_error"] = str(mapping_error)
            if getattr(args, "fail_fast", False):
                raise mapping_error

        def deliver(work_id: str, prepared: dict[str, Any]) -> None:
            wait_for_mappings()
            work = work_by_id[work_id]
            try:
                work_result = process_downstream(
                    config, creator, work, works_file, profile_file, state_dir, media_dir,
                    runner, logger, env, args, prepared, synced_record_ids.get(work_id),
                )
            except Exception as exc:
                logger.write(f"Work downstream processing failed {work_id}: {exc}")
                work_result = dict(prepared)
                work_result["stages"] = dict(work_result.get("stages", {}))
                work_result["stages"]["downstream"] = "failed"
                work_result["error"] = str(exc)
                if not args.dry_run:
                    state_path = state_dir / creator_key(creator) / f"{safe_key(work_id)}.json"
                    persist_work_failure(
                        state_path, creator, work, "downstream", str(exc),
                    )
            delivery_results[work_id] = work_result
            if getattr(args, "fail_fast", False) and "failed" in work_result["stages"].values():
                raise PipelineError(f"Work {work_result['aweme_id']} has a failed stage.")

        workers = asr_worker_count(config, args, len(selected))
        logger.write(f"{name} ASR/文案纠正并发数: {workers}")
        if workers == 1:
            for work in selected:
                work_id = str(work["aweme_id"])
                try:
                    prepared = prepare_work(
                        config, creator, work, works_file, profile_file, state_dir, media_dir,
                        runner, logger, env, args,
                    )
                except Exception as exc:
                    prepared = failed_preparation(work_id, exc)
                deliver(work_id, prepared)
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="asr") as executor:
                futures = {
                    executor.submit(
                        prepare_work,
                        config, creator, work, works_file, profile_file, state_dir, media_dir,
                        runner, logger, env, args,
                    ): str(work["aweme_id"])
                    for work in selected
                }
                for future in as_completed(futures):
                    work_id = futures[future]
                    try:
                        prepared = future.result()
                    except Exception as exc:
                        prepared = failed_preparation(work_id, exc)
                    try:
                        deliver(work_id, prepared)
                    except Exception:
                        for pending in futures:
                            pending.cancel()
                        raise
        wait_for_mappings()

    batch_timings = finalize_creator_batches(
        config, creator, selected, delivery_results, state_dir, runner, logger, env, args,
    )
    result.setdefault("phase_timings", {}).update(batch_timings)
    result["works"] = [delivery_results[str(work["aweme_id"])] for work in selected]
    failed_work = any(
        work_has_failed_stage(item["stages"], feishu_only=getattr(args, "feishu_only", False))
        for item in result["works"]
    )
    mapping_failed = "failed" in result.get("backup_mappings", {}).values()
    result["status"] = "partial_failure" if (not collection_ok or not sync_ok or mapping_failed or failed_work) else "success"
    if result["status"] == "success" and not args.dry_run:
        complete_collection_pending(
            collection_state_file, works_file, [str(work["aweme_id"]) for work in selected],
        )
    result.setdefault("phase_timings", {})["transcript_processing_seconds"] = round(
        time.perf_counter() - phase_started, 3,
    )
    logger.write(f"========== 达人结束: {name}，状态 {result['status']} ==========")
    return result

def process_creator_phase_safely(
    config: dict[str, Any], context: dict[str, Any], runner: Runner,
    logger: Logger, env: dict[str, str], args: argparse.Namespace,
) -> dict[str, Any]:
    """Record a creator-level failure and let the serial consumer advance."""
    started = time.perf_counter()
    try:
        return process_creator_phase(config, context, runner, logger, env, args)
    except Exception as exc:
        creator = context["creator"]
        result = context.get("result")
        if not isinstance(result, dict):
            result = {"key": creator_key(creator), "works": []}
        result["status"] = "failed"
        result["error"] = str(exc)
        result.setdefault("works", [])
        result.setdefault("phase_timings", {})["transcript_processing_seconds"] = round(
            time.perf_counter() - started, 3,
        )
        logger.write(f"达人文案处理失败 {creator_key(creator)}: {exc}")
        return result


def collection_context_error(context: dict[str, Any]) -> str | None:
    """Return the collection failure reason carried by a normal phase result."""
    result = context.get("result")
    if not isinstance(result, dict):
        return "采集阶段返回了无效结果"
    if context.get("collection_ok") is False:
        return str(result.get("collection_error") or "采集阶段返回 collection_ok=false")
    if result.get("status") in {"failed", "partial_failure"} and result.get("collection_error"):
        return str(result["collection_error"])
    return None


def process_creator(
    config: dict[str, Any], creator: dict[str, Any], runner: Runner,
    logger: Logger, env: dict[str, str], args: argparse.Namespace,
) -> dict[str, Any]:
    """Run one creator sequentially; retained for fail-fast and direct callers."""
    context = collect_creator_phase(config, creator, runner, logger, env, args)
    return process_creator_phase(config, context, runner, logger, env, args)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行抖音达人文案完整流水线")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="流水线 JSON 配置，默认 local/pipeline.json")
    parser.add_argument("--creator", action="append", default=[], help="只运行指定达人 key，可重复")
    parser.add_argument("--aweme-id", action="append", default=[], help="只处理指定作品 ID，可重复")
    parser.add_argument("--max-works", type=int, default=None, help="每个达人最多处理条数；0 表示不限")
    parser.add_argument(
        "--collect-workers", type=int, default=3,
        help="达人信息采集并发数；默认为 3，并发失败后逐个串行重试",
    )
    parser.add_argument(
        "--asr-workers", type=int, default=None,
        help="视频转音频和 ASR 并发数；默认读取 asr.max_workers，未配置时为 4",
    )
    parser.add_argument(
        "--backup-workers", type=int, default=None,
        help="IMA/夸克/Obsidian 独立备份并发数；默认读取 backups.max_workers，未配置时为 3",
    )
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--normalize-only", action="store_true", help="采集阶段只规范化已有 MediaCrawler 输出")
    parser.add_argument("--force-full-collect", action="store_true", help="忽略全量基线标记，强制重新抓取全部历史作品")
    parser.add_argument(
        "--backfill-existing", action="store_true",
        help="只选择缺少最终文案或尚未完成已启用备份的本地历史作品；支持 --max-works 分批续跑",
    )
    parser.add_argument("--skip-feishu-sync", action="store_true")
    parser.add_argument("--skip-transcribe", action="store_true")
    parser.add_argument("--skip-correction", action="store_true")
    parser.add_argument("--skip-feishu-writeback", action="store_true")
    parser.add_argument("--skip-ima", action="store_true")
    parser.add_argument("--skip-kuake", action="store_true")
    parser.add_argument("--skip-obsidian", action="store_true")
    parser.add_argument(
        "--feishu-only", action="store_true",
        help="??????????????????????????????????????",
    )
    parser.add_argument(
        "--refresh-mappings", action="store_true",
        help="忽略达人目录映射缓存，重新确认 IMA/夸克/Obsidian 目录并回写飞书",
    )
    parser.add_argument(
        "--force-stage", action="append", choices=["all", *STAGES], default=[],
        help="强制重跑指定阶段，可重复",
    )
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="忽略成功状态并重跑未禁用阶段")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已有 Obsidian 笔记")
    parser.add_argument("--fail-fast", action="store_true", help="任一阶段失败立即停止；默认继续其他备份和作品")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不执行外部命令、不写状态")
    parser.set_defaults(resume=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    wall_started = time.perf_counter()
    started_at = now_text()
    run_id = datetime.now(BEIJING_TZ).strftime("%Y%m%d-%H%M%S")
    config: dict[str, Any] | None = None
    runner: Runner | None = None
    args = build_parser().parse_args(argv)
    if args.feishu_only:
        args.skip_collect = True
        args.skip_transcribe = True
        args.skip_correction = True
    try:
        config_path = path_from(args.config)
        if config_path is None:
            raise PipelineError("配置文件路径无效。")
        config = read_json(config_path)
        creators = config.get("creators")
        if not isinstance(creators, list) or not creators:
            raise PipelineError(f"配置文件没有 creators 数组: {config_path}")
        creators = [item for item in creators if isinstance(item, dict) and item.get("enabled", True)]
        if args.creator:
            requested = set(args.creator)
            creators = [item for item in creators if creator_key(item) in requested]
            found = {creator_key(item) for item in creators}
            if requested - found:
                raise PipelineError(f"配置中找不到或未启用达人: {sorted(requested - found)}")
        if not creators:
            raise PipelineError("没有需要运行的达人。")

        log_dir = path_from(config.get("log_dir"), DEFAULT_LOG_DIR) or DEFAULT_LOG_DIR
        logger = Logger(log_dir / f"pipeline-{run_id}.log", persist=not args.dry_run)
        runner = Runner(logger, args.dry_run)
        env = child_env(config)
        logger.write(f"流水线开始，配置={config_path}，dry_run={args.dry_run}")
        summary: dict[str, Any] = {
            "run_id": run_id, "started_at": started_at, "config": str(config_path),
            "dry_run": args.dry_run, "creators": [],
        }

        if args.fail_fast:
            # Preserve immediate-stop semantics when explicitly requested.
            for creator in creators:
                try:
                    creator_result = process_creator(config, creator, runner, logger, env, args)
                except Exception as exc:
                    logger.write(f"达人任务失败 {creator_key(creator)}: {exc}")
                    creator_result = {
                        "key": creator_key(creator), "status": "failed",
                        "error": str(exc), "works": [],
                    }
                    summary["creators"].append(creator_result)
                    break
                else:
                    summary["creators"].append(creator_result)
        else:
            collect_workers = int(args.collect_workers)
            if collect_workers < 1:
                raise PipelineError("--collect-workers 必须至少为 1。")

            # Creator collection is a bounded parallel producer. Successful creators
            # enter the single-consumer transcript queue immediately. Only creators
            # that fail the parallel attempt are retried once, serially, after the
            # initial collection batch has settled.
            collection_failures: dict[str, tuple[Exception, float]] = {}
            collection_results: dict[str, dict[str, Any]] = {}
            processing_futures: dict[str, Any] = {}
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="creator-transcripts") as transcript_executor:
                worker_count = min(collect_workers, len(creators))
                with ThreadPoolExecutor(
                    max_workers=worker_count, thread_name_prefix="creator-collection",
                ) as collection_executor:
                    collection_started: dict[Any, float] = {}
                    future_creators: dict[Any, dict[str, Any]] = {}
                    for creator in creators:
                        future = collection_executor.submit(
                            collect_creator_phase, config, creator, runner, logger, env, args,
                        )
                        collection_started[future] = time.perf_counter()
                        future_creators[future] = creator

                    for future in as_completed(future_creators):
                        creator = future_creators[future]
                        key = creator_key(creator)
                        attempt_seconds = time.perf_counter() - collection_started[future]
                        try:
                            context = future.result()
                        except Exception as exc:
                            collection_failures[key] = (exc, attempt_seconds)
                            logger.write(f"达人并发采集失败 {key}: {exc}")
                            continue
                        context_error = collection_context_error(context)
                        if context_error is not None:
                            collection_failures[key] = (PipelineError(context_error), attempt_seconds)
                            logger.write(f"达人并发采集失败 {key}: {context_error}")
                            continue
                        result = context["result"]
                        result["collection_attempts"] = 1
                        result["fallback_to_serial"] = False
                        result.setdefault("phase_timings", {})["parallel_collection_seconds"] = round(
                            attempt_seconds, 3,
                        )
                        collection_results[key] = result
                        processing_futures[key] = transcript_executor.submit(
                            process_creator_phase_safely, config, context, runner, logger, env, args,
                        )

                for creator in creators:
                    key = creator_key(creator)
                    failure = collection_failures.get(key)
                    if failure is None:
                        continue
                    initial_error, parallel_seconds = failure
                    logger.write(f"串行补采开始 {key}，并发失败原因: {initial_error}")
                    retry_started = time.perf_counter()
                    try:
                        context = collect_creator_phase(config, creator, runner, logger, env, args)
                    except Exception as exc:
                        retry_seconds = time.perf_counter() - retry_started
                        logger.write(f"达人串行补采失败 {key}: {exc}")
                        collection_results[key] = {
                            "key": key, "status": "failed", "error": str(exc), "works": [],
                            "collection_attempts": 2, "fallback_to_serial": True,
                            "parallel_collection_error": str(initial_error),
                            "phase_timings": {
                                "parallel_collection_seconds": round(parallel_seconds, 3),
                                "serial_retry_seconds": round(retry_seconds, 3),
                                "collection_and_sync_seconds": round(parallel_seconds + retry_seconds, 3),
                            },
                        }
                        continue

                    retry_seconds = time.perf_counter() - retry_started
                    result = context["result"]
                    timings = result.setdefault("phase_timings", {})
                    timings["parallel_collection_seconds"] = round(parallel_seconds, 3)
                    timings["serial_retry_seconds"] = round(retry_seconds, 3)
                    timings["collection_and_sync_seconds"] = round(parallel_seconds + retry_seconds, 3)
                    result["collection_attempts"] = 2
                    result["fallback_to_serial"] = True
                    result["parallel_collection_error"] = str(initial_error)
                    collection_results[key] = result
                    retry_error = collection_context_error(context)
                    if retry_error is not None:
                        logger.write(f"达人串行补采失败 {key}: {retry_error}")
                        continue
                    processing_futures[key] = transcript_executor.submit(
                        process_creator_phase_safely, config, context, runner, logger, env, args,
                    )

                for creator in creators:
                    key = creator_key(creator)
                    future = processing_futures.get(key)
                    if future is None:
                        summary["creators"].append(collection_results[key])
                    else:
                        summary["creators"].append(future.result())

        summary["finished_at"] = now_text()
        summary["wall_seconds"] = round(time.perf_counter() - wall_started, 3)
        summary["timings"] = summarize_metrics(runner.metrics)
        failed = any(item.get("status") in {"failed", "partial_failure"} for item in summary["creators"])
        summary["status"] = "partial_failure" if failed else ("planned" if args.dry_run else "success")
        if not args.dry_run:
            state_dir = path_from(config.get("state_dir"), DEFAULT_STATE_DIR) or DEFAULT_STATE_DIR
            write_json(state_dir / "runs" / f"{run_id}.json", summary)
        logger.write(f"流水线结束，状态={summary['status']}")
        print_console(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if failed else 0
    except PipelineError as exc:
        failure_summary = {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": now_text(),
            "status": "failed",
            "error": str(exc),
            "wall_seconds": round(time.perf_counter() - wall_started, 3),
            "timings": summarize_metrics(runner.metrics if runner is not None else []),
        }
        if config is not None and not args.dry_run:
            try:
                state_dir = path_from(config.get("state_dir"), DEFAULT_STATE_DIR) or DEFAULT_STATE_DIR
                write_json(state_dir / "runs" / f"{run_id}.json", failure_summary)
            except (OSError, PipelineError):
                pass
        print_console(json.dumps(failure_summary, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
