#!/usr/bin/env python3
"""Write final transcripts and backup statuses to Feishu Base."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from feishu_transcript_writer import (
    find_record_by_work_id,
    load_base_token,
    redact_value,
    resolve_lark_cli,
    run_lark,
    write_transcript,
)


BEIJING_TZ = timezone(timedelta(hours=8))
BATCH_SIZE = 200


def feishu_datetime(value: str | int | float | None) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    moment = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ) if text else datetime.now(BEIJING_TZ)
    return int(moment.timestamp() * 1000)


def build_patch(args: argparse.Namespace) -> dict[str, Any]:
    patch: dict[str, Any] = {"记录时间": feishu_datetime(args.record_time)}
    if args.ima_status:
        patch["ima状态"] = args.ima_status
    if args.kuake_status:
        patch["夸克网盘状态"] = args.kuake_status
    if args.local_status:
        patch["本地知识库状态"] = args.local_status
    transcript_file = getattr(args, "transcript_file", None)
    if transcript_file:
        transcript = Path(transcript_file).read_text(encoding="utf-8-sig").strip()
        if not transcript:
            raise SystemExit(f"Transcript file is empty: {transcript_file}")
        patch[getattr(args, "transcript_field", "语音转写全文")] = transcript
    return patch


def item_namespace(args: argparse.Namespace, item: dict[str, Any]) -> argparse.Namespace:
    values = vars(args).copy()
    for key in (
        "work_id", "record_id", "ima_status", "kuake_status", "local_status",
        "record_time", "transcript_file", "transcript_field",
    ):
        if item.get(key) not in (None, ""):
            values[key] = item[key]
    return argparse.Namespace(**values)


def write_one(cli: str, base_token: str, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if not args.table_id or not args.work_id:
        raise ValueError("table_id 和 work_id 不能为空")
    record_id = args.record_id or find_record_by_work_id(
        cli, base_token, args.table_id, args.work_id, args.work_id_field, args.as_identity,
    )
    response = write_transcript(
        cli, base_token, args.table_id, record_id, build_patch(args), args.as_identity, args.dry_run,
    )
    return record_id, redact_value(response, base_token)


def permanent_delivery_error(error: str) -> bool:
    text = str(error or "").lower()
    if any(value in text for value in ("timeout", "timed out", "429", "500", "502", "503", "504", "dns", "temporarily unavailable")):
        return False
    return any(value in text for value in (
        "credential", "cookie", "token invalid", "unauthorized", "forbidden", "permission denied",
        "access denied", "missing config", "not configured", "91403", "凭证", "登录态", "权限", "未配置", "配置缺失",
    ))


def write_checkpoint(
    path: Path | None, results: dict[str, dict[str, Any]], batch_id: str = "",
) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"batch_id": batch_id, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_batch(
    cli: str, base_token: str, table_id: str, rows: list[dict[str, Any]],
    as_identity: str, dry_run: bool,
) -> dict[str, Any]:
    body = {"records": [{"record_id": row["record_id"], "fields": row["fields"]} for row in rows]}
    command = [
        "api", "POST",
        f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records/batch_update",
        "--data", "-", "--format", "json", "--as", as_identity,
    ]
    if dry_run:
        command.append("--dry-run")
    return run_lark(cli, command, json.dumps(body, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-token")
    parser.add_argument("--table-id")
    parser.add_argument("--work-id")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--work-id-field", default="抖音作品ID")
    parser.add_argument("--record-id")
    parser.add_argument("--lark-cli")
    parser.add_argument("--as", dest="as_identity", default="user", choices=["user", "bot"])
    parser.add_argument("--ima-status", choices=["待上传", "已上传", "失败", "跳过"])
    parser.add_argument("--kuake-status", choices=["待上传", "已上传", "失败", "跳过"])
    parser.add_argument("--local-status", choices=["待写入", "已写入", "失败", "跳过"])
    parser.add_argument("--record-time")
    parser.add_argument("--transcript-file")
    parser.add_argument("--transcript-field", default="语音转写全文")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cli = resolve_lark_cli(args.lark_cli)
    base_token = load_base_token(args.base_token)
    if args.manifest:
        payload = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        items = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            parser.error("批量写回清单缺少非空 records 数组")
        if not args.table_id:
            args.table_id = str(payload.get("table_id") or "")
        if not args.table_id:
            parser.error("批量写回缺少 table_id")
        result_file_value = str(payload.get("result_file") or "").strip()
        result_file = Path(result_file_value) if result_file_value else None
        batch_id = str(payload.get("batch_id") or "")
        results: dict[str, dict[str, Any]] = {}
        pending: list[dict[str, Any]] = []
        breaker = ""
        for item in items:
            work_id = str(item.get("work_id") or item.get("aweme_id") or "").strip()
            result_key = work_id or f"item_{len(results) + 1}"
            if breaker:
                results[result_key] = {"status": "failed", "error": breaker}
                write_checkpoint(result_file, results, batch_id)
                continue
            try:
                current = item_namespace(args, {**item, "work_id": work_id})
                if not work_id:
                    raise ValueError("清单项缺少 work_id")
                record_id = current.record_id or find_record_by_work_id(
                    cli, base_token, current.table_id, current.work_id, current.work_id_field, current.as_identity,
                )
                pending.append({"work_id": work_id, "record_id": record_id, "fields": build_patch(current)})
            except (Exception, SystemExit) as exc:
                error = str(exc).replace(base_token, "<BASE_TOKEN>")
                results[result_key] = {"status": "failed", "error": error}
                if permanent_delivery_error(error):
                    breaker = error
                write_checkpoint(result_file, results, batch_id)

        for offset in range(0, len(pending), BATCH_SIZE):
            chunk = pending[offset:offset + BATCH_SIZE]
            if breaker:
                for row in chunk:
                    results[row["work_id"]] = {"status": "failed", "error": breaker}
                write_checkpoint(result_file, results, batch_id)
                continue
            try:
                response = redact_value(write_batch(
                    cli, base_token, args.table_id, chunk, args.as_identity, args.dry_run,
                ), base_token)
                for row in chunk:
                    results[row["work_id"]] = {"status": "success", "record_id": row["record_id"], "response": response}
            except (Exception, SystemExit) as exc:
                error = str(exc).replace(base_token, "<BASE_TOKEN>")
                for row in chunk:
                    results[row["work_id"]] = {"status": "failed", "record_id": row["record_id"], "error": error}
                if permanent_delivery_error(error):
                    breaker = error
            write_checkpoint(result_file, results, batch_id)
        print(json.dumps({"results": results}, ensure_ascii=False))
        return 0

    if not args.table_id or not args.work_id:
        parser.error("单条写回需要 --table-id 和 --work-id")
    record_id, response = write_one(cli, base_token, args)
    print(json.dumps({"record_id": record_id, "response": response}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
