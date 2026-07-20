#!/usr/bin/env python3
"""Read selected fields from one creator record in the Feishu Base table."""

from __future__ import annotations

import argparse
import json
import sys

from sync_creator_backup_mapping_to_feishu import (
    DEFAULT_LARK_CLI,
    MappingSyncError,
    get_record_fields,
    load_base_token,
    search_creator_record,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读取飞书达人基础信息表中的指定字段")
    parser.add_argument("--base-token")
    parser.add_argument("--table-id", required=True)
    parser.add_argument("--match-field", required=True)
    parser.add_argument("--match-value", required=True)
    parser.add_argument("--field", action="append", required=True)
    parser.add_argument("--lark-cli", default=str(DEFAULT_LARK_CLI))
    parser.add_argument("--as", dest="as_identity", default="user", choices=["user", "bot"])
    args = parser.parse_args(argv)

    try:
        token = load_base_token(args.base_token)
        record_id = search_creator_record(
            args.lark_cli,
            token,
            args.table_id,
            args.match_field,
            args.match_value,
            args.as_identity,
        )
        existing = get_record_fields(
            args.lark_cli,
            token,
            args.table_id,
            record_id,
            args.as_identity,
        )
        fields = {name: existing.get(name) for name in args.field if name in existing}
        print(json.dumps({"record_id": record_id, "fields": fields}, ensure_ascii=False))
        return 0
    except MappingSyncError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
