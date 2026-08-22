#!/usr/bin/env python3
"""达人基础信息表 —— 字段归属与写入规则（唯一权威定义 + 校验器）。

写入方（stage）：
- onboard : 新达人接入（check_and_onboard_new_creators.py --apply）
- profile : 刷主页/采集资料（collect_douyin_creator_profile.py -> --sync-profiles）
- backup  : 备份文案（IMA / 夸克 / Obsidian 三个备份脚本）
- manual  : 用户手动维护（达人类型）
- system  : 飞书系统自动（创建时间）

设计目标（用户硬性要求）：三个写入期回写飞书前，必须调用对应
validate_<stage>_patch()，确保「不写少、不写漏、不写错」。

- 不写错：字段值符合类型/取值/跨字段一致性（如 抖音UID 不得等于 SecUID）。
- 不写漏：本期负责的字段必须齐全（缺失则告警列出）。
校验器返回 (clean_patch, warnings)；warnings 非空时由调用方打印，但写入永不中断。
"""

from __future__ import annotations

import json
import re
from typing import Any

ONBOARD, PROFILE, BACKUP, MANUAL, SYSTEM = "onboard", "profile", "backup", "manual", "system"

# kind 取值：
#   "str"                非空字符串
#   "int"                整数（可为 0）
#   "url"                以 http(s):// 开头
#   "datetime"           YYYY-MM-DD HH:MM:SS
#   "json"               dict / list（主页原始数据等）
#   "enum:a,b,c"         取值须在枚举内
#   "numeric_or_empty"   空 或 纯数字（用于 抖音UID：无可靠来源时留空，绝不填 SecUID）
#   None                 不校验

FIELD_RULES: dict[str, dict[str, Any]] = {
    # ===================== 接入期写入 =====================
    "所属平台":   dict(owner=ONBOARD, required=True,  kind="enum:抖音,TikTok", note="按主页域名推断"),
    "SecUID":     dict(owner=ONBOARD, required=True,  kind="str", note="URL 中 user/ 后截到 ? 前的 base64 令牌"),
    "最近检查时间": dict(owner=ONBOARD, required=True, kind="datetime", note="也由 profile 期更新"),
    "主页采集状态": dict(owner=ONBOARD, required=True, kind="enum:已接入监控,正常,异常", note="也由 profile 期更新"),
    "达人昵称":   dict(owner=ONBOARD, required=False, kind="str",
                       note="仅当飞书留空时由接入补全显示名；profile 期也会写"),
    "作品表名称":  dict(owner=ONBOARD, required=True,  kind="str"),
    "作品表ID":   dict(owner=ONBOARD, required=True,  kind="str"),
    "作品表链接":  dict(owner=ONBOARD, required=False, kind="url"),

    # ===================== 采集期写入 =====================
    "达人主页地址": dict(owner=PROFILE, required=True,  kind="url"),
    "抖音UID":    dict(owner=PROFILE, required=False, kind="numeric_or_empty",
                       note="纯数字账号ID；采集期从抖音 aweme/post 接口 author.uid 抓取（真实，非污染假账号），"
                            "用 sec_uid 校验是本人；非空必为纯数字且与 SecUID 不同；无来源时留空，绝不填成 SecUID"),
    "账号ID":     dict(owner=PROFILE, required=True,  kind="str", note="抖音号 handle，如 zaoweilai8（不是数字 UID）"),
    "IP属地":     dict(owner=PROFILE, required=False, kind="str",
                       note="可选；抓到才更新，缺失时不清空历史值，也不影响采集成功"),
    "所在地区":   dict(owner=PROFILE, required=False, kind="str",
                       note="可选；抓到才更新，缺失时不清空历史值，也不影响采集成功"),
    "性别":       dict(owner=PROFILE, required=False, kind="enum:男,女,未知",
                       note="抖音主页常不暴露性别，非必填；抓到才填，无则不写（不覆盖历史值）"),
    "关注数":     dict(owner=PROFILE, required=True,  kind="int"),
    "粉丝数":     dict(owner=PROFILE, required=True,  kind="int"),
    "获赞数":     dict(owner=PROFILE, required=True,  kind="int"),
    "作品数":     dict(owner=PROFILE, required=True,  kind="int"),
    "账号简介":   dict(owner=PROFILE, required=False, kind="str"),
    "头像URL":    dict(owner=PROFILE, required=False, kind="url"),
    "最近发稿时间": dict(owner=PROFILE, required=True, kind="datetime", note="由作品文件最大 create_time 推算"),
    "账号状态":   dict(owner=PROFILE, required=True,  kind="enum:正常,异常"),
    "主页原始数据": dict(owner=PROFILE, required=False, kind="json"),

    # ===================== 备份期写入（三组）=====================
    "IMA知识库名称":  dict(owner=BACKUP, required=True, kind="str", group="IMA"),
    "IMA知识库ID":    dict(owner=BACKUP, required=True, kind="str", group="IMA"),
    "IMA文件夹名称":  dict(owner=BACKUP, required=True, kind="str", group="IMA"),
    "IMA文件夹ID":    dict(owner=BACKUP, required=True, kind="str", group="IMA"),
    "IMA同步状态":    dict(owner=BACKUP, required=True, kind="enum:已映射,已上传,失败", group="IMA"),
    "夸克文件夹名称": dict(owner=BACKUP, required=True, kind="str", group="夸克"),
    "夸克文件夹ID":   dict(owner=BACKUP, required=True, kind="str", group="夸克"),
    "夸克文件夹路径": dict(owner=BACKUP, required=True, kind="str", group="夸克"),
    "夸克同步状态":   dict(owner=BACKUP, required=True, kind="enum:已映射,已上传,失败", group="夸克"),
    "Obsidian文件夹名称": dict(owner=BACKUP, required=True, kind="str", group="Obsidian"),
    "Obsidian文件夹路径": dict(owner=BACKUP, required=True, kind="str", group="Obsidian"),
    "Obsidian同步状态":  dict(owner=BACKUP, required=True, kind="enum:已映射,已写入,失败", group="Obsidian"),

    # ===================== 手动维护 =====================
    "达人类型":   dict(owner=MANUAL, required=False, kind="str", note="用户手动填；用于选内容总结模板"),

    # ===================== 系统 / 不抓 =====================
    "创建时间":   dict(owner=SYSTEM, required=False, kind=None),
    "认证信息":   dict(owner=None, required=False, kind=None, note="流水线不抓，长期为空"),
}

