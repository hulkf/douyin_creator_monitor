from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_DIR / "logs"
ROTATABLE_PATTERNS = (
    "bat-*.log",
    "pipeline-*.log",
    "new-creator-check-*.log",
    "reconcile-*.log",
    "feishu-identity-*.log",
    "task-launch-*.log",
)


def rotate_logs(
    log_dir: Path,
    *,
    max_task_log_bytes: int = 5 * 1024 * 1024,
    retention_days: int = 60,
    now: float | None = None,
) -> dict[str, object]:
    if max_task_log_bytes < 1:
        raise ValueError("max_task_log_bytes must be positive")
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    log_dir = log_dir.expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    now_epoch = time.time() if now is None else float(now)
    rotated = ""
    task_log = log_dir / "task-launch.log"
    if task_log.exists() and task_log.stat().st_size >= max_task_log_bytes:
        stamp = datetime.fromtimestamp(now_epoch).strftime("%Y%m%d-%H%M%S")
        destination = log_dir / f"task-launch-{stamp}.log"
        suffix = 1
        while destination.exists():
            destination = log_dir / f"task-launch-{stamp}-{suffix}.log"
            suffix += 1
        task_log.replace(destination)
        rotated = destination.name

    cutoff = now_epoch - retention_days * 86400
    removed: list[str] = []
    candidates: set[Path] = set()
    for pattern in ROTATABLE_PATTERNS:
        candidates.update(log_dir.glob(pattern))
    for path in sorted(candidates):
        if rotated and path.name == rotated:
            continue
        if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path.name)
    return {"rotated": rotated, "removed": removed, "retention_days": retention_days}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate and retain local pipeline logs safely.")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--max-task-log-bytes", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--retention-days", type=int, default=60)
    args = parser.parse_args(argv)
    result = rotate_logs(
        args.log_dir,
        max_task_log_bytes=args.max_task_log_bytes,
        retention_days=args.retention_days,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
