"""Sync normalized Douyin works into a Feishu Base works table.

The sync is intentionally non-destructive:
- match existing rows by "抖音作品ID";
- update only fields present in the current normalized input;
- create rows missing from Feishu;
- never delete rows that are absent from the current Douyin capture.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from work_table_schema import CANONICAL_WORK_FIELDS, CANONICAL_WORK_FIELD_NAMES
from verify_feishu_cli_identity import isolated_lark_env, scoped_lark_command


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_WORKS_FILE = PROJECT_DIR / "runtime" / "zhiliao-works-from-mediacrawler.json"
DEFAULT_LARK_CLI = PROJECT_DIR / "tools" / "lark-cli" / "lark-cli.exe"
BEIJING_TZ = timezone(timedelta(hours=8))
HASHTAG_RE = re.compile(r"#([^#\s]+)")
SYNC_COMPARE_FIELDS = (
    "抖音作品ID", "作品链接", "作品类型", "是否置顶", "作品标题", "原始文案",
    "话题标签", "发布时间", "点赞数", "评论数", "收藏数", "分享数", "封面图URL", "采集状态",
    "原始作品数据",
)
CORE_VERIFY_FIELDS = ("抖音作品ID", "发布时间", "点赞数", "评论数", "收藏数", "分享数")

MAX_BATCH_CREATE_RECORDS = 200
MAX_BATCH_CREATE_JSON_CHARS = 20_000
RETRYABLE_FEISHU_WRITE_CODES = {800004135, 1254290, 1254291}
FEISHU_WRITE_ATTEMPTS = 5


def retryable_feishu_write_error(payload: Any) -> bool:
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    try:
        code = int(error.get("code"))
    except (TypeError, ValueError):
        code = 0
    message = str(error.get("message") or "").casefold()
    return code in RETRYABLE_FEISHU_WRITE_CODES or (
        "limited" in message or "write conflict" in message or "too many request" in message
    )


def run_lark(cli: str, args: list[str]) -> dict[str, Any]:
    for attempt in range(FEISHU_WRITE_ATTEMPTS):
        result = subprocess.run(
            scoped_lark_command(cli, args),
            cwd=PROJECT_DIR,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            env=isolated_lark_env(),
        )
        output = result.stdout if result.returncode == 0 else result.stderr or result.stdout
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"lark-cli did not return JSON: {output[:1000]}") from exc
        if result.returncode == 0 and payload.get("ok", False):
            return payload
        if retryable_feishu_write_error(payload) and attempt + 1 < FEISHU_WRITE_ATTEMPTS:
            time.sleep(min(2 ** attempt, 8))
            continue
        raise SystemExit(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit("Feishu write retry loop exhausted.")


def beijing_time(epoch_seconds: int | float | None) -> str | None:
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc).astimezone(BEIJING_TZ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def extract_hashtags(desc: str) -> str:
    return " ".join(f"#{tag}" for tag in HASHTAG_RE.findall(desc or ""))


def title_from_desc(desc: str) -> str:
    title = HASHTAG_RE.sub("", desc or "")
    title = re.sub(r"\s+", " ", title).strip()
    return title[:120] or (desc or "")[:120]


def build_patch(work: dict[str, Any], captured_at: str) -> dict[str, Any]:
    desc = work.get("desc") or ""
    patch: dict[str, Any] = {
        "抖音作品ID": str(work["aweme_id"]),
        "作品链接": work.get("url") or f"https://www.douyin.com/video/{work['aweme_id']}",
        "作品类型": "视频",
        "是否置顶": "是" if work.get("is_top") else "否",
        "作品标题": title_from_desc(desc),
        "原始文案": desc,
        "话题标签": extract_hashtags(desc),
        "发布时间": beijing_time(work.get("create_time")),
        "点赞数": work.get("digg_count"),
        "评论数": work.get("comment_count"),
        "收藏数": work.get("collect_count"),
        "分享数": work.get("share_count"),
        "最近采集时间": captured_at,
        "记录时间": captured_at,
        "采集状态": "已完成",
        "原始作品数据": json.dumps(work, ensure_ascii=False, separators=(",", ":")),
    }
    if work.get("cover_url"):
        patch["封面图URL"] = work["cover_url"]
    return {key: value for key, value in patch.items() if value is not None}


def load_existing_records(
    cli: str, base_token: str, table_id: str, as_identity: str = "user",
) -> dict[str, dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    offset = 0
    page_size = 200
    requested_fields = sorted(SYNC_COMPARE_FIELDS)
    while True:
        command = [
            "base", "+record-list", "--base-token", base_token,
            "--table-id", table_id,
        ]
        for field_name in requested_fields:
            command.extend(["--field-id", field_name])
        command.extend([
            "--offset", str(offset), "--limit", str(page_size),
            "--format", "json", "--as", as_identity,
        ])
        payload = run_lark(cli, command)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        names = data.get("fields") if isinstance(data.get("fields"), list) else requested_fields
        rows = data.get("data") if isinstance(data.get("data"), list) else []
        record_ids = data.get("record_id_list") if isinstance(data.get("record_id_list"), list) else []
        for row, record_id in zip(rows, record_ids):
            if not isinstance(row, list):
                continue
            fields = {str(name): value for name, value in zip(names, row)}
            work_id = str(fields.get("抖音作品ID") or "").strip()
            if work_id:
                if work_id in existing:
                    raise SystemExit(f"Duplicate Feishu records found for 抖音作品ID={work_id}")
                existing[work_id] = {"record_id": record_id, "fields": fields}
        if not data.get("has_more"):
            break
        offset += page_size
    return existing


MARKDOWN_LINK_RE = re.compile(r"^\[[^]]*\]\((https?://[^)]+)\)$")
VOLATILE_SYNC_FIELDS = {"最近采集时间", "记录时间"}


def normalized_cell(value: Any) -> Any:
    if isinstance(value, list) and len(value) == 1:
        return normalized_cell(value[0])
    if isinstance(value, str):
        match = MARKDOWN_LINK_RE.match(value.strip())
        return match.group(1) if match else value.strip()
    return value


def has_content_changes(existing: dict[str, Any], patch: dict[str, Any]) -> bool:
    fields = existing.get("fields") if isinstance(existing.get("fields"), dict) else {}
    return any(
        normalized_cell(fields.get(name)) != normalized_cell(value)
        for name, value in patch.items()
        if name in SYNC_COMPARE_FIELDS
    )


def plan_sync(
    works: list[dict[str, Any]], existing: dict[str, dict[str, Any]], captured_at: str,
) -> dict[str, list[dict[str, Any]]]:
    plan: dict[str, list[dict[str, Any]]] = {"create": [], "update": [], "skip": []}
    seen: set[str] = set()
    for work in works:
        work_id = str(work["aweme_id"])
        if work_id in seen:
            raise SystemExit(f"Duplicate work in normalized input: {work_id}")
        seen.add(work_id)
        patch_value = build_patch(work, captured_at)
        current = existing.get(work_id)
        if current is None:
            plan["create"].append({"work_id": work_id, "patch": patch_value})
        elif has_content_changes(current, patch_value):
            plan["update"].append({
                "work_id": work_id,
                "record_id": str(current["record_id"]),
                "patch": patch_value,
            })
        else:
            plan["skip"].append({
                "work_id": work_id,
                "record_id": str(current["record_id"]),
                "patch": patch_value,
            })
    return plan


def comparable_schema_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: comparable_schema_value(child) for key, child in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [comparable_schema_value(child) for child in value]
    return value


# Options the pipeline does NOT require to pre-exist in the live table, because
# feishu_work_status_writer.py auto-creates them on write (single-select options are
# created lazily by lark-cli when first written). They may legitimately be absent from
# a work table, so the schema validator must not treat their absence as a mismatch.
ALLOWED_MISSING_OPTIONS = frozenset({"内容总结待补充"})


def _option_names(options: Any) -> set[str]:
    if not isinstance(options, (list, tuple)):
        return set()
    return {str(o.get("name")) for o in options if isinstance(o, dict) and o.get("name")}


def schema_mismatches(live_fields: list[dict[str, Any]]) -> list[str]:
    live_by_name = {str(field.get("name")): field for field in live_fields}
    problems: list[str] = []
    for expected in CANONICAL_WORK_FIELDS:
        name = str(expected["name"])
        actual = live_by_name.get(name)
        if actual is None:
            problems.append(f"missing: {name}")
            continue
        if actual.get("type") != expected.get("type"):
            problems.append(f"{name}: type expected {expected.get('type')}, got {actual.get('type')}")
            continue
        # style / multiple are compared exactly (order/cosmetic-insensitive via
        # comparable_schema_value's sorted-key normalization).
        for key in ("style", "multiple"):
            if key in expected and comparable_schema_value(actual.get(key)) != comparable_schema_value(expected.get(key)):
                problems.append(f"{name}: {key} mismatch")
        # options: compare by NAME SET, not as an ordered list. Single-select option
        # ORDER and cosmetic attributes (hue/lightness) drift naturally (options are
        # auto-created by the status writer in arbitrary order) and are not
        # semantically meaningful, so they must not trigger a schema mismatch.
        # Only flag options genuinely missing from the live field — and even then,
        # ignore those the writer auto-creates on demand.
        if "options" in expected:
            missing = (_option_names(expected["options"]) - _option_names(actual.get("options"))) - ALLOWED_MISSING_OPTIONS
            if missing:
                problems.append(f"{name}: options missing {sorted(missing)}")
    return problems


def validate_live_schema(
    cli: str, base_token: str, table_id: str, as_identity: str = "user",
) -> None:
    payload = run_lark(
        cli,
        [
            "base", "+field-list", "--base-token", base_token,
            "--table-id", table_id, "--format", "json", "--as", as_identity,
        ],
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    fields = data.get("fields") if isinstance(data.get("fields"), list) else []
    problems = schema_mismatches(fields)
    if problems:
        raise SystemExit("Feishu work table schema mismatch: " + "; ".join(problems))


def batch_create_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    field_order = [
        str(field["name"])
        for field in CANONICAL_WORK_FIELDS
        if any(field["name"] in item["patch"] for item in items)
    ]
    return {
        "fields": field_order,
        "rows": [[item["patch"].get(field_name) for field_name in field_order] for item in items],
    }


def create_batches(
    items: list[dict[str, Any]],
    *,
    maximum_records: int = MAX_BATCH_CREATE_RECORDS,
    maximum_json_chars: int = MAX_BATCH_CREATE_JSON_CHARS,
) -> list[tuple[list[dict[str, Any]], str]]:
    """Split creates by both API row count and Windows command-line payload size."""
    if maximum_records < 1 or maximum_json_chars < 1:
        raise ValueError("Batch limits must be positive")

    batches: list[tuple[list[dict[str, Any]], str]] = []
    current: list[dict[str, Any]] = []
    current_json = ""
    for item in items:
        candidate = [*current, item]
        candidate_json = json.dumps(batch_create_payload(candidate), ensure_ascii=False)
        exceeds_limit = len(candidate) > maximum_records or len(candidate_json) > maximum_json_chars
        if current and exceeds_limit:
            batches.append((current, current_json))
            current = [item]
            current_json = json.dumps(batch_create_payload(current), ensure_ascii=False)
        else:
            current = candidate
            current_json = candidate_json
    if current:
        batches.append((current, current_json))
    return batches


def extract_record_ids(payload: Any) -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        direct = payload.get("record_id_list")
        if isinstance(direct, list):
            found.extend(str(value) for value in direct if isinstance(value, str))
        record_id = payload.get("record_id") or payload.get("id")
        if isinstance(record_id, str) and record_id.startswith("rec"):
            found.append(record_id)
        for value in payload.values():
            found.extend(extract_record_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            found.extend(extract_record_ids(value))
    return list(dict.fromkeys(found))


def structured_record_fields(value: Any) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    if isinstance(value, dict):
        fields = value.get("fields")
        record_id = value.get("record_id") or value.get("id")
        if isinstance(record_id, str) and isinstance(fields, dict):
            records[record_id] = fields
        for child in value.values():
            records.update(structured_record_fields(child))
    elif isinstance(value, list):
        for child in value:
            records.update(structured_record_fields(child))
    return records


def verify_written_records(
    cli: str, base_token: str, table_id: str,
    record_ids: dict[str, str], expected_fields: dict[str, dict[str, Any]],
    as_identity: str = "user",
) -> int:
    """Read newly written rows back and fail if any core field is absent or changed."""
    verified = 0
    by_record_id = {record_id: work_id for work_id, record_id in record_ids.items()}
    requested_ids = list(by_record_id)
    for start in range(0, len(requested_ids), 200):
        batch_ids = requested_ids[start:start + 200]
        command = [
            "base", "+record-get", "--base-token", base_token, "--table-id", table_id,
        ]
        for record_id in batch_ids:
            command.extend(["--record-id", record_id])
        for field_name in CORE_VERIFY_FIELDS:
            command.extend(["--field-id", field_name])
        command.extend(["--format", "json", "--as", as_identity])
        payload = run_lark(cli, command)
        structured = structured_record_fields(payload)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        names = data.get("fields") if isinstance(data.get("fields"), list) else list(CORE_VERIFY_FIELDS)
        rows = data.get("data") if isinstance(data.get("data"), list) else []
        returned_ids = data.get("record_id_list") if isinstance(data.get("record_id_list"), list) else batch_ids
        if structured:
            returned_ids = [record_id for record_id in batch_ids if record_id in structured]
            rows = [[structured[record_id].get(name) for name in CORE_VERIFY_FIELDS] for record_id in returned_ids]
            names = list(CORE_VERIFY_FIELDS)
        if len(rows) != len(batch_ids):
            raise SystemExit(f"Feishu readback returned {len(rows)} rows for {len(batch_ids)} written records")
        seen: set[str] = set()
        for row, record_id in zip(rows, returned_ids):
            actual = {str(name): value for name, value in zip(names, row)}
            work_id = by_record_id.get(str(record_id)) or str(actual.get("抖音作品ID") or "")
            expected = expected_fields.get(work_id)
            if expected is None:
                raise SystemExit(f"Feishu readback returned an unexpected record: {record_id}")
            problems = [
                name for name in CORE_VERIFY_FIELDS
                if actual.get(name) is None
                or normalized_cell(actual.get(name)) != normalized_cell(expected.get(name))
            ]
            if problems:
                raise SystemExit(f"Feishu write verification failed for 抖音作品ID={work_id}: {problems}")
            seen.add(work_id)
            verified += 1
        missing = {by_record_id[record_id] for record_id in batch_ids} - seen
        if missing:
            raise SystemExit(f"Feishu write verification missed works: {sorted(missing)}")
    return verified


def sync(args: argparse.Namespace) -> dict[str, Any]:
    base_token = args.base_token or os.environ.get("FEISHU_BASE_TOKEN")
    if not base_token:
        raise SystemExit("Missing --base-token or FEISHU_BASE_TOKEN.")
    as_identity = str(getattr(args, "as_identity", "user") or "user")
    works_payload = json.loads(Path(args.works_file).read_text(encoding="utf-8"))
    works = works_payload.get("works") or []
    if not works:
        raise SystemExit("No works found in normalized input.")
    missing = [
        work.get("aweme_id")
        for work in works
        if not work.get("aweme_id")
        or not work.get("create_time")
        or work.get("digg_count") is None
        or work.get("comment_count") is None
        or work.get("collect_count") is None
        or work.get("share_count") is None
    ]
    if missing:
        raise SystemExit(f"Refusing to sync; missing core fields for works: {missing[:10]}")

    captured_at = beijing_time(datetime.now(tz=timezone.utc).timestamp()) or ""
    if not args.skip_schema_validation:
        validate_live_schema(args.lark_cli, base_token, args.table_id, as_identity)
    existing = load_existing_records(args.lark_cli, base_token, args.table_id, as_identity)
    existing_before = len(existing)
    plan = plan_sync(works, existing, captured_at)
    updated = len(plan["update"])
    created = len(plan["create"])
    skipped = len(plan["skip"])
    actions: list[dict[str, str]] = []
    record_ids = {
        item["work_id"]: item["record_id"]
        for category in ("update", "skip")
        for item in plan[category]
    }

    if args.dry_run:
        actions.extend({"work_id": item["work_id"], "action": "create"} for item in plan["create"])
    else:
        for batch, batch_json in create_batches(plan["create"]):
            response = run_lark(
                args.lark_cli,
                [
                    "base", "+record-batch-create", "--base-token", base_token,
                    "--table-id", args.table_id,
                    "--json", batch_json,
                    "--format", "json", "--as", as_identity,
                ],
            )
            created_ids = extract_record_ids(response)
            if len(created_ids) != len(batch):
                raise SystemExit(
                    f"Feishu batch create returned {len(created_ids)} record IDs for {len(batch)} works"
                )
            for item, record_id in zip(batch, created_ids):
                record_ids[item["work_id"]] = record_id
                actions.append({"work_id": item["work_id"], "action": "created", "record_id": record_id})

    for item in plan["update"]:
        work_id = item["work_id"]
        record_id = item["record_id"]
        patch_value = item["patch"]
        command = [
            "base",
            "+record-upsert",
            "--base-token",
            base_token,
            "--table-id",
            args.table_id,
            "--json",
            json.dumps(patch_value, ensure_ascii=False),
            "--as",
            as_identity,
            "--record-id",
            record_id,
        ]
        if args.dry_run:
            actions.append({"work_id": work_id, "action": "update", "record_id": record_id})
            continue
        response = run_lark(args.lark_cli, command)
        data = response.get("data") or {}
        new_record_id = data.get("record_id") or data.get("id") or record_id
        record_ids[work_id] = str(new_record_id)
        actions.append({"work_id": work_id, "action": "updated", "record_id": str(new_record_id)})

    recent_collection_refreshed = 0
    if not args.dry_run:
        skipped_ids = [str(item["record_id"]) for item in plan["skip"]]
        for start in range(0, len(skipped_ids), 200):
            batch_ids = skipped_ids[start:start + 200]
            run_lark(
                args.lark_cli,
                [
                    "base", "+record-batch-update", "--base-token", base_token,
                    "--table-id", args.table_id,
                    "--json", json.dumps({
                        "record_id_list": batch_ids,
                        "patch": {"最近采集时间": captured_at},
                    }, ensure_ascii=False),
                    "--format", "json", "--as", as_identity,
                ],
            )
            recent_collection_refreshed += len(batch_ids)

    for item in plan["skip"]:
        actions.append({"work_id": item["work_id"], "action": "skipped", "record_id": item["record_id"]})

    verified = 0
    if not args.dry_run:
        written = plan["create"] + plan["update"]
        written_ids = {item["work_id"]: record_ids[item["work_id"]] for item in written}
        expected = {
            item["work_id"]: {name: item["patch"].get(name) for name in CORE_VERIFY_FIELDS}
            for item in written
        }
        verified = verify_written_records(
            args.lark_cli, base_token, args.table_id, written_ids, expected, as_identity,
        )

    return {
        "input_count": len(works),
        "existing_before": existing_before,
        "updated": updated,
        "created": created,
        "skipped": skipped,
        "recent_collection_refreshed": recent_collection_refreshed,
        "verified": verified,
        "dry_run": args.dry_run,
        "actions": actions,
        "record_ids": record_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--works-file", default=str(DEFAULT_WORKS_FILE))
    parser.add_argument("--base-token")
    parser.add_argument("--table-id", required=True)
    parser.add_argument("--lark-cli", default=str(DEFAULT_LARK_CLI))
    parser.add_argument("--as", dest="as_identity", default="user", choices=["user", "bot"])
    parser.add_argument("--skip-schema-validation", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = sync(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