ONBOARD_FIELDS = [f for f, r in FIELD_RULES.items() if r["owner"] == ONBOARD]
PROFILE_FIELDS = [f for f, r in FIELD_RULES.items() if r["owner"] == PROFILE]
BACKUP_FIELDS = [f for f, r in FIELD_RULES.items() if r["owner"] == BACKUP]

BACKUP_GROUPS: dict[str, list[str]] = {}
for _f, _r in FIELD_RULES.items():
    _g = _r.get("group")
    if _g:
        BACKUP_GROUPS.setdefault(_g, []).append(_f)

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _is_empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    if isinstance(v, (list, dict)) and len(v) == 0:
        return True
    return False


def _check_kind(kind: str | None, value: Any) -> str | None:
    """返回错误信息字符串；合法则返回 None。"""
    if kind is None:
        return None
    if kind == "str":
        if not isinstance(value, str) or value.strip() == "":
            return "应为非空字符串"
        return None
    if kind == "int":
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return "应为整数"
        if iv < 0:
            return "不应为负数"
        return None
    if kind == "url":
        if not isinstance(value, str) or not value.strip().lower().startswith(("http://", "https://")):
            return "应为 http(s) 链接"
        return None
    if kind == "datetime":
        if not isinstance(value, str) or not _DATETIME_RE.match(value.strip()):
            return "应为 YYYY-MM-DD HH:MM:SS"
        return None
    if kind == "json":
        if isinstance(value, (dict, list)):
            return None
        if isinstance(value, str):
            # 飞书文本字段只能存字符串，采集方常将 dict/list 预先 json.dumps 成字符串。
            # 只要能被解析为 dict/list 即视为合法；否则才不合法（避免把明文误当 JSON 丢弃）。
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return "应为 JSON 对象/数组（或合法 JSON 字符串）"
            if isinstance(parsed, (dict, list)):
                return None
            return "应为 JSON 对象/数组"
        return "应为 JSON 对象/数组"
    if kind == "numeric_or_empty":
        if _is_empty(value):
            return None
        if not str(value).strip().isdigit():
            return "非空时必须是纯数字"
        return None
    if kind.startswith("enum:"):
        allowed = kind.split(":", 1)[1].split(",")
        if str(value).strip() not in allowed:
            return f"取值须为 {allowed} 之一"
        return None
    return None


def _rule(fname: str) -> dict[str, Any]:
    return FIELD_RULES.get(fname, {})


