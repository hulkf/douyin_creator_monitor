"""Shared rules for user-supplied file or directory inputs."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterable


def iter_input_files(
    input_path: str | Path,
    *,
    pattern: str = "*",
    suffixes: Iterable[str] | None = None,
) -> list[Path]:
    """Accept one file or scan only the direct files in one directory."""
    raw_path = str(input_path).strip()
    if len(raw_path) >= 2 and raw_path[0] == raw_path[-1] and raw_path[0] in {'"', "'"}:
        raw_path = raw_path[1:-1]
    path = Path(raw_path).expanduser()
    wanted_suffixes = {str(item).lower() for item in (suffixes or ())}

    if path.is_file():
        return [path] if _matches(path, pattern, wanted_suffixes) else []
    if not path.exists() or not path.is_dir():
        raise NotADirectoryError(f"输入路径不存在或不是文件夹: {path}")
    return sorted(
        (child for child in path.iterdir() if child.is_file() and _matches(child, pattern, wanted_suffixes)),
        key=lambda item: item.name.lower(),
    )


def _matches(path: Path, pattern: str, suffixes: set[str]) -> bool:
    return fnmatch.fnmatch(path.name, pattern) and (
        not suffixes or path.suffix.lower() in suffixes
    )
