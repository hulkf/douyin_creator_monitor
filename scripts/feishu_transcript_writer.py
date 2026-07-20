#!/usr/bin/env python3
"""Write ASR transcript text back to a Feishu Base work record."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
LOCAL_FEISHU_IDS = PROJECT_DIR / "local" / "feishu-ids.md"
DEFAULT_CLI_CANDIDATES = [
    PROJECT_DIR / "tools" / "lark-cli" / "lark-cli.exe",
    Path("lark-cli"),
]


def resolve_lark_cli(cli_path: str | None = None) -> str:
    if cli_path:
        return cli_path
    for candidate in DEFAULT_CLI_CANDIDATES:
        if candidate.exists() or str(candidate) == "lark-cli":
            return str(candidate)
    return "lark-cli"


def load_base_token(base_token: str | None = None) -> str:
    if base_token:
        return base_token
    env_value = os.environ.get("FEISHU_BASE_TOKEN", "").strip()
    if env_value:
        return env_value
    if LOCAL_FEISHU_IDS.exists():
        text = LOCAL_FEISHU_IDS.read_text(encoding="utf-8-sig")
        match = re.search(r"base_token:\s*([A-Za-z0-9]+)", text)
        if match:
            return match.group(1)
    raise SystemExit("Missing Feishu base token. Pass --base-token or set FEISHU_BASE_TOKEN.")


def read_text_arg(value: str | None, file_value: str | None) -> str:
    if file_value:
        return Path(file_value).read_text(encoding="utf-8-sig").strip()
    return value or ""


def run_lark(cli: str, args: list[str], input_text: str | None = None) -> dict:
    result = subprocess.run(
        [cli, *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=input_text,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or f"lark-cli exited {result.returncode}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"lark-cli did not return JSON: {result.stdout[:1000]}") from exc


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def find_records(payload: dict) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    for item in iter_dicts(payload):
        record_id = item.get("record_id") or item.get("id")
        if isinstance(record_id, str) and record_id.startswith("rec") and record_id not in seen:
            records.append(item)
            seen.add(record_id)
    return records


def find_record_by_work_id(
    cli: str,
    base_token: str,
    table_id: str,
    work_id: str,
    work_id_field: str,
    as_identity: str,
) -> str:
    payload = run_lark(
        cli,
        [
            "base",
            "+record-search",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--keyword",
            work_id,
            "--search-field",
            work_id_field,
            "--field-id",
            work_id_field,
            "--limit",
            "10",
            "--format",
            "json",
            "--as",
            as_identity,
        ],
    )
    records = find_records(payload)
    if not records:
        # Base keyword search can return no rows for long numeric identifiers
        # even when the exact text exists. Fall back to a field-scoped list and
        # compare cell values exactly so transcript writeback stays reliable.
        offset = 0
        while True:
            listed = run_lark(
                cli,
                [
                    "base", "+record-list", "--base-token", base_token,
                    "--table-id", table_id, "--field-id", work_id_field,
                    "--offset", str(offset), "--limit", "200",
                    "--format", "json", "--as", as_identity,
                ],
            )
            data = listed.get("data") if isinstance(listed.get("data"), dict) else {}
            rows = data.get("data") if isinstance(data.get("data"), list) else []
            record_ids = data.get("record_id_list") if isinstance(data.get("record_id_list"), list) else []
            records.extend(
                {"record_id": record_id}
                for row, record_id in zip(rows, record_ids)
                if isinstance(row, list) and row and str(row[0] or "").strip() == work_id
            )
            if not data.get("has_more"):
                break
            offset += 200
    if not records:
        raise SystemExit(f"No Feishu record found for {work_id_field}={work_id}")
    if len(records) > 1:
        raise SystemExit(f"Multiple Feishu records found for {work_id_field}={work_id}; please clean duplicates first.")
    record_id = records[0].get("record_id") or records[0].get("id")
    if not isinstance(record_id, str):
        raise SystemExit("Matched record did not include record_id.")
    return record_id


def write_transcript(
    cli: str,
    base_token: str,
    table_id: str,
    record_id: str,
    patch: dict[str, Any],
    as_identity: str,
    dry_run: bool = False,
) -> dict:
    args = [
        "base",
        "+record-upsert",
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--record-id",
        record_id,
        "--json",
        json.dumps(patch, ensure_ascii=False),
        "--format",
        "json",
        "--as",
        as_identity,
    ]
    if dry_run:
        args.append("--dry-run")
    return run_lark(cli, args)


def redact_value(value: Any, base_token: str) -> Any:
    if isinstance(value, dict):
        return {key: redact_value(child, base_token) for key, child in value.items()}
    if isinstance(value, list):
        return [redact_value(child, base_token) for child in value]
    if isinstance(value, str) and base_token:
        return value.replace(base_token, "<BASE_TOKEN>")
    return value


def build_patch(args: argparse.Namespace) -> dict[str, Any]:
    transcript = read_text_arg(args.transcript, args.transcript_file)
    corrected = read_text_arg(args.corrected_transcript, args.corrected_transcript_file)
    final_text = corrected or transcript

    if not final_text:
        raise SystemExit("Nothing to write. Pass --transcript/--transcript-file or --corrected-transcript/--corrected-transcript-file.")

    return {args.transcript_field: final_text}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-token")
    parser.add_argument("--table-id", required=True, help="Creator-specific Feishu work table ID.")
    parser.add_argument("--work-id", required=True, help="Douyin work/video ID.")
    parser.add_argument("--work-id-field", default="抖音作品ID")
    parser.add_argument("--record-id", help="Known Feishu record ID. Skips search when provided.")
    parser.add_argument("--lark-cli")
    parser.add_argument("--as", dest="as_identity", default="user", choices=["user", "bot"])
    parser.add_argument("--dry-run", action="store_true")

    parser.add_argument("--transcript")
    parser.add_argument("--transcript-file")
    parser.add_argument("--corrected-transcript")
    parser.add_argument("--corrected-transcript-file")
    parser.add_argument("--raw-json")
    parser.add_argument("--raw-json-file")
    parser.add_argument("--correction-report")
    parser.add_argument("--correction-report-file")

    parser.add_argument("--transcript-field", default="语音转写全文")
    args = parser.parse_args()

    cli = resolve_lark_cli(args.lark_cli)
    base_token = load_base_token(args.base_token)
    record_id = args.record_id or find_record_by_work_id(
        cli,
        base_token,
        args.table_id,
        args.work_id,
        args.work_id_field,
        args.as_identity,
    )
    response = write_transcript(
        cli,
        base_token,
        args.table_id,
        record_id,
        build_patch(args),
        args.as_identity,
        dry_run=args.dry_run,
    )
    safe_response = redact_value(response, base_token)
    print(json.dumps({"record_id": record_id, "response": safe_response}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
