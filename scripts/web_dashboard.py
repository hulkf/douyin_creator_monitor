#!/usr/bin/env python3
"""Local web control panel for the Douyin creator monitor."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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
CREATOR_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_CONFIG_LOCK = threading.Lock()


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
    for key in ("incremental_enabled", "clean_media_output"):
        _check_boolean(collection, key, f"collection.{key}", errors)
    profiles = collection.get("account_profiles", [])
    if not isinstance(profiles, list) or any(not isinstance(item, str) for item in profiles):
        errors.append("collection.account_profiles 必须是字符串数组")

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
        "log_tail": snapshot.log_tail,
        "refreshed_at": _iso(snapshot.refreshed_at),
    }


def make_handler(
    project_dir: Path = PROJECT_DIR,
    *,
    snapshot_builder: Callable[..., GUI.DashboardSnapshot] = GUI.build_dashboard_snapshot,
    task_starter: Callable[[str], str] = GUI.start_scheduled_task,
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
            if urlsplit(self.path).path != "/api/run":
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            try:
                self._read_json()
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
