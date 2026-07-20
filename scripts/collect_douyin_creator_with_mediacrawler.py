"""Collect and normalize a Douyin creator's works through MediaCrawler.

This script replaces the fragile browser/Crawlio capture layer with a stable
adapter around the external MediaCrawler project, while preserving this
project's downstream contract:

- output a normalized JSON payload with a top-level ``works`` list;
- keep the same field names consumed by ``sync_douyin_works_to_feishu.py``;
- require publish time and engagement metrics before the result is considered
  valid for Feishu sync.

The script does not vendor MediaCrawler. Point ``--media-crawler-dir`` or the
``MEDIACRAWLER_DIR`` environment variable at a local MediaCrawler checkout.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable


PROJECT_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = PROJECT_DIR / "runtime"
DEFAULT_OUTPUT_FILE = PROJECT_DIR / "runtime" / "zhiliao-works-from-mediacrawler.json"
DEFAULT_MEDIA_OUTPUT_DIR = PROJECT_DIR / "runtime" / "mediacrawler-output"
BEIJING_TZ = timezone(timedelta(hours=8))
COLLECTION_STATE_VERSION = 1
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "aweme_id": ("aweme_id", "awemeId", "note_id", "id", "作品ID", "抖音作品ID"),
    "desc": ("desc", "title", "display_title", "content", "原始文案", "作品标题"),
    "create_time": ("create_time", "publish_time", "time", "发布时间"),
    "digg_count": ("digg_count", "liked_count", "like_count", "likes", "点赞数"),
    "comment_count": ("comment_count", "comments_count", "comments", "评论数"),
    "collect_count": ("collect_count", "collected_count", "favorite_count", "收藏数"),
    "share_count": ("share_count", "shares_count", "shares", "分享数"),
    "cover_url": ("cover_url", "cover", "video_cover", "封面图URL"),
    "url": ("url", "aweme_url", "note_url", "作品链接"),
    "is_top": ("is_top", "isTop", "是否置顶"),
}


def now_beijing() -> str:
    return datetime.now(tz=timezone.utc).astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def creator_id_from_url(value: str) -> str:
    """Extract the Douyin sec_user_id/user token from a profile URL or raw id."""

    value = value.strip()
    match = re.search(r"/user/([^/?#]+)", value)
    if match:
        return match.group(1)
    return value.rstrip("/")


def safe_browser_profile_key(value: str) -> str:
    """Return a stable profile directory component without path traversal characters."""

    normalized = "".join(char if char.isalnum() or char in "-_." else "_" for char in value.strip())
    normalized = normalized.strip(" ._")
    return normalized if normalized not in {"", ".", ".."} else "creator"


def resolve_media_crawler_dir(value: str | None) -> Path:
    media_dir = Path(value or os.environ.get("MEDIACRAWLER_DIR", "")).expanduser()
    if not str(media_dir) or not (media_dir / "main.py").exists():
        raise SystemExit("Missing MediaCrawler checkout. Pass --media-crawler-dir or set MEDIACRAWLER_DIR.")
    return media_dir.resolve()


def resolve_media_crawler_python(media_dir: Path, value: str | None) -> str:
    if value:
        return str(Path(value).expanduser())
    env_value = os.environ.get("MEDIACRAWLER_PYTHON")
    if env_value:
        return str(Path(env_value).expanduser())
    local_venv_python = media_dir / ".venv" / "Scripts" / "python.exe"
    if local_venv_python.exists():
        return str(local_venv_python)
    return sys.executable


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    multiplier = 1
    if text.endswith("万"):
        multiplier = 10_000
        text = text[:-1]
    elif text.endswith("w") or text.endswith("W"):
        multiplier = 10_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def as_unix_seconds(value: Any) -> int | None:
    parsed = as_int(value)
    if parsed is not None:
        if parsed > 10_000_000_000:
            return int(parsed / 1000)
        return parsed
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            parsed_dt = datetime.strptime(text[:19] if "%H" in fmt else text[:10], fmt)
            return int(parsed_dt.replace(tzinfo=BEIJING_TZ).timestamp())
        except ValueError:
            continue
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def first_present(source: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None and value != "":
            return value
    return None


def aliased(source: dict[str, Any], field: str) -> Any:
    return first_present(source, FIELD_ALIASES[field])


def nested(source: dict[str, Any], *keys: str) -> Any:
    current: Any = source
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_url(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, list):
        for item in value:
            found = first_url(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("url_list", "url", "uri", "cover", "origin_cover"):
            found = first_url(value.get(key))
            if found:
                return found
    return None


def normalize_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize either MediaCrawler's stored shape or Douyin's raw API shape."""

    statistics = record.get("statistics") if isinstance(record.get("statistics"), dict) else {}
    aweme_id = aliased(record, "aweme_id")
    if aweme_id is None:
        return None

    desc = aliased(record, "desc") or ""
    create_time = as_unix_seconds(aliased(record, "create_time"))

    digg_count = as_int(
        aliased(record, "digg_count")
        if aliased(record, "digg_count") is not None
        else statistics.get("digg_count")
    )
    comment_count = as_int(
        aliased(record, "comment_count")
        if aliased(record, "comment_count") is not None
        else statistics.get("comment_count")
    )
    collect_count = as_int(
        aliased(record, "collect_count")
        if aliased(record, "collect_count") is not None
        else statistics.get("collect_count")
    )
    share_count = as_int(
        aliased(record, "share_count")
        if aliased(record, "share_count") is not None
        else statistics.get("share_count")
    )

    cover_url = first_url(
        aliased(record, "cover_url")
        or nested(record, "video", "cover")
        or nested(record, "video", "origin_cover")
    )
    url = aliased(record, "url")
    if not url:
        url = f"https://www.douyin.com/video/{aweme_id}"

    normalized: dict[str, Any] = {
        "aweme_id": str(aweme_id),
        "desc": str(desc),
        "create_time": create_time,
        "digg_count": digg_count,
        "comment_count": comment_count,
        "collect_count": collect_count,
        "share_count": share_count,
        "cover_url": cover_url,
        "url": url,
        "is_top": bool(aliased(record, "is_top")),
        "source": "mediacrawler",
        "raw": record,
    }
    return {key: value for key, value in normalized.items() if value is not None}


