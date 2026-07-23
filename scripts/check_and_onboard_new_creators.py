#!/usr/bin/env python3
"""Detect new creators in 达人基础信息表 and onboard them.

每次跑项目前调用本脚本即可自动发现飞书《达人基础信息表》里新增、但还没有接入
pipeline.json 配置的达人，并按以下规则处理：

1. 加入日常更新的数据里面：向 pipeline.json 的 creators 数组追加该达人配置，
   使后续每日运行自动采集其作品。
2. 把信息补充完全（用户默认只往表里贴一个主页链接，其余由系统补全）：
   - 若其记录的「作品表ID」为空，则新建专属作品表（字段沿用 work_table_schema 规范），
     并把「作品表名称 / 作品表ID / 作品表链接」回写到达人基础信息表；
   - 写回可推导字段：所属平台、SecUID、最近检查时间、主页采集状态、
     达人昵称（若用户留空）。抖音UID 当前无可靠来源，接入期**故意留空**，绝不填成 SecUID；
   - 主流水线采集后，若本地存在 runtime/profile-<key>-update.json（含粉丝数/获赞数/
     作品数/账号简介等），再用 --sync-profiles 把统计字段回写飞书，补全基础信息。

调用方式：
- 默认（report-only）：只报告发现了哪些新增达人、各自缺什么，不写任何东西。
- --apply：真正建表、改配置、回写信息。日常自动化用 --apply --no-collect
  （只接入+补全信息，采集交给随后的主流水线，避免重复抓）。
- --sync-profiles：把现有达人（含本次新接入）本地主页资料回写飞书统计字段。

注意：当前项目流水线并未自动产出 profile-<key>-update.json（仅早期有遗留文件），
因此统计字段回写（--sync-profiles）在资料产出前为空操作；资料采集接通后自动生效。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 字段归属/规则校验（单一权威来源：scripts/creator_table_fields.py）。
# 模块缺失时降级为无操作，绝不阻断主流程。
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from creator_table_fields import validate_onboard_patch, validate_profile_patch
except Exception:  # noqa: BLE001
    def validate_onboard_patch(patch, sec_uid=""):
        return patch, []

    def validate_profile_patch(patch):
        return patch, []

PROJECT_DIR = Path(__file__).resolve().parents[1]
PIPELINE_CONFIG = PROJECT_DIR / "local" / "pipeline.json"
DEFAULT_LARK_CLI = PROJECT_DIR / "tools" / "lark-cli" / "lark-cli.exe"
FEISHU_IDS = PROJECT_DIR / "local" / "feishu-ids.md"

BEIJING_TZ = timezone(timedelta(hours=8))


def _load_sync_module():
    spec = importlib.util.spec_from_file_location(
        "sync_creator_backup_mapping_to_feishu",
        PROJECT_DIR / "scripts" / "sync_creator_backup_mapping_to_feishu.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SYNC = _load_sync_module()
run_lark = SYNC.run_lark
load_base_token = SYNC.load_base_token


def _load_schema_module():
    spec = importlib.util.spec_from_file_location(
        "work_table_schema", PROJECT_DIR / "scripts" / "work_table_schema.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CANONICAL_WORK_FIELDS = _load_schema_module().CANONICAL_WORK_FIELDS

# 用来比对的达人基础信息表字段
HOMEPAGE_FIELD = "达人主页地址"
NICKNAME_FIELD = "达人昵称"
WORKS_TABLE_ID_FIELD = "作品表ID"
WORKS_TABLE_NAME_FIELD = "作品表名称"
WORKS_TABLE_LINK_FIELD = "作品表链接"
TYPE_FIELD = "达人类型"


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def beijing_now() -> str:
    return datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def sec_uid_from_url(url: Any) -> str:
    if not url:
        return ""
    m = re.search(r"user/([A-Za-z0-9_-]+)", str(url))
    if m:
        return m.group(1)
    # 兜底：URL 最后一段
    seg = str(url).rsplit("/", 1)[-1].split("?", 1)[0].strip()
    return seg


def platform_from_url(url: Any) -> str:
    text = str(url or "").lower()
    if "douyin.com" in text:
        return "抖音"
    if "tiktok.com" in text:
        return "TikTok"
    return "未知"


def plain_url(value: Any) -> str:
    """Feishu 链接字段可能以 [text](url) 或 {link:..} 形式返回，归一化为纯 URL。"""
    if isinstance(value, dict):
        return str(value.get("link") or value.get("url") or value.get("text") or "").strip()
    text = str(value or "").strip()
    m = re.search(r"\]\((https?://[^)]+)\)", text)
    if m:
        return m.group(1)
    return text


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# 读取配置
# --------------------------------------------------------------------------
def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_config(path: Path, config: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


# --------------------------------------------------------------------------
# 主页地址归一化：提取 user/<SecUID> 这一段作为稳定标识
# --------------------------------------------------------------------------
def normalize_homepage(url: Any) -> str:
    if not url:
        return ""
    text = str(url).strip()
    if not text:
        return ""
    m = re.search(r"user/([A-Za-z0-9_-]+)", text)
    if m:
        return "user/" + m.group(1)
    text = re.sub(r"[?#].*$", "", text).rstrip("/")
    return text.lower()


def configured_homepages(config: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for creator in config.get("creators", []):
        url = creator.get("creator_url")
        if url:
            result.add(normalize_homepage(url))
        if creator.get("feishu_match_field") and creator.get("feishu_match_value"):
            result.add(str(creator["feishu_match_value"]).strip())
    return result


# --------------------------------------------------------------------------
# 列出达人基础信息表记录
# --------------------------------------------------------------------------
def list_creator_records(
    cli: str, base_token: str, table_id: str, as_identity: str
) -> list[dict[str, Any]]:
    """返回 [{record_id, fields: {字段名: 值}}]"""
    proj_fields = [
        HOMEPAGE_FIELD,
        NICKNAME_FIELD,
        WORKS_TABLE_ID_FIELD,
        WORKS_TABLE_NAME_FIELD,
        TYPE_FIELD,
    ]
    records: list[dict[str, Any]] = []
    offset = 0
    while True:
        cmd = [
            "base", "+record-list",
            "--base-token", base_token,
            "--table-id", table_id,
            "--offset", str(offset),
            "--limit", "200",
            "--format", "json",
            "--as", as_identity,
        ]
        for f in proj_fields:
            cmd += ["--field-id", f]
        payload = run_lark(cli, cmd)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = data.get("data") if isinstance(data.get("data"), list) else []
        record_ids = (
            data.get("record_id_list")
            if isinstance(data.get("record_id_list"), list)
            else []
        )
        for row, record_id in zip(rows, record_ids):
            if not isinstance(record_id, str) or not record_id.startswith("rec"):
                continue
            fields: dict[str, Any] = {}
            if isinstance(row, list):
                for name, value in zip(proj_fields, row):
                    fields[name] = value
            elif isinstance(row, dict):
                fields.update(row)
            records.append({"record_id": record_id, "fields": fields})
        if not data.get("has_more"):
            break
        offset += 200
    return records


def find_record_by_homepage(
    records: list[dict[str, Any]], homepage: str
) -> str | None:
    target = normalize_homepage(homepage)
    for rec in records:
        if normalize_homepage(rec["fields"].get(HOMEPAGE_FIELD)) == target:
            return rec["record_id"]
    return None


# --------------------------------------------------------------------------
# 检测新增达人
# --------------------------------------------------------------------------
def detect_new_creators(
    config: dict[str, Any], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    known = configured_homepages(config)
    new: list[dict[str, Any]] = []
    for rec in records:
        fields = rec["fields"]
        homepage = normalize_homepage(fields.get(HOMEPAGE_FIELD))
        nickname = str(fields.get(NICKNAME_FIELD) or "").strip()
        if not homepage and not nickname:
            continue  # 没有主页也没有昵称，无法接入
        if homepage and homepage in known:
            continue
        if nickname and nickname in known:
            continue
        new.append(rec)
    return new


# --------------------------------------------------------------------------
# 派生 creator key / 显示名
# --------------------------------------------------------------------------
def derive_key(nickname: str, homepage: str, existing_keys: set[str]) -> str:
    import hashlib

    slug = re.sub(r"[^A-Za-z0-9_]", "", nickname.lower())
    if len(slug) < 2:
        # 链接型达人（昵称可能为空或中文太短）：用 SecUID 哈希保证稳定且唯一
        seed = sec_uid_from_url(homepage) or homepage or nickname
        slug = "c" + hashlib.md5(seed.encode("utf-8")).hexdigest()[:6]
    if slug not in existing_keys:
        return slug
    i = 2
    while f"{slug}{i}" in existing_keys:
        i += 1
    return f"{slug}{i}"


# --------------------------------------------------------------------------
# 建作品表
# --------------------------------------------------------------------------
def find_existing_table(
    cli: str, base_token: str, name: str, as_identity: str
) -> str | None:
    """List tables in the base and return the id of one whose name matches."""
    payload = run_lark(
        cli,
        [
            "base", "+table-list",
            "--base-token", base_token,
            "--limit", "100",
            "--format", "json",
            "--as", as_identity,
        ],
    )
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tables = data.get("tables") if isinstance(data.get("tables"), list) else []
    for t in tables:
        if isinstance(t, dict) and str(t.get("name")) == name:
            tid = t.get("id") or t.get("table_id")
            if isinstance(tid, str) and tid.startswith("tbl"):
                return tid
    return None


def create_works_table(
    cli: str, base_token: str, name: str, as_identity: str
) -> str:
    # 幂等：同名表已存在则直接复用，避免产生孤儿表。
    existing = find_existing_table(cli, base_token, name, as_identity)
    if existing:
        return existing
    payload = run_lark(
        cli,
        [
            "base", "+table-create",
            "--base-token", base_token,
            "--name", name,
            "--fields", json.dumps(CANONICAL_WORK_FIELDS, ensure_ascii=False),
            "--format", "json",
            "--as", as_identity,
        ],
    )
    for path in (
        ("data", "table", "id"),
        ("data", "table_id"),
        ("data", "id"),
        ("table_id",),
        ("id",),
    ):
        node: Any = payload
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and isinstance(node, str) and node.startswith("tbl"):
            return node
    raise RuntimeError(f"建表后无法解析新表 ID：{json.dumps(payload, ensure_ascii=False)[:300]}")


def wiki_link() -> str:
    if FEISHU_IDS.exists():
        m = re.search(r"wiki/base 链接:\s*(\S+)", FEISHU_IDS.read_text(encoding="utf-8-sig"))
        if m:
            return m.group(1).rstrip("/")
    return ""


# --------------------------------------------------------------------------
# 回写字段到达人基础信息表（通用 upsert）
# --------------------------------------------------------------------------
def upsert_base_info_fields(
    cli: str, base_token: str, table_id: str, record_id: str,
    patch: dict[str, Any], as_identity: str,
) -> None:
    patch = {k: v for k, v in patch.items() if v not in (None, "", [], {})}
    if not patch:
        return
    run_lark(
        cli,
        [
            "base", "+record-upsert",
            "--base-token", base_token,
            "--table-id", table_id,
            "--record-id", record_id,
            "--json", json.dumps(patch, ensure_ascii=False),
            "--format", "json",
            "--as", as_identity,
        ],
    )


# --------------------------------------------------------------------------
# 把本地主页资料回写飞书统计字段
# --------------------------------------------------------------------------
PROFILE_FIELDS_TO_SYNC = [
    "达人昵称", "达人主页地址", "所属平台", "账号ID", "抖音UID", "SecUID", "IP属地",
    "所在地区", "性别", "关注数", "粉丝数", "获赞数", "作品数", "账号简介",
    "头像URL", "最近发稿时间", "最近检查时间", "主页采集状态", "账号状态",
    "主页原始数据",
]


def sanitize_profile_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        return value
    if isinstance(value, (dict, list)):
        if not value:
            return None
        return json.dumps(value, ensure_ascii=False)
    return value


def _compute_last_post_time(key: str, creator: dict[str, Any] | None = None) -> str | None:
    """最近发稿时间 = 该达人作品文件里最大的发布时间（作品派生指标，由作品推算）。"""
    wf = (creator or {}).get("works_file")
    if not wf:
        try:
            cfg = load_config(PIPELINE_CONFIG)
        except Exception:
            return None
        for item in cfg.get("creators", []):
            if creator_key(item) == key:
                wf = item.get("works_file")
                break
    if not wf:
        return None
    p = PROJECT_DIR / wf
    if not p.exists():
        return None
    try:
        d = read_json(p)
    except Exception:
        return None
    works = d.get("works") if isinstance(d, dict) else None
    if not works:
        return None
    ts_list = []
    for w in works:
        ct = w.get("create_time") or w.get("createTime") or w.get("publish_time")
        if ct:
            try:
                ts_list.append(int(ct))
            except Exception:
                pass
    if not ts_list:
        return None
    try:
        from datetime import datetime, timezone, timedelta
        bj = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(max(ts_list), bj).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def sync_profile_to_feishu(
    cli: str, base_token: str, table_id: str, as_identity: str,
    key: str, record_id: str, creator: dict[str, Any] | None = None,
) -> bool:
    profile_path = creator_profile_path(creator or {"key": key})
    data = read_json(profile_path)
    if not isinstance(data, dict):
        data = {}
    patch = {
        f: sanitize_profile_value(data.get(f))
        for f in PROFILE_FIELDS_TO_SYNC
    }
    # 最近发稿时间：由作品文件最大发布时间推算（profile 文件里没有此字段）
    last_post = _compute_last_post_time(key, creator)
    if last_post:
        patch["最近发稿时间"] = last_post
    patch = {k: v for k, v in patch.items() if v is not None}
    if not patch:
        return False
    patch, profile_warn = validate_profile_patch(patch)
    for w in profile_warn:
        print(f"  [采集校验] 警告（{key}）：{w}", file=sys.stderr)
    upsert_base_info_fields(cli, base_token, table_id, record_id, patch, as_identity)
    return True


# --------------------------------------------------------------------------
# 主流程：report / apply / sync-profiles
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查并接入飞书达人基础信息表中的新增达人")
    parser.add_argument("--config", type=Path, default=PIPELINE_CONFIG)
    parser.add_argument("--base-token")
    parser.add_argument("--table-id", default=None, help="达人基础信息表 ID，缺省读 config.feishu.creator_table_id")
    parser.add_argument("--lark-cli", default=str(DEFAULT_LARK_CLI))
    parser.add_argument("--as", dest="as_identity", default=None, choices=["user", "bot"])
    parser.add_argument("--records-file", type=Path, default=None,
                        help="离线测试用：直接读 JSON 记录列表，而不调用 lark-cli")
    parser.add_argument("--apply", action="store_true", help="真正建表、改配置、回写信息（默认仅报告）")
    parser.add_argument("--no-collect", action="store_true", help="--apply 时只接入+补全信息，不触发采集（采集交给随后的主流水线）")
    parser.add_argument("--creator", action="append", default=[], help="只处理指定达人 key，可重复")
    parser.add_argument("--sync-profiles", action="store_true", help="把现有达人本地主页资料回写飞书统计字段")
    parser.add_argument("--collect-profiles", action="store_true",
                        help="采集现有达人主页资料(粉丝数/获赞数/作品数/账号名等)到 runtime/profile-<key>-update.json；"
                             "可与 --sync-profiles 连用，先采集再回写飞书")
    parser.add_argument("--pipeline-script", type=Path,
                        default=PROJECT_DIR / "scripts" / "run_creator_pipeline.py")
    parser.add_argument("--python", default=sys.executable, help="运行采集流水线用的 python")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    table_id = args.table_id or config.get("feishu", {}).get("creator_table_id")
    if not table_id:
        print("错误：未配置 feishu.creator_table_id，也无法从参数获取。", file=sys.stderr)
        return 1
    as_identity = args.as_identity or config.get("feishu", {}).get("as_identity", "user")

    # ---- 模式：先采集主页资料（可选），再回写 ----
    if args.collect_profiles:
        rc = run_collect_profiles(args, config)
        if rc != 0 or not args.sync_profiles:
            return rc
    # ---- 模式 1：回写统计字段 ----
    if args.sync_profiles:
        return run_sync_profiles(args, config, table_id, as_identity)

    # ---- 取记录 ----
    if args.records_file:
        raw = json.loads(args.records_file.read_text(encoding="utf-8-sig"))
        records = [
            {"record_id": r.get("record_id", f"rec{i}"), "fields": r.get("fields", r)}
            for i, r in enumerate(raw)
        ]
    else:
        token = load_base_token(args.base_token)
        records = list_creator_records(args.lark_cli, token, table_id, as_identity)

    # ---- 检测新增 ----
    new = detect_new_creators(config, records)
    print(f"达人基础信息表记录数: {len(records)}，已接入配置: {len(config.get('creators', []))}，新增达人: {len(new)}")

    if not new:
        print("未发现新增达人，无需接入。")
        return 0

    existing_keys = {str(c.get("key")) for c in config.get("creators", [])}
    exit_code = 0
    for rec in new:
        fields = rec["fields"]
        homepage_url = plain_url(fields.get(HOMEPAGE_FIELD))
        nickname = str(fields.get(NICKNAME_FIELD) or "").strip()
        if args.apply and not nickname:
            exit_code = 1
            print(
                f"  错误：记录 {rec['record_id']} 缺少达人昵称，已跳过接入，避免用哈希占位名创建作品表。",
                file=sys.stderr,
            )
            continue
        homepage = normalize_homepage(homepage_url)
        existing_works_tbl = str(fields.get(WORKS_TABLE_ID_FIELD) or "").strip()
        key = derive_key(nickname, homepage_url, existing_keys)
        display_name = nickname or key
        sec_uid = sec_uid_from_url(homepage_url)
        platform = platform_from_url(homepage_url)

        plan = {
            "record_id": rec["record_id"],
            "nickname": nickname or "(空)",
            "homepage": homepage,
            "derived_key": key,
            "existing_works_table": existing_works_tbl or None,
            "will_create_table": not existing_works_tbl,
            "creator_type": str(fields.get(TYPE_FIELD) or "").strip(),
            "sec_uid": sec_uid,
            "platform": platform,
        }

        if not args.apply:
            print(json.dumps({"action": "detected", **plan}, ensure_ascii=False, indent=2))
            continue

        # ---- apply：接入 + 补全信息 ----
        print(f"[apply] 接入达人: {display_name} (key={key})")
        new_tbl = existing_works_tbl
        patch: dict[str, Any] = {
            "所属平台": platform,
            "SecUID": sec_uid,
            # 注意：抖音UID 不在此写入（无可靠数字来源），由 validate_onboard_patch 兜底防止误填成 SecUID
            "最近检查时间": beijing_now(),
            "主页采集状态": "已接入监控",
        }
        if not nickname:
            patch["达人昵称"] = display_name  # 用户留空则补全显示名

        if not new_tbl:
            new_tbl = create_works_table(args.lark_cli, load_base_token(args.base_token), display_name, as_identity)
            link = f"{wiki_link()}?table={new_tbl}" if wiki_link() else ""
            patch[WORKS_TABLE_NAME_FIELD] = display_name
            patch[WORKS_TABLE_ID_FIELD] = new_tbl
            if link:
                patch[WORKS_TABLE_LINK_FIELD] = link
            print(f"  已建作品表 {new_tbl}")
        else:
            print(f"  复用已有作品表 {new_tbl}")
            patch[WORKS_TABLE_NAME_FIELD] = display_name
            link = f"{wiki_link()}?table={new_tbl}" if wiki_link() else ""
            if link:
                patch[WORKS_TABLE_LINK_FIELD] = link

        token = load_base_token(args.base_token)
        patch, onboard_warn = validate_onboard_patch(patch, sec_uid)
        for w in onboard_warn:
            print(f"  [接入校验] 警告：{w}", file=sys.stderr)
        upsert_base_info_fields(args.lark_cli, token, table_id, rec["record_id"], patch, as_identity)
        print(f"  已回写基础信息表字段: {', '.join(patch.keys())}")

        entry = {
            "key": key,
            "enabled": True,
            "creator_url": homepage_url,
            "creator_name": display_name,
            "creator_dir_name": display_name,
            "works_table_id": new_tbl,
            "works_file": f"runtime/{key}-works-from-mediacrawler.json",
            "profile_file": f"runtime/profile-{key}-update.json",
            "media_output_dir": f"runtime/mediacrawler-output-{key}",
            "correction_domain": "douyin_shop_ads",
        }
        config.setdefault("creators", []).append(entry)
        existing_keys.add(key)
        save_config(args.config, config)
        print(f"  已写入 pipeline.json creators（key={key}）")

        if args.no_collect:
            print(f"  跳过采集（--no-collect）。随后的主流水线会自动采集 {key}。")
        else:
            print(f"  开始采集并同步 {key} ...")
            code = subprocess.run(
                [args.python, str(args.pipeline_script), "--creator", key],
                cwd=PROJECT_DIR,
            ).returncode
            if code != 0:
                exit_code = 1
                print(f"  警告：{key} 采集返回非零退出码 {code}", file=sys.stderr)
            else:
                # 采集成功则回写统计字段（若有资料产出）
                if sync_profile_to_feishu(args.lark_cli, token, table_id, as_identity, key, rec["record_id"]):
                    print(f"  已回写主页统计字段到飞书。")

    if args.apply:
        save_config(args.config, config)
        print("接入完成。已更新的 pipeline.json 配置已保存。")
    return exit_code


def _resolve_user_data_dir(browser_data: Path, key: str) -> str | None:
    """Locate the logged-in Chromium dir for a creator (mirrors MediaCrawler naming)."""
    exact = browser_data / f"cdp_{key}_dy_user_data_dir"
    if exact.exists():
        return str(exact)
    if browser_data.exists():
        matches = [
            p for p in browser_data.iterdir()
            if p.is_dir() and key in p.name and p.name.endswith("_dy_user_data_dir")
        ]
        if matches:
            return str(matches[0])
    return None


def creator_profile_path(creator: dict[str, Any]) -> Path:
    key = creator_key(creator)
    raw = creator.get("profile_file") or f"runtime/profile-{key}-update.json"
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def run_collect_profiles(args, config) -> int:
    """(Re)produce runtime/profile-<key>-update.json for every enabled creator.

    Uses the SAME logged-in Chromium dir MediaCrawler uses, so no re-login is
    needed. After this runs, a separate --sync-profiles pushes the data to Feishu.
    """
    print("=== 采集现有达人主页资料（粉丝数/获赞数/作品数/账号名等）===")
    collection = config.get("collection", {})
    media_crawler_dir = Path(str(collection.get("media_crawler_python") or "")).resolve().parents[1] \
        if not collection.get("media_crawler_dir") else Path(str(collection.get("media_crawler_dir"))).resolve()
    venv_python = str(collection.get("media_crawler_python") or "")
    collector = PROJECT_DIR / "scripts" / "collect_douyin_creator_profile.py"
    browser_data = media_crawler_dir / "browser_data"
    ok = skipped = failed = reused = 0
    try:
        profile_ttl_seconds = max(0.0, float(collection.get("profile_ttl_hours", 12))) * 3600
    except (TypeError, ValueError):
        profile_ttl_seconds = 12 * 3600
    jobs: list[tuple[str, str, str, Path]] = []
    for creator in config.get("creators", []):
        if not creator.get("enabled", True):
            continue
        key = creator_key(creator)
        homepage = normalize_homepage(creator.get("creator_url"))
        if not homepage:
            print(f"  跳过 {key}：无 creator_url")
            skipped += 1
            continue
        out = creator_profile_path(creator)
        if profile_ttl_seconds and out.is_file():
            try:
                is_fresh = time.time() - out.stat().st_mtime <= profile_ttl_seconds
            except OSError:
                is_fresh = False
            if is_fresh:
                reused += 1
                print(f"  复用 {key}：主页资料仍在 TTL 内")
                continue
        profile_key = str(creator.get("browser_profile_key") or key).strip() or key
        udir = _resolve_user_data_dir(browser_data, profile_key)
        if not udir or not Path(udir).exists():
            print(f"  跳过 {key}：未找到登录态目录（期望 {browser_data / ('cdp_' + profile_key + '_dy_user_data_dir')}）")
            skipped += 1
            continue
        jobs.append((key, homepage, udir, out))

    try:
        workers = max(1, int(collection.get("profile_max_workers", 3)))
    except (TypeError, ValueError):
        workers = 3

    def collect_one(job: tuple[str, str, str, Path]) -> tuple[str, int, Path]:
        key, homepage, udir, out = job
        cmd = [
            venv_python or sys.executable, str(collector),
            "--creator-url", homepage,
            "--user-data-dir", udir,
            "--output", str(out),
        ]
        code = subprocess.run(cmd, cwd=PROJECT_DIR).returncode
        return key, code, out

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs) or 1)) as executor:
        futures = {executor.submit(collect_one, job): job[0] for job in jobs}
        for future in as_completed(futures):
            key = futures[future]
            try:
                _, code, out = future.result()
            except Exception as exc:
                code = 1
                out = PROJECT_DIR / "runtime" / f"profile-{key}-update.json"
                print(f"    警告：{key} 主页采集异常：{exc}", file=sys.stderr)
            if code == 0:
                ok += 1
                print(f"    已生成 {out.name}")
            else:
                failed += 1
                print(f"    警告：{key} 采集返回非零退出码 {code}（可能登录态失效，需重新扫码）", file=sys.stderr)
    print(f"主页资料采集完成：成功 {ok}，复用 {reused}，跳过 {skipped}，失败 {failed}。")
    print("（随后由 --sync-profiles 把资料回写飞书统计字段）")
    return 0 if failed == 0 and skipped == 0 else 1


def run_sync_profiles(args, config, table_id, as_identity) -> int:
    print("=== 回写现有达人主页统计字段到飞书 ===")
    token = load_base_token(args.base_token)
    records = list_creator_records(args.lark_cli, token, table_id, as_identity)
    synced = 0
    skipped = 0
    requested = set(getattr(args, "creator", []) or [])
    for creator in config.get("creators", []):
        if not creator.get("enabled", True):
            continue
        key = creator_key(creator)
        if requested and key not in requested:
            continue
        homepage = normalize_homepage(creator.get("creator_url"))
        record_id = find_record_by_homepage(records, homepage) if homepage else None
        if not record_id:
            # 退而用 SecUID 匹配
            sec = sec_uid_from_url(creator.get("creator_url"))
            for rec in records:
                if normalize_homepage(rec["fields"].get(HOMEPAGE_FIELD)) and \
                   rec["fields"].get("SecUID") == sec:
                    record_id = rec["record_id"]
                    break
        if not record_id:
            print(f"  跳过 {key}：未在基础信息表找到对应记录")
            skipped += 1
            continue
        if sync_profile_to_feishu(args.lark_cli, token, table_id, as_identity, key, record_id, creator):
            synced += 1
            print(f"  已回写 {key} 统计字段")
        else:
            print(f"  跳过 {key}：无本地主页资料")
            skipped += 1
    print(f"统计字段回写完成，成功 {synced} 个，跳过 {skipped} 个。")
    return 1 if skipped else 0


def creator_key(creator: dict[str, Any]) -> str:
    return str(creator.get("key") or creator.get("creator_dir_name") or creator.get("creator_name") or "?").strip()


if __name__ == "__main__":
    raise SystemExit(main())