# --------------------------------------------------------------------------
# 接入期校验
# --------------------------------------------------------------------------
def validate_onboard_patch(patch: dict[str, Any], sec_uid: str = "") -> tuple[dict[str, Any], list[str]]:
    """新达人接入回写前的校验。会修正：抖音UID 误填成 SecUID / 非数字 -> 留空。"""
    warnings: list[str] = []
    patch = dict(patch)
    sec_uid = str(sec_uid or patch.get("SecUID") or "").strip()

    # 跨字段正确性：抖音UID 不得等于 SecUID，且须为数字
    duid = patch.get("抖音UID")
    if not _is_empty(duid):
        duid_s = str(duid).strip()
        if duid_s == sec_uid:
            warnings.append("抖音UID 误填成 SecUID（同值），已留空。")
            patch.pop("抖音UID", None)
        elif not duid_s.isdigit():
            warnings.append(f"抖音UID 『{duid_s[:16]}…』非纯数字，无法确认来源，已留空。")
            patch.pop("抖音UID", None)

    # 完整性 + 类型
    for fname in ONBOARD_FIELDS:
        rule = _rule(fname)
        val = patch.get(fname)
        if rule.get("required") and _is_empty(val):
            warnings.append(f"接入期必填字段缺失：{fname}")
            continue
        if _is_empty(val):
            continue
        err = _check_kind(rule.get("kind"), val)
        if err:
            warnings.append(f"{fname} 值不合法（{err}）：{val!r}")
    return patch, warnings


# --------------------------------------------------------------------------
# 采集期校验
# --------------------------------------------------------------------------
def validate_profile_patch(patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate profile fields and remove invalid values to preserve remote data."""
    warnings: list[str] = []
    patch = dict(patch)
    for fname in PROFILE_FIELDS:
        rule = _rule(fname)
        val = patch.get(fname)
        if rule.get("required") and _is_empty(val):
            warnings.append(f"采集期必填字段缺失：{fname}")
            continue
        if _is_empty(val):
            continue
        err = _check_kind(rule.get("kind"), val)
        if err:
            warnings.append(f"{fname} 值不合法（{err}）：{val!r}")
            patch.pop(fname, None)
    # 跨字段：账号ID 不应是 base64 令牌（形如 MS4wLjAB...）
    aid = patch.get("账号ID")
    if isinstance(aid, str) and aid.strip().startswith("MS4") and len(aid.strip()) > 20:
        warnings.append(f"账号ID 疑似被填成 SecUID 令牌：{aid[:16]}…，应是抖音号(handle)。")
        patch.pop("账号ID", None)
    # 跨字段/类型：抖音UID 不得等于 SecUID（防御污染假账号或旧错值），且必须为纯数字。
    duid = patch.get("抖音UID")
    if duid is not None:
        duid_s = str(duid).strip()
        sec_uid = str(patch.get("SecUID") or "").strip()
        if duid_s == sec_uid:
            warnings.append("抖音UID 与 SecUID 同值（疑似污染假账号/旧错值），已移除。")
            patch.pop("抖音UID", None)
        elif not duid_s.isdigit():
            warnings.append(f"抖音UID 『{duid_s[:16]}…』非纯数字，已移除。")
            patch.pop("抖音UID", None)
    return patch, warnings


# --------------------------------------------------------------------------
# 备份期校验
# --------------------------------------------------------------------------
def validate_backup_patch(patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """备份映射回写前的校验：同一平台组的字段必须齐全。"""
    warnings: list[str] = []
    patch = dict(patch)
    present_groups: set[str] = set()
    for fname, val in patch.items():
        if _is_empty(val):
            continue
        g = _rule(fname).get("group")
        if g:
            present_groups.add(g)
    for g in present_groups:
        group_fields = BACKUP_GROUPS.get(g, [])
        missing = [f for f in group_fields if _is_empty(patch.get(f))]
        if missing:
            warnings.append(f"备份组『{g}』字段不全，缺少：{', '.join(missing)}")
        for f in group_fields:
            val = patch.get(f)
            if _is_empty(val):
                continue
            err = _check_kind(_rule(f).get("kind"), val)
            if err:
                warnings.append(f"{f} 值不合法（{err}）：{val!r}")
    return patch, warnings


if __name__ == "__main__":
    # 自测：打印字段归属概览
    for stage, fields in (
        ("onboard", ONBOARD_FIELDS),
        ("profile", PROFILE_FIELDS),
        ("backup", BACKUP_FIELDS),
    ):
        print(f"[{stage}] {len(fields)} 字段: {', '.join(fields)}")
    print(f"[backup groups] { {k: len(v) for k, v in BACKUP_GROUPS.items()} }")
    print(f"[manual] 达人类型  [system] 创建时间  [不抓] 认证信息")
