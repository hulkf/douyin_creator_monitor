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
    for key in ("incremental_enabled", "clean_media_output", "headless"):
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
        if path.exists():
            shutil.copyfile(path, backup_path)
            actual_backup: Path | None = backup_path
        else:
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
        "latest_log": str(snapshot.latest_log) if running and snapshot.latest_log else None,
        "log_tail": snapshot.log_tail if running else "当前没有正在执行的任务。",
        "activity_events": activity_events(snapshot.log_tail) if running else [],
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
            try:
                stat = cookie_file.stat()
                ready = stat.st_size >= GUI.VALID_LOGIN_MIN_BYTES
                updated_at = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
            except OSError:
                ready = False
                updated_at = None
            if running:
                status = "running"
            elif ready:
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
    history_provider: Callable[[Path], dict[str, Any]] = run_history_payload,
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
                elif path == "/api/history":
                    self._json(HTTPStatus.OK, history_provider(project_dir))
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("端口必须在 1 到 65535 之间")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(PROJECT_DIR))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"抖音达人监控 Web 控制台：{url}", flush=True)
    print("按 Ctrl+C 停止本地服务。", flush=True)
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止 Web 控制台…", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
