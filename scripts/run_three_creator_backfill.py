#!/usr/bin/env python3
"""Resumable historical backfill for the three configured Douyin creators.

This fixed entrypoint is intentionally suitable for Windows Task Scheduler.
It keeps Feishu work sync, transcript/status writeback, ASR, correction, IMA,
Kuake, and Obsidian enabled. Completed full-history collection markers are
honored, so restarts do not crawl all creator pages again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
PIPELINE = PROJECT_DIR / "scripts" / "run_creator_pipeline.py"
CONFIG_FILE = PROJECT_DIR / "local" / "pipeline.json"
STATE_DIR_DEFAULT = PROJECT_DIR / "runtime" / "pipeline"
MEDIA_DIR_DEFAULT = PROJECT_DIR / "runtime" / "media"
RUNS_DIR = STATE_DIR_DEFAULT / "runs"
COLLECTION_DIR = STATE_DIR_DEFAULT / "collection"
LOCK_FILE = STATE_DIR_DEFAULT / "three-creator-backfill.lock"
LOG_DIR = PROJECT_DIR / "logs" / "backfill"
CREATORS = ("zhiliao", "nuomi", "aligc")
BATCH_SIZE = 20
ASR_WORKERS = 4
BACKUP_WORKERS = 3
MAX_ATTEMPTS_PER_WORK = 3
MAX_ROUNDS_PER_CREATOR = 40
REQUIRED_BACKUPS = (
    ("ima", "ima_backed_up"),
    ("kuake", "kuake_backed_up"),
    ("obsidian", "obsidian_exported"),
)


def log(message: str, handle) -> None:
    line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
    console_encoding = sys.stdout.encoding or "utf-8"
    safe_line = line.encode(console_encoding, errors="replace").decode(console_encoding)
    print(safe_line, flush=True)
    handle.write(line + "\n")
    handle.flush()


def project_path(value: Any, default: Path) -> Path:
    raw = str(value or "").strip()
    path = Path(raw) if raw else default
    return path if path.is_absolute() else PROJECT_DIR / path


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def stage_status(state: dict[str, Any], stage: str) -> str:
    value = state.get("stages", {}).get(stage, {}) if isinstance(state.get("stages"), dict) else {}
    if isinstance(value, dict):
        return str(value.get("status") or "")
    return str(value or "")


def collection_is_complete(creator: str, state_dir: Path = STATE_DIR_DEFAULT) -> bool:
    return bool(read_json(state_dir / "collection" / f"{creator}.json").get("full_history_collected"))


def creator_config(config: dict[str, Any], creator: str) -> dict[str, Any]:
    for item in config.get("creators", []):
        if isinstance(item, dict) and str(item.get("key") or item.get("creator_key") or "") == creator:
            return item
    raise RuntimeError(f"配置中找不到达人: {creator}")


def pending_works(config: dict[str, Any], creator: str) -> list[dict[str, Any]]:
    creator_item = creator_config(config, creator)
    state_dir = project_path(config.get("state_dir"), STATE_DIR_DEFAULT)
    media_dir = project_path(config.get("media_dir"), MEDIA_DIR_DEFAULT)
    works_file = project_path(
        creator_item.get("works_file"),
        PROJECT_DIR / "runtime" / f"{creator}-works-from-mediacrawler.json",
    )
    payload = read_json(works_file)
    works = [item for item in payload.get("works", []) if isinstance(item, dict) and item.get("aweme_id")]
    required_stages = [
        stage for section_name, stage in REQUIRED_BACKUPS
        if bool(config.get(section_name, {}).get("enabled", True))
    ]
    required_stages.extend(("feishu_synced", "feishu_written_back", "backup_statuses_written_back"))

    pending: list[dict[str, Any]] = []
    for work in works:
        work_id = str(work["aweme_id"])
        final_path = media_dir / f"{work_id}.final.txt"
        state = read_json(state_dir / creator / f"{work_id}.json")
        final_missing = not final_path.is_file() or final_path.stat().st_size == 0
        backup_missing = any(stage_status(state, stage) != "success" for stage in required_stages)
        if final_missing or backup_missing:
            pending.append(work)
    pending.sort(key=lambda item: int(item.get("create_time") or 0), reverse=True)
    return pending


def snapshots() -> dict[Path, int]:
    if not RUNS_DIR.exists():
        return {}
    return {path: path.stat().st_mtime_ns for path in RUNS_DIR.glob("*.json")}


def newest_summary(previous: dict[Path, int]) -> Path | None:
    if not RUNS_DIR.exists():
        return None
    changed = [
        path for path in RUNS_DIR.glob("*.json")
        if path not in previous or path.stat().st_mtime_ns != previous[path]
    ]
    candidates = changed or list(RUNS_DIR.glob("*.json"))
    return max(candidates, key=lambda path: path.stat().st_mtime_ns) if candidates else None


def read_creator_result(summary_path: Path | None, creator: str) -> dict[str, Any]:
    if not summary_path:
        return {}
    payload = read_json(summary_path)
    for result in payload.get("creators", []):
        if isinstance(result, dict) and result.get("key") == creator:
            return result
    return {}


def run_batch(
    creator: str,
    *,
    skip_collect: bool,
    aweme_ids: list[str] | None,
    handle,
) -> tuple[int, dict[str, Any]]:
    command = [
        sys.executable,
        str(PIPELINE),
        "--creator", creator,
        "--backfill-existing",
        "--max-works", str(BATCH_SIZE),
        "--asr-workers", str(ASR_WORKERS),
        "--backup-workers", str(BACKUP_WORKERS),
    ]
    command.append("--skip-collect" if skip_collect else "--force-full-collect")
    for work_id in aweme_ids or []:
        command.extend(["--aweme-id", work_id])

    before = snapshots()
    log(
        f"START creator={creator} collect={'skip' if skip_collect else 'full'} "
        f"ids={','.join(aweme_ids or []) or 'auto'}",
        handle,
    )
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["VOLC_ASR_QUERY_ATTEMPTS"] = "180"
    env["VOLC_ASR_QUERY_INTERVAL_SECONDS"] = "2"
    process = subprocess.Popen(
        command,
        cwd=PROJECT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert process.stdout is not None
    for line in process.stdout:
        console_encoding = sys.stdout.encoding or "utf-8"
        safe_line = line.encode(console_encoding, errors="replace").decode(console_encoding)
        sys.stdout.write(safe_line)
        sys.stdout.flush()
        handle.write(line)
        handle.flush()
    return_code = process.wait()
    summary = newest_summary(before)
    result = read_creator_result(summary, creator)
    log(
        f"END creator={creator} rc={return_code} selected={result.get('selected_count', 'unknown')} "
        f"status={result.get('status', 'unknown')} summary={summary}",
        handle,
    )
    return return_code, result


def acquire_lock() -> int:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        age_seconds = time.time() - LOCK_FILE.stat().st_mtime
        if age_seconds < 24 * 3600:
            raise RuntimeError(f"已有补录任务锁：{LOCK_FILE}")
        LOCK_FILE.unlink()
        return os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)


def main() -> int:
    config = read_json(CONFIG_FILE)
    if not config:
        raise RuntimeError(f"无法读取流水线配置：{CONFIG_FILE}")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"three-creators-{datetime.now():%Y%m%d-%H%M%S}.log"
    lock_fd = acquire_lock()
    os.write(lock_fd, f"pid={os.getpid()} started={datetime.now().astimezone().isoformat()}\n".encode())
    os.close(lock_fd)

    incomplete: dict[str, list[str]] = {}
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            log(f"Backfill started; log={log_path}", handle)
            for creator in CREATORS:
                attempts: dict[str, int] = {}
                state_dir = project_path(config.get("state_dir"), STATE_DIR_DEFAULT)
                if not collection_is_complete(creator, state_dir):
                    log(f"FULL COLLECTION REQUIRED creator={creator}", handle)
                    run_batch(creator, skip_collect=False, aweme_ids=None, handle=handle)
                else:
                    log(f"FULL COLLECTION ALREADY COMPLETE creator={creator}; skip crawl", handle)

                for round_number in range(1, MAX_ROUNDS_PER_CREATOR + 1):
                    pending = pending_works(config, creator)
                    if not pending:
                        log(f"COMPLETE creator={creator} rounds={round_number - 1}", handle)
                        break
                    eligible = [
                        work for work in pending
                        if attempts.get(str(work["aweme_id"]), 0) < MAX_ATTEMPTS_PER_WORK
                    ]
                    eligible.sort(
                        key=lambda work: (
                            attempts.get(str(work["aweme_id"]), 0),
                            -int(work.get("create_time") or 0),
                        )
                    )
                    selected_ids = [str(work["aweme_id"]) for work in eligible[:BATCH_SIZE]]
                    if not selected_ids:
                        remaining = [str(work["aweme_id"]) for work in pending]
                        incomplete[creator] = remaining
                        log(f"STOP creator={creator}: retry limit reached ids={','.join(remaining)}", handle)
                        break

                    run_batch(creator, skip_collect=True, aweme_ids=selected_ids, handle=handle)
                    still_pending = {str(work["aweme_id"]) for work in pending_works(config, creator)}
                    for work_id in selected_ids:
                        if work_id in still_pending:
                            attempts[work_id] = attempts.get(work_id, 0) + 1
                else:
                    remaining = [str(work["aweme_id"]) for work in pending_works(config, creator)]
                    if remaining:
                        incomplete[creator] = remaining
                        log(f"STOP creator={creator}: round limit reached ids={','.join(remaining)}", handle)

            if incomplete:
                detail = "; ".join(f"{creator}={','.join(ids)}" for creator, ids in incomplete.items())
                log(f"Backfill incomplete {detail}", handle)
                return 1
            log("Backfill completed for all creators", handle)
            return 0
    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
