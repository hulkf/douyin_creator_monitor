#!/usr/bin/env python3
"""Local web control panel for the Douyin creator monitor."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable, NamedTuple
from urllib.parse import urlsplit


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import gui_dashboard as GUI  # noqa: E402
import run_creator_pipeline as PIPELINE  # noqa: E402
from collect_douyin_creator_with_mediacrawler import (  # noqa: E402
    close_new_blank_chrome_windows,
    find_unrelated_user_chrome_pids,
    visible_blank_chrome_windows,
)


CONFIG_RELATIVE_PATH = Path("local") / "pipeline.json"
CONFIG_BACKUP_RELATIVE_PATH = Path("local") / "pipeline.backup.json"
CONFIG_TEMPLATE_RELATIVE_PATH = Path("config") / "pipeline.example.json"
WEB_DIR_RELATIVE_PATH = Path("web")
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_HISTORY_FILES = 100
DEFAULT_HISTORY_LIMIT = 30
CREATOR_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_CONFIG_LOCK = threading.Lock()
_ACCOUNT_LOGIN_LOCK = threading.Lock()
_ACCOUNT_LOGIN_PROCESSES: dict[str, subprocess.Popen[Any]] = {}


class WebDashboardError(RuntimeError):
    pass


class LoadedConfig(NamedTuple):
    config: dict[str, Any]
    path: Path
    exists: bool


class SavedConfig(NamedTuple):
    path: Path
    backup_path: Path | None


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise WebDashboardError(f"找不到配置文件：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise WebDashboardError(f"无法解析配置文件 {path.name}：{exc}") from exc
    if not isinstance(payload, dict):
        raise WebDashboardError(f"配置文件根节点必须是 JSON 对象：{path}")
    return payload


def merge_defaults(defaults: Any, value: Any) -> Any:
    """Merge object defaults while treating arrays as complete user values."""
    if not isinstance(defaults, dict) or not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, default_value in defaults.items():
        if key in value:
            result[key] = merge_defaults(default_value, value[key])
        else:
            result[key] = default_value
    for key, child in value.items():
        if key not in result:
            result[key] = child
    return result


def load_pipeline_config(project_dir: Path = PROJECT_DIR) -> LoadedConfig:
    project_dir = project_dir.resolve()
    path = project_dir / CONFIG_RELATIVE_PATH
    template_path = project_dir / CONFIG_TEMPLATE_RELATIVE_PATH
    template = read_json_object(template_path)
    if not path.exists():
        return LoadedConfig(template, path, False)
    current = read_json_object(path)
    return LoadedConfig(merge_defaults(template, current), path, True)


def _require_object(config: dict[str, Any], key: str, errors: list[str]) -> dict[str, Any]:
    value = config.get(key, {})
    if not isinstance(value, dict):
        errors.append(f"{key} 必须是对象")
        return {}
    return value


def _check_integer(
    source: dict[str, Any],
    key: str,
    label: str,
    errors: list[str],
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> None:
    if key not in source:
        return
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{label} 必须是整数")
        return
    if value < minimum or (maximum is not None and value > maximum):
        range_text = f"{minimum} 到 {maximum}" if maximum is not None else f"不小于 {minimum}"
        errors.append(f"{label} 必须{range_text}")


def _check_number(
    source: dict[str, Any],
    key: str,
    label: str,
    errors: list[str],
    *,
    minimum: float = 0,
) -> None:
    if key not in source:
        return
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{label} 必须是数字")
    elif value < minimum:
        errors.append(f"{label} 必须不小于 {minimum:g}")


def _check_boolean(source: dict[str, Any], key: str, label: str, errors: list[str]) -> None:
    if key in source and not isinstance(source[key], bool):
        errors.append(f"{label} 必须是开关值")


def _check_required_text(
    source: dict[str, Any],
    key: str,
    label: str,
    errors: list[str],
    *,
    expected: str | None = None,
) -> None:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} 不能为空")
    elif expected is not None and value.strip() != expected:
        errors.append(f"{label} 必须是 {expected}")


def validate_pipeline_config(config: Any) -> list[str]:
    if not isinstance(config, dict):
        return ["配置根节点必须是 JSON 对象"]

    errors: list[str] = []
    _check_integer(config, "max_works", "max_works", errors, minimum=0)

    collection = _require_object(config, "collection", errors)
    for key, minimum in (
        ("incremental_probe_count", 1),
        ("cdp_port_stride", 1),
        ("max_count", 1),
        ("expect_min_count", 0),
        ("profile_max_workers", 1),
        ("browser_window_width", 200),
        ("browser_window_height", 150),
    ):
        _check_integer(collection, key, f"collection.{key}", errors, minimum=minimum)
    _check_integer(
        collection,
        "cdp_port_start",
        "collection.cdp_port_start",
        errors,
        minimum=1,
        maximum=65535,
    )
    _check_number(
        collection,
        "profile_ttl_hours",
        "collection.profile_ttl_hours",
        errors,
        minimum=0,
    )
    for key in ("incremental_enabled", "clean_media_output", "headless", "per_creator_profile_pool"):
        _check_boolean(collection, key, f"collection.{key}", errors)
    profiles = collection.get("account_profiles", [])
    if not isinstance(profiles, list) or any(not isinstance(item, str) for item in profiles):
        errors.append("collection.account_profiles 必须是字符串数组")
    elif any(not item.strip() or not CREATOR_KEY_PATTERN.fullmatch(item.strip()) for item in profiles):
        errors.append("collection.account_profiles 只能包含非空的字母、数字、下划线和连字符")
    elif len({item.strip() for item in profiles}) != len(profiles):
        errors.append("collection.account_profiles 不能包含重复账号槽位")

    feishu = _require_object(config, "feishu", errors)
    _check_required_text(feishu, "creator_table_id", "feishu.creator_table_id", errors)
    _check_required_text(
        feishu,
        "work_id_field",
        "feishu.work_id_field",
        errors,
        expected="抖音作品ID",
    )

    asr = _require_object(config, "asr", errors)
    _check_integer(asr, "max_workers", "asr.max_workers", errors, minimum=1)
    summary = _require_object(config, "summary", errors)
    _check_boolean(summary, "enabled", "summary.enabled", errors)
    _check_number(summary, "temperature", "summary.temperature", errors, minimum=0)
    for key, minimum in (
        ("max_tokens", 1),
        ("timeout", 1),
        ("retry_attempts", 1),
    ):
        _check_integer(summary, key, f"summary.{key}", errors, minimum=minimum)
    backups = _require_object(config, "backups", errors)
    _check_integer(backups, "max_workers", "backups.max_workers", errors, minimum=1)
    _check_number(
        backups,
        "mapping_cache_ttl_hours",
        "backups.mapping_cache_ttl_hours",
        errors,
        minimum=0,
    )
    for section_name in ("ima", "kuake", "obsidian"):
        section = _require_object(config, section_name, errors)
        _check_boolean(section, "enabled", f"{section_name}.enabled", errors)

    creators = config.get("creators", [])
    if not isinstance(creators, list):
        errors.append("creators 必须是数组")
        return errors
    keys: set[str] = set()
    for index, creator in enumerate(creators, start=1):
        prefix = f"creators[{index}]"
        if not isinstance(creator, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        key = str(creator.get("key") or "").strip()
        if not key:
            errors.append(f"{prefix}.key 不能为空")
        elif not CREATOR_KEY_PATTERN.fullmatch(key):
            errors.append(f"{prefix}.key 只能包含字母、数字、下划线和连字符")
        elif key in keys:
            errors.append(f"达人 key 重复：{key}")
        keys.add(key)
        _check_boolean(creator, "enabled", f"{prefix}.enabled", errors)
        for required_key in (
            "creator_url",
            "creator_name",
            "creator_dir_name",
            "works_table_id",
            "works_file",
        ):
            _check_required_text(
                creator,
                required_key,
                f"{prefix}.{required_key}",
                errors,
            )
        creator_profiles = creator.get("account_profiles", [])
        if not isinstance(creator_profiles, list) or any(
            not isinstance(item, str) for item in creator_profiles
        ):
            errors.append(f"{prefix}.account_profiles 必须是字符串数组")
    return errors


def save_pipeline_config(config: Any, project_dir: Path = PROJECT_DIR) -> SavedConfig:
    errors = validate_pipeline_config(config)
    if errors:
        raise WebDashboardError("\n".join(errors))
    assert isinstance(config, dict)
    project_dir = project_dir.resolve()
    path = project_dir / CONFIG_RELATIVE_PATH
    backup_path = project_dir / CONFIG_BACKUP_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(config, ensure_ascii=False, indent=2) + "\n"

    with _CONFIG_LOCK:
        actual_backup: Path | None = None
        if path.exists():
            # 备份只是保险，失败（如被其他进程/编辑器占用）不应阻止主配置保存，
            # 否则账号池扫码登录前的 PUT /api/config 会卡在备份步骤。
            try:
                shutil.copyfile(path, backup_path)
                actual_backup = backup_path
            except OSError:
                actual_backup = None
        temp_handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix="pipeline.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        )
        temp_path = Path(temp_handle.name)
        try:
            with temp_handle:
                temp_handle.write(serialized)
                temp_handle.flush()
                os.fsync(temp_handle.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
    return SavedConfig(path, actual_backup)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def snapshot_payload(snapshot: GUI.DashboardSnapshot) -> dict[str, Any]:
    running = snapshot.task.state.casefold() == "running"
    return {
        "task": {
            "state": snapshot.task.state,
            "last_run_time": _iso(snapshot.task.last_run_time),
            "next_run_time": _iso(snapshot.task.next_run_time),
            "last_result": snapshot.task.last_result,
        },
        "latest_run": {
            "run_id": snapshot.latest_run.run_id,
            "status": snapshot.latest_run.status,
            "started_at": _iso(snapshot.latest_run.started_at),
            "finished_at": _iso(snapshot.latest_run.finished_at),
            "wall_seconds": snapshot.latest_run.wall_seconds,
            "path": str(snapshot.latest_run.path) if snapshot.latest_run.path else None,
        },
        "creators": [
            {
                "key": item.key,
                "name": item.name,
                "works_count": item.works_count,
                "pending_count": item.pending_count,
                "status": item.status,
                "detail": item.detail,
                "latest_publish_time": _iso(item.latest_publish_time),
                "works_updated_at": _iso(item.works_updated_at),
            }
            for item in snapshot.creators
        ],
        "total_creators": snapshot.total_creators,
        "total_works": snapshot.total_works,
        "pending_works": snapshot.pending_works,
        "account_profiles_total": snapshot.account_profiles_total,
        "account_profiles_detected": snapshot.account_profiles_detected,
        "latest_log": str(snapshot.latest_log) if snapshot.latest_log else None,
        "log_tail": snapshot.log_tail if snapshot.latest_log else "当前没有正在执行的任务。",
        "activity_events": activity_events(snapshot.log_tail) if snapshot.latest_log else [],
        "refreshed_at": _iso(snapshot.refreshed_at),
    }


def public_error_summary(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "运行未完成"
    lowered = text.lower()
    if "account blocked" in lowered:
        return "抖音账号被风控（account blocked）"
    if "200005" in lowered or "请求超量" in text:
        return "IMA 当日请求额度已用完"
    if "timeout" in lowered or "超时" in text:
        return "网络请求超时"
    if any(marker in lowered for marker in ("dns", "connection refused", "network is unreachable")):
        return "网络连接异常"
    if "作品文件不存在" in text:
        return "本地作品数据文件不存在"
    if "filenotfounderror" in lowered or "no such file" in lowered:
        return "本地文件不存在"
    if "traceback" in lowered:
        return "运行组件异常，请查看技术日志"
    if re.search(r"(?:[A-Za-z]:[\\/]|\\\\)", text) or re.search(
        r"(?:^|\s)/(?:tmp|home|users|var|opt|srv|mnt)(?:/|\b)", lowered,
    ):
        return "本地文件或目录异常，请查看技术日志"
    prefix = re.split(r"\s+(?:\d{4}-\d{2}-\d{2}|Traceback)", text, maxsplit=1)[0]
    return GUI.summarize_error(prefix.rstrip("：:,， "), limit=90) or "运行未完成"


def activity_events(log_tail: str, limit: int = 40) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    pattern = re.compile(r"^\[([^\]]+)\]\s*(.+)$")
    for raw_line in str(log_tail or "").splitlines():
        match = pattern.match(raw_line.strip())
        if not match:
            continue
        timestamp, message = match.groups()
        if " stdout:" in message or " stderr:" in message:
            continue
        if message.startswith("流水线开始"):
            message = "流水线已启动"
        elif message.startswith("开始 ") and ":" in message:
            message = message.split(":", 1)[0]
        lowered_message = message.lower()
        if (
            any(word in message for word in ("失败", "异常", "封控", "耗尽"))
            or "partial_failure" in lowered_message
            or re.search(r"(?:状态|status)\s*[=:]?\s*failed\b", lowered_message)
        ):
            level = "failed"
        elif (
            any(word in message for word in ("完成", "成功"))
            or re.search(r"(?:状态|status)\s*[=:]?\s*success\b", lowered_message)
        ):
            level = "success"
        else:
            level = "info"
        events.append({"time": timestamp, "message": message[:180], "level": level})
    return events[-max(1, limit):]


def summarize_run(payload: dict[str, Any]) -> dict[str, Any]:
    creators = [item for item in payload.get("creators", []) if isinstance(item, dict)]
    successful = [item for item in creators if item.get("status") == "success"]
    failed = [item for item in creators if item.get("status") != "success"]
    selected_count = sum(int(item.get("selected_count") or 0) for item in creators)
    pending_count = sum(int(item.get("pending_count") or 0) for item in creators)
    work_results = [
        work
        for creator in creators
        for work in creator.get("works", [])
        if isinstance(work, dict)
    ]
    issues: list[dict[str, str]] = []
    if payload.get("error"):
        issues.append({"creator": "整体任务", "message": public_error_summary(payload["error"])})
    for creator in failed:
        error = (
            creator.get("collection_error")
            or creator.get("profile_sync_error")
            or creator.get("error")
            or "运行未完成"
        )
        issues.append({
            "creator": str(creator.get("creator_name") or creator.get("key") or "未命名达人"),
            "message": public_error_summary(error),
        })
    raw_status = str(payload.get("status") or "failed")
    status = raw_status if raw_status in {"success", "partial_failure"} else "failed"
    if status == "success":
        headline = f"检查 {len(creators)} 位达人，本次处理 {selected_count} 条作品，运行成功"
    elif creators:
        headline = f"{len(successful)} 位达人正常，{len(failed)} 位异常，本次处理 {selected_count} 条作品"
    else:
        headline = issues[0]["message"] if issues else "本次运行未完成"
    return {
        "run_id": str(payload.get("run_id") or ""),
        "status": status,
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "wall_seconds": float(payload.get("wall_seconds") or 0),
        "creator_count": len(creators),
        "successful_creators": len(successful),
        "failed_creators": len(failed),
        "selected_count": selected_count,
        "pending_count": pending_count,
        "successful_works": sum(1 for item in work_results if item.get("status") == "success"),
        "failed_works": sum(
            1 for item in work_results if item.get("status") in {"failed", "partial_failure"}
        ),
        "failed_calls": int((payload.get("timings") or {}).get("failed_calls") or 0)
        if isinstance(payload.get("timings"), dict) else 0,
        "headline": headline,
        "issues": issues[:5],
    }


def run_history_payload(
    project_dir: Path = PROJECT_DIR,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> dict[str, Any]:
    project_dir = project_dir.resolve()
    config = load_pipeline_config(project_dir).config
    state_dir = GUI.project_path(
        project_dir,
        config.get("state_dir"),
        project_dir / "runtime" / "pipeline",
    )
    try:
        files = sorted(
            (path for path in (state_dir / "runs").glob("*.json") if path.is_file()),
            key=lambda path: path.name,
            reverse=True,
        )[:MAX_HISTORY_FILES]
    except OSError:
        files = []
    runs: list[dict[str, Any]] = []
    for path in files:
        payload = GUI.read_json(path)
        if payload and payload.get("status") != "planned" and payload.get("dry_run") is not True:
            runs.append(summarize_run(payload))
    successful_runs = sum(1 for item in runs if item["status"] == "success")
    issue_runs = sum(1 for item in runs if item["status"] in {"failed", "partial_failure"})
    latest_success = next((item.get("finished_at") for item in runs if item["status"] == "success"), None)
    total_runs = len(runs)
    return {
        "stats": {
            "total_runs": total_runs,
            "successful_runs": successful_runs,
            "issue_runs": issue_runs,
            "success_rate": round(successful_runs * 100 / total_runs) if total_runs else 0,
            "latest_success_at": latest_success,
            "window_size": MAX_HISTORY_FILES,
        },
        "runs": runs[:max(1, min(int(limit), MAX_HISTORY_FILES))],
    }


# 作品处理阶段顺序与中文标签，对应流水线 STAGES。
# 各阶段状态持久化在 runtime/pipeline/<达人key>/<作品id>.json 的 stages 字典中。
WORK_STAGE_ORDER = [
    ("collected", "采集"),
    ("feishu_synced", "飞书同步"),
    ("transcribed", "转写"),
    ("corrected", "校正"),
    ("summarized", "总结"),
    ("feishu_written_back", "飞书回写"),
    ("ima_backed_up", "IMA 备份"),
    ("kuake_backed_up", "夸克备份"),
    ("obsidian_exported", "Obsidian 导出"),
    ("backup_statuses_written_back", "状态回写"),
]


def work_funnel_payload(project_dir: Path = PROJECT_DIR) -> dict[str, Any]:
    """聚合每位达人状态目录下的作品阶段状态，产出处理漏斗统计。

    只读 runtime/pipeline/<达人key>/*.json，不修改任何流水线产物。
    """
    project_dir = project_dir.resolve()
    config = load_pipeline_config(project_dir).config
    state_dir = GUI.project_path(
        project_dir,
        config.get("state_dir"),
        project_dir / "runtime" / "pipeline",
    )
    stage_keys = [key for key, _ in WORK_STAGE_ORDER]
    creators_out: list[dict[str, Any]] = []
    global_counts = {key: 0 for key in stage_keys}
    global_total = 0
    for creator in config.get("creators", []):
        if not isinstance(creator, dict) or not creator.get("enabled", True):
            continue
        key = str(
            creator.get("key")
            or creator.get("creator_dir_name")
            or creator.get("creator_name")
            or "?"
        )
        name = str(creator.get("creator_name") or creator.get("creator_dir_name") or key)
        dir_path = state_dir / key
        files = sorted(dir_path.glob("*.json")) if dir_path.is_dir() else []
        per_counts = {k: 0 for k in stage_keys}
        n = 0
        for fp in files:
            data = GUI.read_json(fp)
            if not isinstance(data, dict):
                continue
            stages = data.get("stages")
            n += 1
            if not isinstance(stages, dict):
                continue
            for sk in stage_keys:
                stage = stages.get(sk)
                if isinstance(stage, dict) and stage.get("status") == "success":
                    per_counts[sk] += 1
        for sk in stage_keys:
            global_counts[sk] += per_counts[sk]
        global_total += n
        if n:
            creators_out.append({"key": key, "name": name, "total": n, "stages": per_counts})
    stages_out = [
        {"key": k, "label": lbl, "success": global_counts[k]}
        for k, lbl in WORK_STAGE_ORDER
    ]
    return {
        "total_works_with_state": global_total,
        "stages": stages_out,
        "creators": creators_out,
    }


def _account_profile_keys(config: dict[str, Any]) -> list[str]:
    collection = config.get("collection")
    collection = collection if isinstance(collection, dict) else {}
    result: list[str] = []
    for value in collection.get("account_profiles", []):
        key = str(value or "").strip()
        if key and key not in result:
            result.append(key)
    return result


def _account_cookie_file(project_dir: Path, config: dict[str, Any], profile_key: str) -> Path:
    collection = config.get("collection")
    collection = collection if isinstance(collection, dict) else {}
    media_crawler_dir = GUI.project_path(
        project_dir,
        collection.get("media_crawler_dir"),
        project_dir / "MediaCrawler",
    )
    return (
        media_crawler_dir
        / "browser_data"
        / f"cdp_{profile_key}_dy_user_data_dir"
        / "Default"
        / "Network"
        / "Cookies"
    )


# Keep the dashboard and runtime collector on one login-validity threshold.
VALID_LOGIN_MIN_BYTES = PIPELINE.VALID_LOGIN_MIN_BYTES


def _account_login_state_file(project_dir: Path, profile_key: str) -> Path:
    return project_dir / "runtime" / "account-login" / profile_key / "login_state.json"


def account_pool_payload(project_dir: Path = PROJECT_DIR) -> dict[str, Any]:
    project_dir = project_dir.resolve()
    loaded = load_pipeline_config(project_dir)
    profiles: list[dict[str, Any]] = []
    with _ACCOUNT_LOGIN_LOCK:
        for key in _account_profile_keys(loaded.config):
            process = _ACCOUNT_LOGIN_PROCESSES.get(key)
            return_code = process.poll() if process is not None else None
            running = process is not None and return_code is None
            cookie_file = _account_cookie_file(project_dir, loaded.config, key)
            # Older Chromium/Playwright writes cookies to Default/Cookies instead of Default/Network/Cookies.
            legacy_cookie_file = cookie_file.parent.parent / "Cookies"
            ready = False
            updated_at = None
            for candidate in (cookie_file, legacy_cookie_file):
                try:
                    stat = candidate.stat()
                except OSError:
                    continue
                if stat.st_size > VALID_LOGIN_MIN_BYTES:
                    ready = True
                    updated_at = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
                    break
            # Explicit login-state marker written by the login script on success.
            marker_ready = False
            if not running:
                state_file = _account_login_state_file(project_dir, key)
                try:
                    marker = json.loads(state_file.read_text(encoding="utf-8"))
                    if marker.get("status") == "ready":
                        marker_ready = True
                except (OSError, ValueError):
                    marker_ready = False
            # The cookie file on disk is authoritative: a present-but-empty shell
            # (32768 bytes) means NOT logged in even if a stale marker exists.
            effective_ready = ready or (marker_ready and not cookie_file.exists())
            if running:
                status = "running"
            elif effective_ready:
                status = "ready"
            elif process is not None and return_code not in (None, 0):
                status = "failed"
            else:
                status = "missing"
            profiles.append(
                {
                    "key": key,
                    "status": status,
                    "updated_at": updated_at,
                    "profile_dir": str(cookie_file.parents[2]),
                }
            )
            if process is not None and return_code is not None:
                _ACCOUNT_LOGIN_PROCESSES.pop(key, None)
    return {"profiles": profiles, "configured_count": len(profiles)}


def _read_local_env_keys(path: Path) -> set[str]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return set()
    keys: set[str] = set()
    for line in lines:
        match = re.match(r"\s*(?:\$env:)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        value = match.group(2).strip().strip('"\'') if match else ""
        if match and value and not line.lstrip().startswith("#"):
            keys.add(match.group(1))
    return keys


def _available_secret_keys(project_dir: Path, *json_names: str) -> set[str]:
    keys = {key for key, value in os.environ.items() if str(value or "").strip()}
    keys.update(_read_local_env_keys(project_dir / "local" / ".env"))
    for name in json_names:
        try:
            payload = json.loads((project_dir / "local" / name).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            keys.update(key for key, value in payload.items() if str(value or "").strip())
    return keys


def _configured_path(project_dir: Path, value: Any, fallback: str) -> Path:
    return GUI.project_path(project_dir, value, project_dir / fallback)


def _permission_check(
    check_id: str,
    label: str,
    ready: bool,
    success_detail: str,
    failure_detail: str,
    *,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "status": "ready" if ready else "failed",
        "detail": success_detail if ready else failure_detail,
    }
    if action:
        result["action"] = action
    return result


def permission_checks_payload(project_dir: Path = PROJECT_DIR) -> dict[str, Any]:
    """Return fast, read-only startup checks for every enabled account dependency.

    These checks deliberately describe local credential/session readiness. They do
    not write remote data and do not claim an online permission probe succeeded.
    """

    project_dir = project_dir.resolve()
    config = load_pipeline_config(project_dir).config
    checks: list[dict[str, Any]] = []

    account_pool = account_pool_payload(project_dir)
    profiles = account_pool.get("profiles") if isinstance(account_pool, dict) else []
    profiles = profiles if isinstance(profiles, list) else []
    if profiles:
        for profile in profiles:
            key = str(profile.get("key") or "")
            status = str(profile.get("status") or "missing")
            check = _permission_check(
                f"douyin:{key}",
                f"抖音采集账号 · {key}",
                status == "ready",
                "已检测到本地登录态；正式采集时仍会校验是否失效或风控。",
                "未检测到有效本地登录态，请重新扫码登录。",
                action={"type": "douyin_login", "profile_key": key, "label": "重新登录"},
            )
            if status == "running":
                check["status"] = "running"
                check["detail"] = "登录窗口已打开，正在等待扫码完成。"
            checks.append(check)
    else:
        checks.append(_permission_check(
            "douyin",
            "抖音采集账号",
            False,
            "账号池已配置。",
            "尚未配置任何抖音账号槽位。",
            action={"type": "config", "category": "accounts", "label": "配置账号"},
        ))

    feishu = config.get("feishu") if isinstance(config.get("feishu"), dict) else {}
    feishu_keys = _available_secret_keys(project_dir)
    token_env = str(feishu.get("base_token_env") or "FEISHU_BASE_TOKEN")
    ids_file = _configured_path(project_dir, feishu.get("ids_file"), "local/feishu-ids.md")
    try:
        ids_text = ids_file.read_text(encoding="utf-8-sig")
    except OSError:
        ids_text = ""
    local_token_match = re.search(r"(?im)^\s*[-*]?\s*base[_ ]token\s*[:=]\s*(\S+)", ids_text)
    local_token = local_token_match.group(1).strip() if local_token_match else ""
    has_base_token = token_env in feishu_keys or bool(
        local_token and not local_token.startswith("REPLACE_WITH_")
    )
    lark_cli = _configured_path(project_dir, feishu.get("lark_cli"), "tools/lark-cli/lark-cli.exe")
    profile = str(feishu.get("profile") or "").strip()
    creator_table_id = str(feishu.get("creator_table_id") or "").strip()
    feishu_ready = bool(
        lark_cli.is_file()
        and has_base_token
        and profile
        and not profile.startswith("REPLACE_WITH_")
        and creator_table_id
        and not creator_table_id.startswith("REPLACE_WITH_")
    )
    checks.append(_permission_check(
        "feishu",
        "飞书数据账号",
        feishu_ready,
        "CLI、Base 标识和身份配置已就绪；此处未执行远端写入。",
        "飞书 CLI、Base 标识或身份配置不完整。",
        action={"type": "config", "category": "feishu", "label": "检查配置"},
    ))

    asr = config.get("asr") if isinstance(config.get("asr"), dict) else {}
    provider = str(asr.get("provider") or "volcengine").strip().casefold()
    if provider == "volcengine":
        asr_keys = _available_secret_keys(project_dir, "volcengine.env.json")
        asr_ready = "VOLC_ASR_API_KEY" in asr_keys or {
            "VOLC_ASR_APP_ID", "VOLC_ASR_ACCESS_TOKEN", "VOLC_ASR_CLUSTER",
        }.issubset(asr_keys)
        asr_label = "火山引擎 ASR 账号"
        asr_id = "volcengine_asr"
    else:
        asr_keys = _available_secret_keys(project_dir, "bailian.env.json")
        asr_ready = bool({"DASHSCOPE_API_KEY", "BAILIAN_API_KEY"} & asr_keys)
        asr_label = f"{provider or 'ASR'} 转写账号"
        asr_id = "asr"
    checks.append(_permission_check(
        asr_id,
        asr_label,
        asr_ready,
        "本地转写凭据已配置；实际额度和接口权限将在调用时确认。",
        "缺少当前转写服务所需的本地凭据。",
        action={"type": "config", "category": "asr", "label": "更新凭据"},
    ))

    summary = config.get("summary") if isinstance(config.get("summary"), dict) else {}
    if bool(summary.get("enabled", True)):
        summary_key = str(summary.get("api_key_env") or "OPENAI_API_KEY").strip()
        summary_keys = _available_secret_keys(project_dir)
        summary_ready = bool(str(summary.get("model") or "").strip() and summary_key in summary_keys)
        checks.append(_permission_check(
            "summary",
            "内容总结模型账号",
            summary_ready,
            "模型和本地 API Key 已配置；实际额度将在调用时确认。",
            "缺少总结模型名称或对应的本地 API Key。",
            action={"type": "config", "category": "summary", "label": "更新凭据"},
        ))

    ima = config.get("ima") if isinstance(config.get("ima"), dict) else {}
    if bool(ima.get("enabled", True)):
        ima_keys = _available_secret_keys(project_dir, "ima.env.json")
        ima_ready = {"IMA_OPENAPI_CLIENTID", "IMA_OPENAPI_APIKEY"}.issubset(ima_keys) or {
            "client_id", "api_key",
        }.issubset(ima_keys)
        checks.append(_permission_check(
            "ima", "IMA 知识库账号", ima_ready,
            "本地 OpenAPI 凭据已配置；在线权限和当日额度将在调用时确认。",
            "缺少 IMA Client ID 或 API Key。",
            action={"type": "config", "category": "backups", "label": "更新凭据"},
        ))

    kuake = config.get("kuake") if isinstance(config.get("kuake"), dict) else {}
    if bool(kuake.get("enabled", True)):
        kuake_env_path = _configured_path(project_dir, kuake.get("local_env"), "local/kuake.env.json")
        kuake_keys = _available_secret_keys(project_dir, kuake_env_path.name)
        has_kuake_credential = bool({"KUAKE_COOKIE", "kuake_cookie"} & kuake_keys) or {
            "KUAKE_PUS", "KUAKE_PUUS",
        }.issubset(kuake_keys) or {"kuake_pus", "kuake_puus"}.issubset(kuake_keys)
        kuake_exe = _configured_path(project_dir, kuake.get("kuake_exe"), "tools/kuake-cli/kuake.exe")
        checks.append(_permission_check(
            "kuake", "夸克网盘账号", has_kuake_credential and kuake_exe.is_file(),
            "夸克 CLI 和本地 Cookie 凭据已配置；失效状态将在调用时确认。",
            "缺少夸克 CLI 或 Cookie 凭据。",
            action={"type": "config", "category": "backups", "label": "更新凭据"},
        ))

    passed = sum(1 for check in checks if check["status"] == "ready")
    return {
        "checks": checks,
        "total": len(checks),
        "checked": len(checks),
        "passed": passed,
        "all_passed": passed == len(checks),
        "checked_at": datetime.now().astimezone().isoformat(),
        "scope_note": "绿色仅表示本地登录态或凭据准备检查通过；远端权限、额度与风控仍由正式调用确认。扫码型账号可直接重登，密钥型账号请更新本地凭据。",
    }


def build_account_login_command(
    project_dir: Path,
    config: dict[str, Any],
    profile_key: str,
) -> list[str]:
    profile_key = str(profile_key or "").strip()
    if not CREATOR_KEY_PATTERN.fullmatch(profile_key):
        raise WebDashboardError("账号槽位只能包含字母、数字、下划线和连字符")
    keys = _account_profile_keys(config)
    if profile_key not in keys:
        raise WebDashboardError(f"账号槽位尚未保存到配置：{profile_key}")
    collection = config.get("collection")
    collection = collection if isinstance(collection, dict) else {}
    try:
        port_start = int(collection.get("cdp_port_start", 9222))
        port_stride = int(collection.get("cdp_port_stride", 10))
    except (TypeError, ValueError) as exc:
        raise WebDashboardError("账号池 CDP 端口配置无效") from exc
    cdp_port = port_start + keys.index(profile_key) * port_stride
    if not 1 <= cdp_port <= 65535:
        raise WebDashboardError(f"账号槽位 {profile_key} 的 CDP 端口超出范围")
    login_dir = project_dir / "runtime" / "account-login" / profile_key
    command = [
        str(config.get("python") or sys.executable),
        str(project_dir / "scripts" / "collect_douyin_creator_with_mediacrawler.py"),
        "--creator-url",
        "https://www.douyin.com/user/account-login-probe",
        "--media-output-dir",
        str(login_dir / "output"),
        "--output-file",
        str(login_dir / "probe.json"),
        "--max-count",
        "1",
        "--expect-min-count",
        "0",
        "--login-type",
        "qrcode",
        "--browser-profile-key",
        profile_key,
        "--cdp-port",
        str(cdp_port),
        "--login-only",
    ]
    for option, value in (
        ("--media-crawler-dir", collection.get("media_crawler_dir")),
        ("--media-crawler-python", collection.get("media_crawler_python")),
    ):
        if str(value or "").strip():
            command.extend([option, str(value).strip()])
    return command


def _record_replica_refresh(
    project_dir: Path,
    profile_key: str,
    status: str,
    detail: dict[str, Any] | str,
) -> None:
    state_file = _account_login_state_file(project_dir, profile_key)
    try:
        current = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {"status": "ready", "profile_key": profile_key}
    current["replica_refresh"] = {
        "status": status,
        "at": datetime.now().astimezone().isoformat(),
        "detail": detail,
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, state_file)


def refresh_replicas_after_account_login(
    profile_key: str,
    process: subprocess.Popen[Any],
    project_dir: Path = PROJECT_DIR,
) -> None:
    """Refresh creator replicas immediately after the current main login exits."""

    return_code = process.wait()
    if return_code:
        return
    try:
        config = load_pipeline_config(project_dir).config
        ordered_profiles = _account_profile_keys(config)
        if not ordered_profiles or ordered_profiles[0] != profile_key:
            return
        creators = [
            creator
            for creator in config.get("creators", [])
            if isinstance(creator, dict) and creator.get("enabled", True)
        ]
        log_path = (
            project_dir / "runtime" / "account-login"
            / profile_key / "replica-refresh.log"
        )
        result = PIPELINE.refresh_primary_profile_replicas(
            config, creators, PIPELINE.Logger(log_path),
        )
        _record_replica_refresh(
            project_dir, profile_key, "success", result,
        )
    except Exception as exc:  # noqa: BLE001 - watcher must not kill the web server
        try:
            _record_replica_refresh(
                project_dir, profile_key, "failed", str(exc),
            )
        except OSError:
            pass


def start_account_login(profile_key: str, project_dir: Path = PROJECT_DIR) -> dict[str, Any]:
    project_dir = project_dir.resolve()
    try:
        task = GUI.query_scheduled_task(GUI.TASK_NAME)
    except GUI.DashboardError:
        task = None
    if task is not None and task.state.casefold() == "running":
        raise WebDashboardError("每日流水线正在运行，请等待采集结束后再扫码登录")
    config = load_pipeline_config(project_dir).config
    command = build_account_login_command(project_dir, config, profile_key)
    with _ACCOUNT_LOGIN_LOCK:
        current = _ACCOUNT_LOGIN_PROCESSES.get(profile_key)
        if current is not None and current.poll() is None:
            raise WebDashboardError(f"账号槽位 {profile_key} 正在等待扫码")
        options: dict[str, Any] = {
            "cwd": project_dir,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(command, **options)
        except OSError as exc:
            raise WebDashboardError(f"无法启动账号槽位 {profile_key} 的扫码登录：{exc}") from exc
        _ACCOUNT_LOGIN_PROCESSES[profile_key] = process
        threading.Thread(
            target=refresh_replicas_after_account_login,
            args=(profile_key, process, project_dir),
            name=f"account-profile-refresh-{profile_key}",
            daemon=True,
        ).start()
    return {
        "message": f"已打开账号槽位 {profile_key} 的抖音扫码窗口",
        "profile_key": profile_key,
    }


def active_account_login_keys() -> list[str]:
    with _ACCOUNT_LOGIN_LOCK:
        return [
            key
            for key, process in _ACCOUNT_LOGIN_PROCESSES.items()
            if process.poll() is None
        ]


def make_handler(
    project_dir: Path = PROJECT_DIR,
    *,
    snapshot_builder: Callable[..., GUI.DashboardSnapshot] = GUI.build_dashboard_snapshot,
    task_starter: Callable[[str], str] = GUI.start_scheduled_task,
    account_status_provider: Callable[[Path], dict[str, Any]] = account_pool_payload,
    account_login_starter: Callable[[str, Path], dict[str, Any]] = start_account_login,
    permission_provider: Callable[[Path], dict[str, Any]] = permission_checks_payload,
    history_provider: Callable[[Path], dict[str, Any]] = run_history_payload,
    funnel_provider: Callable[[Path], dict[str, Any]] = work_funnel_payload,
) -> type[BaseHTTPRequestHandler]:
    project_dir = project_dir.resolve()
    web_dir = project_dir / WEB_DIR_RELATIVE_PATH

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "DouyinCreatorMonitor/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_headers(self, status: int, content_type: str, length: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'",
            )
            self.end_headers()

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_headers(status, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _error(self, status: int, message: str, details: list[str] | None = None) -> None:
            payload: dict[str, Any] = {"error": message}
            if details:
                payload["details"] = details
            self._json(status, payload)

        def _read_json(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise WebDashboardError("Content-Length 无效") from exc
            if length > MAX_REQUEST_BYTES:
                raise WebDashboardError("请求内容过大")
            try:
                return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WebDashboardError("请求不是有效的 JSON") from exc

        def _serve_static(self, request_path: str) -> None:
            relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
            pure = PurePosixPath(relative)
            allowed = {"index.html", "app.js", "styles.css", "favicon.svg"}
            if pure.as_posix() not in allowed:
                self._error(HTTPStatus.NOT_FOUND, "页面不存在")
                return
            path = web_dir / pure.as_posix()
            try:
                body = path.read_bytes()
            except OSError:
                self._error(HTTPStatus.NOT_FOUND, "页面资源不存在")
                return
            content_types = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
            }
            self._send_headers(HTTPStatus.OK, content_types.get(path.suffix, "application/octet-stream"), len(body))
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            try:
                if path == "/api/config":
                    loaded = load_pipeline_config(project_dir)
                    self._json(
                        HTTPStatus.OK,
                        {
                            "config": loaded.config,
                            "exists": loaded.exists,
                            "path": str(loaded.path),
                        },
                    )
                elif path == "/api/status":
                    self._json(HTTPStatus.OK, snapshot_payload(snapshot_builder(project_dir)))
                elif path == "/api/accounts":
                    self._json(HTTPStatus.OK, account_status_provider(project_dir))
                elif path == "/api/permissions":
                    self._json(HTTPStatus.OK, permission_provider(project_dir))
                elif path == "/api/history":
                    self._json(HTTPStatus.OK, history_provider(project_dir))
                elif path == "/api/funnel":
                    self._json(HTTPStatus.OK, funnel_provider(project_dir))
                elif path.startswith("/api/"):
                    self._error(HTTPStatus.NOT_FOUND, "接口不存在")
                else:
                    self._serve_static(path)
            except Exception as exc:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))

        def do_PUT(self) -> None:  # noqa: N802
            if urlsplit(self.path).path != "/api/config":
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            try:
                config = self._read_json()
                errors = validate_pipeline_config(config)
                if errors:
                    self._error(HTTPStatus.BAD_REQUEST, "配置校验失败", errors)
                    return
                result = save_pipeline_config(config, project_dir)
                self._json(
                    HTTPStatus.OK,
                    {
                        "message": "配置已保存",
                        "config": config,
                        "path": str(result.path),
                        "backup_path": str(result.backup_path) if result.backup_path else None,
                    },
                )
            except WebDashboardError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"保存失败：{exc}")

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/api/accounts/login":
                try:
                    payload = self._read_json()
                    profile_key = str(payload.get("profile_key") or "") if isinstance(payload, dict) else ""
                    self._json(HTTPStatus.ACCEPTED, account_login_starter(profile_key, project_dir))
                except WebDashboardError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc))
                except Exception as exc:
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"账号登录启动失败：{exc}")
                return
            if path != "/api/run":
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            try:
                self._read_json()
                active_logins = active_account_login_keys()
                if active_logins:
                    raise WebDashboardError(
                        f"账号槽位 {', '.join(active_logins)} 正在等待扫码，请先完成或关闭登录窗口"
                    )
                message = task_starter(GUI.TASK_NAME)
                self._json(HTTPStatus.ACCEPTED, {"message": message})
            except WebDashboardError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except Exception as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"启动失败：{exc}")

    return DashboardHandler


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抖音达人监控本地 Web 控制台")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser.parse_args(argv)


def open_dashboard_without_blank(
    url: str,
    *,
    opener: Callable[[str], bool] = webbrowser.open,
) -> bool:
    """Open the dashboard while removing only a blank created by Chrome handoff."""

    user_chrome_pids = find_unrelated_user_chrome_pids()
    existing_blank_handles = set(visible_blank_chrome_windows(user_chrome_pids))
    try:
        return bool(opener(url))
    finally:
        close_new_blank_chrome_windows(existing_blank_handles, user_chrome_pids)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("端口必须在 1 到 65535 之间")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(PROJECT_DIR))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"抖音达人监控 Web 控制台：{url}", flush=True)
    print("按 Ctrl+C 停止本地服务。", flush=True)
    if not args.no_browser:
        threading.Timer(0.4, open_dashboard_without_blank, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 Web 控制台…", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