def read_json_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return []
    payload = json.loads(text)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("aweme_list", "works", "data", "items", "notes"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            records.append(item)
    return records


def read_csv_records(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def candidate_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for pattern in ("*.json", "*.jsonl", "*.csv"):
        files.extend(root.rglob(pattern))
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def load_records_from_output(root: Path) -> tuple[list[dict[str, Any]], list[Path]]:
    all_records: list[dict[str, Any]] = []
    used_files: list[Path] = []
    for path in candidate_files(root):
        name = path.name.lower()
        # MediaCrawler's official Douyin JSONL writer stores creator works as
        # ``douyin/jsonl/creator_contents_YYYY-MM-DD.jsonl``.  The file name
        # does not contain ``douyin`` or ``aweme``, so keep ``contents`` here
        # and rely on normalize_record() to reject non-Douyin rows.
        if not any(token in name for token in ("douyin", "dy", "aweme", "note", "works", "contents")):
            continue
        try:
            if path.suffix.lower() == ".jsonl":
                records = read_jsonl_records(path)
            elif path.suffix.lower() == ".csv":
                records = read_csv_records(path)
            else:
                records = read_json_records(path)
        except Exception:
            continue
        normalized = [item for item in (normalize_record(record) for record in records) if item]
        if normalized:
            all_records.extend(normalized)
            used_files.append(path)
    return all_records, used_files


def dedupe_works(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        work_id = str(record["aweme_id"])
        previous = by_id.get(work_id)
        if not previous:
            by_id[work_id] = record
            continue
        previous_score = sum(previous.get(key) is not None for key in CORE_KEYS)
        current_score = sum(record.get(key) is not None for key in CORE_KEYS)
        if current_score >= previous_score:
            by_id[work_id] = record
    return sorted(by_id.values(), key=lambda item: int(item.get("create_time") or 0), reverse=True)


CORE_KEYS = ("aweme_id", "create_time", "digg_count", "comment_count", "collect_count", "share_count")


def validate_core_fields(works: list[dict[str, Any]]) -> None:
    missing = [work.get("aweme_id") for work in works if any(work.get(key) is None or work.get(key) == "" for key in CORE_KEYS)]
    if missing:
        raise SystemExit(f"Refusing to write normalized output; missing core fields for works: {missing[:10]}")


def safe_source_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_DIR.resolve()))
    except ValueError:
        return path.name


def read_existing_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read existing normalized works file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Existing normalized works file must contain a JSON object: {path}")
    return payload


def works_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    works = payload.get("works", [])
    if not isinstance(works, list):
        return []
    return [item for item in works if isinstance(item, dict) and item.get("aweme_id")]


def merge_works(existing: Iterable[dict[str, Any]], current: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge refreshed works without erasing useful fields missing from this run."""

    by_id: dict[str, dict[str, Any]] = {
        str(item["aweme_id"]): dict(item) for item in existing if item.get("aweme_id")
    }
    for item in current:
        work_id = str(item.get("aweme_id") or "")
        if not work_id:
            continue
        merged = dict(by_id.get(work_id, {}))
        for key, value in item.items():
            if value is not None and value != "":
                merged[key] = value
        merged["aweme_id"] = work_id
        by_id[work_id] = merged
    return sorted(by_id.values(), key=lambda item: int(item.get("create_time") or 0), reverse=True)


def select_incremental_page(
    items: Iterable[dict[str, Any]], known_ids: set[str], checked_count: int,
    probe_count: int, boundary_seen: bool = False,
) -> tuple[list[dict[str, Any]], int, bool, str | None]:
    """Choose the newest page prefix and stop after probing enough rows past a known boundary."""

    selected: list[dict[str, Any]] = []
    boundary_id: str | None = None
    ordered = sorted(items, key=lambda item: int(item.get("create_time") or 0), reverse=True)
    for item in ordered:
        work_id = str(item.get("aweme_id") or "")
        if not work_id:
            continue
        selected.append(item)
        checked_count += 1
        if work_id in known_ids:
            boundary_seen = True
            boundary_id = boundary_id or work_id
        if boundary_seen and checked_count >= max(1, probe_count):
            return selected, checked_count, True, boundary_id
    return selected, checked_count, False, boundary_id


def read_collection_state(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def determine_collection_mode(
    state: dict[str, Any], creator_id: str, existing_works: list[dict[str, Any]], force_full: bool,
) -> str:
    if force_full:
        return "full"
    if (
        existing_works
        and state.get("full_history_collected") is True
        and str(state.get("creator_id") or "") == creator_id
    ):
        return "incremental"
    return "full"


def ordered_ids(ids: Iterable[str], works: list[dict[str, Any]]) -> list[str]:
    wanted = {str(item) for item in ids if str(item)}
    return [str(work["aweme_id"]) for work in works if str(work.get("aweme_id")) in wanted]


def write_collection_state(
    path: Path | None, *, creator_id: str, full_history_collected: bool,
    mode: str, works: list[dict[str, Any]], pending_ids: list[str], report: dict[str, Any],
    full_history_collected_at: str | None = None,
) -> dict[str, Any]:
    state = read_collection_state(path)
    if state.get("creator_id") and str(state.get("creator_id")) != creator_id:
        state = {}
    captured_at = now_beijing()
    state.update(
        {
            "version": COLLECTION_STATE_VERSION,
            "creator_id": creator_id,
            "full_history_collected": bool(full_history_collected),
            "last_successful_collection_at": captured_at,
            "last_collection_mode": mode,
            "known_work_count": len(works),
            "latest_aweme_ids": [str(item["aweme_id"]) for item in works[:20]],
            "pending_aweme_ids": ordered_ids(pending_ids, works),
            "last_report": report,
        }
    )
    if full_history_collected:
        state["full_history_collected_at"] = (
            full_history_collected_at
            or str(state.get("full_history_collected_at") or "")
            or captured_at
        )
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def build_payload(
    works: list[dict[str, Any]], creator_url: str, used_files: list[Path], *,
    mode: str, full_history_collected: bool, new_ids: list[str],
    pending_ids: list[str], report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source": "MediaCrawler",
        "creator_url": creator_url,
        "creator_id": creator_id_from_url(creator_url),
        "captured_at": now_beijing(),
        "count": len(works),
        "works": works,
        "media_crawler_files": [safe_source_path(path) for path in used_files],
        "collection_mode": mode,
        "full_history_collected": bool(full_history_collected),
        "new_count": len(new_ids),
        "new_aweme_ids": new_ids,
        "pending_count": len(pending_ids),
        "pending_aweme_ids": pending_ids,
        "collection_report": report,
    }


def write_payload(payload: dict[str, Any], output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_mediacrawler(
    args: argparse.Namespace, *, mode: str, known_ids: set[str],
) -> tuple[Path, dict[str, Any]]:
    media_dir = resolve_media_crawler_dir(args.media_crawler_dir)

    output_root = Path(args.media_output_dir).expanduser().resolve()
    if args.clean_media_output and output_root.exists():
        resolved_runtime = RUNTIME_DIR.resolve()
        if output_root == resolved_runtime or resolved_runtime not in output_root.parents:
            raise SystemExit(f"Refusing to clean output directory outside project runtime/: {output_root}")
        shutil.rmtree(output_root)
    run_id = datetime.now(BEIJING_TZ).strftime("%Y%m%d-%H%M%S-%f")
    output_dir = output_root / "runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    report_file = output_dir / "_collection_report.json"

    bootstrap = output_dir / "_run_mediacrawler_douyin_creator.py"
    creator_id = creator_id_from_url(args.creator_url)
    browser_profile_key = safe_browser_profile_key(
        str(getattr(args, "browser_profile_key", "") or creator_id)
    )
    cdp_port = int(getattr(args, "cdp_port", 9222))
    if not 1 <= cdp_port <= 65535:
        raise SystemExit("--cdp-port must be between 1 and 65535")
    bootstrap.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import asyncio, json, runpy, sys",
                f"media_dir = Path({str(media_dir)!r})",
                "sys.path.insert(0, str(media_dir))",
                "import config",
                "config.ENABLE_CDP_MODE = True",
                "config.CDP_CONNECT_EXISTING = False",
                f"config.CDP_DEBUG_PORT = {cdp_port}",
                f"config.USER_DATA_DIR = {f'{browser_profile_key}_%s_user_data_dir'!r}",
                f"print('[collector] isolated browser profile={browser_profile_key} cdp_port_start={cdp_port}')",
                "from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError",
                "_original_page_goto = Page.goto",
                "async def _page_goto_with_retry(self, url, **kwargs):",
                "    kwargs.setdefault('wait_until', 'domcontentloaded')",
                "    kwargs.setdefault('timeout', 45000)",
                "    for attempt in range(3):",
                "        try:",
                "            return await _original_page_goto(self, url, **kwargs)",
                "        except PlaywrightTimeoutError:",
                "            if attempt == 2:",
                "                raise",
                "            await asyncio.sleep(2 ** attempt)",
                "Page.goto = _page_goto_with_retry",
                "from media_platform.douyin.core import DouYinCrawler",
                "from media_platform.douyin.client import DouYinClient",
                "_original_create_douyin_client = DouYinCrawler.create_douyin_client",
                "async def _create_douyin_client_with_navigation_retry(self, httpx_proxy):",
                "    for attempt in range(3):",
                "        try:",
                "            return await _original_create_douyin_client(self, httpx_proxy)",
                "        except Exception as exc:",
                "            if 'Execution context was destroyed' not in str(exc) or attempt == 2:",
                "                raise",
                "            await self.context_page.wait_for_load_state('domcontentloaded', timeout=30000)",
                "            await asyncio.sleep(1)",
                "DouYinCrawler.create_douyin_client = _create_douyin_client_with_navigation_retry",
                "_original_get_aweme_detail = DouYinCrawler.get_aweme_detail",
                "async def _get_aweme_detail_with_network_retry(self, aweme_id, semaphore):",
                "    for attempt in range(3):",
                "        try:",
                "            return await _original_get_aweme_detail(self, aweme_id, semaphore)",
                "        except Exception as exc:",
                "            if attempt == 2:",
                "                print(f'[collector] skipped aweme {aweme_id} after network retries: {type(exc).__name__}')",
                "                return None",
                "            await asyncio.sleep(2 ** attempt)",
                "DouYinCrawler.get_aweme_detail = _get_aweme_detail_with_network_retry",
                f"collection_mode = {mode!r}",
                f"known_ids = set({sorted(known_ids)!r})",
                f"probe_count = {max(1, int(args.incremental_probe_count))}",
                f"report_file = Path({str(report_file)!r})",
                "if collection_mode == 'incremental':",
                "    async def _get_incremental_user_aweme_posts(self, sec_user_id, callback=None):",
                "        posts_has_more, max_cursor, checked = 1, '', 0",
                "        result, scanned_ids, seen_ids = [], [], set()",
                "        boundary_seen, boundary_id = False, None",
                "        stop_reason = 'history_exhausted'",
                "        while posts_has_more == 1:",
                "            response = await self.get_user_aweme_posts(sec_user_id, max_cursor)",
                "            posts_has_more = response.get('has_more', 0)",
                "            max_cursor = response.get('max_cursor')",
                "            page = response.get('aweme_list') or []",
                "            page = sorted(page, key=lambda item: int(item.get('create_time') or 0), reverse=True)",
                "            selected = []",
                "            should_stop = False",
                "            for item in page:",
                "                work_id = str(item.get('aweme_id') or '')",
                "                if not work_id or work_id in seen_ids:",
                "                    continue",
                "                seen_ids.add(work_id)",
                "                selected.append(item)",
                "                scanned_ids.append(work_id)",
                "                checked += 1",
                "                if work_id in known_ids:",
                "                    boundary_seen = True",
                "                    boundary_id = boundary_id or work_id",
                "                if boundary_seen and checked >= probe_count:",
                "                    should_stop = True",
                "                    stop_reason = 'known_boundary'",
                "                    break",
                "            if callback and selected:",
                "                await callback(selected)",
                "            result.extend(selected)",
                "            if should_stop:",
                "                break",
                "        report = {",
                "            'mode': collection_mode, 'probe_count': probe_count,",
                "            'checked_count': checked, 'scanned_aweme_ids': scanned_ids,",
                "            'known_boundary_aweme_id': boundary_id, 'stop_reason': stop_reason,",
                "        }",
                "        report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')",
                "        return result",
                "    DouYinClient.get_all_user_aweme_posts = _get_incremental_user_aweme_posts",
                f"config.DY_CREATOR_ID_LIST = [{creator_id!r}]",
                f"config.CRAWLER_MAX_NOTES_COUNT = {int(args.max_count)}",
                f"config.SAVE_DATA_OPTION = {args.save_data_option!r}",
                f"config.SAVE_DATA_PATH = {str(output_dir)!r}",
                "sys.argv = [",
                "    str(media_dir / 'main.py'),",
                "    '--platform', 'dy',",
                "    '--type', 'creator',",
                "    '--lt', " + repr(args.login_type) + ",",
                "    '--creator_id', " + repr(creator_id) + ",",
                "    '--crawler_max_notes_count', " + repr(str(int(args.max_count))) + ",",
                "    '--save_data_option', " + repr(args.save_data_option) + ",",
                "    '--save_data_path', " + repr(str(output_dir)) + ",",
                "    '--get_comment', 'false',",
                "]",
                "runpy.run_path(str(media_dir / 'main.py'), run_name='__main__')",
            ]
        ),
        encoding="utf-8",
    )
    command = [resolve_media_crawler_python(media_dir, args.media_crawler_python), str(bootstrap)]
    result = subprocess.run(command, cwd=media_dir, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    report = read_existing_payload(report_file) if report_file.exists() else {
        "mode": mode,
        "probe_count": max(1, int(args.incremental_probe_count)),
        "stop_reason": "history_exhausted" if mode == "full" else "report_missing",
    }
    return output_dir, report


def collect(args: argparse.Namespace) -> dict[str, Any]:
    output_file = Path(args.output_file).expanduser().resolve()
    state_file = Path(args.collection_state_file).expanduser().resolve() if args.collection_state_file else None
    creator_id = creator_id_from_url(args.creator_url)
    existing_payload = read_existing_payload(output_file)
    existing_works = works_from_payload(existing_payload)
    existing_ids = {str(item["aweme_id"]) for item in existing_works}
    state = read_collection_state(state_file)

    if args.mark_existing_full:
        if state_file is None:
            raise SystemExit("--mark-existing-full requires --collection-state-file.")
        if not existing_works:
            raise SystemExit("--mark-existing-full requires an existing normalized works file with at least one work.")
        report = {"mode": "mark_existing_full", "stop_reason": "existing_file_confirmed"}
        written_state = write_collection_state(
            state_file, creator_id=creator_id, full_history_collected=True,
            mode="mark_existing_full", works=existing_works, pending_ids=[], report=report,
        )
        return {
            "output_file": str(output_file), "count": len(existing_works),
            "collection_mode": "mark_existing_full", "full_history_collected": True,
            "new_count": 0, "pending_count": len(written_state.get("pending_aweme_ids", [])),
        }

    mode = determine_collection_mode(state, creator_id, existing_works, args.force_full_collect)
    if args.normalize_only:
        source_dir = Path(args.media_output_dir).expanduser().resolve()
        report = {"mode": "normalize_only", "stop_reason": "existing_output_normalized"}
    else:
        source_dir, report = run_mediacrawler(args, mode=mode, known_ids=existing_ids)

    records, used_files = load_records_from_output(source_dir)
    if not records and args.normalize_only:
        try:
            media_dir = resolve_media_crawler_dir(args.media_crawler_dir)
        except SystemExit:
            media_dir = None
        if media_dir:
            records, used_files = load_records_from_output(media_dir / "data")
    current_works = dedupe_works(records)
    if args.expect_min_count and len(current_works) < args.expect_min_count:
        raise SystemExit(f"Only found {len(current_works)} works, below --expect-min-count={args.expect_min_count}.")
    if not current_works:
        raise SystemExit("No Douyin works found in MediaCrawler output.")
    validate_core_fields(current_works)

    new_ids_set = {str(item["aweme_id"]) for item in current_works} - existing_ids
    merged_works = merge_works(existing_works, current_works)
    new_ids = ordered_ids(new_ids_set, merged_works)
    state_matches = str(state.get("creator_id") or "") == creator_id
    old_pending = (
        state.get("pending_aweme_ids", [])
        if state_matches and isinstance(state.get("pending_aweme_ids"), list)
        else []
    )
    pending_ids = ordered_ids([*old_pending, *new_ids], merged_works)
    completed_full = mode == "full" and not args.normalize_only
    full_history_collected = (state_matches and bool(state.get("full_history_collected"))) or completed_full
    payload = build_payload(
        merged_works, args.creator_url, used_files, mode=mode,
        full_history_collected=full_history_collected, new_ids=new_ids,
        pending_ids=pending_ids, report=report,
    )
    write_payload(payload, output_file)
    write_collection_state(
        state_file, creator_id=creator_id, full_history_collected=full_history_collected,
        mode=mode, works=merged_works, pending_ids=pending_ids, report=report,
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Douyin creator works through MediaCrawler and normalize them.")
    parser.add_argument("--creator-url", required=True, help="Douyin creator profile URL or sec_user_id.")
    parser.add_argument("--media-crawler-dir", help="Local MediaCrawler project checkout. Can also use MEDIACRAWLER_DIR.")
    parser.add_argument("--media-crawler-python", help="Python executable with MediaCrawler dependencies. Defaults to MEDIACRAWLER_PYTHON or MediaCrawler/.venv.")
    parser.add_argument("--media-output-dir", default=str(DEFAULT_MEDIA_OUTPUT_DIR))
    parser.add_argument("--output-file", default=str(DEFAULT_OUTPUT_FILE))
    parser.add_argument("--collection-state-file", help="Per-creator incremental collection state JSON.")
    parser.add_argument("--incremental-probe-count", type=int, default=3, help="Minimum newest works inspected before stopping at a known work.")
    parser.add_argument("--force-full-collect", action="store_true", help="Ignore the completed baseline marker and crawl all history again.")
    parser.add_argument("--mark-existing-full", action="store_true", help="Mark the existing normalized works file as a complete baseline without network access.")
    parser.add_argument("--max-count", type=int, default=200)
    parser.add_argument("--expect-min-count", type=int, default=1)
    parser.add_argument("--login-type", default="qrcode", choices=["qrcode", "phone", "cookie"])
    parser.add_argument(
        "--browser-profile-key",
        help="Stable per-creator browser profile key; login state persists in MediaCrawler/browser_data.",
    )
    parser.add_argument(
        "--cdp-port", type=int, default=9222,
        help="Per-run CDP starting port. MediaCrawler selects the next free port when needed.",
    )
    parser.add_argument("--save-data-option", default="jsonl", choices=["jsonl", "json", "csv"])
    parser.add_argument("--normalize-only", action="store_true", help="Skip running MediaCrawler; only normalize existing output files.")
    parser.add_argument("--clean-media-output", action="store_true")
    args = parser.parse_args()
    if args.incremental_probe_count < 1:
        parser.error("--incremental-probe-count must be at least 1")
    if not 1 <= args.cdp_port <= 65535:
        parser.error("--cdp-port must be between 1 and 65535")
    payload = collect(args)
    print(json.dumps({
        "output_file": str(Path(args.output_file)),
        "count": payload["count"],
        "collection_mode": payload.get("collection_mode"),
        "new_count": payload.get("new_count", 0),
        "pending_count": payload.get("pending_count", 0),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
