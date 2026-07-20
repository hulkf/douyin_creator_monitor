"""Canonical Feishu work-table schema shared by sync and validation tools."""

from __future__ import annotations

from typing import Any


STATUS_OPTIONS = (
    {"name": "待上传", "hue": "Yellow", "lightness": "Lighter"},
    {"name": "已上传", "hue": "Green", "lightness": "Lighter"},
    {"name": "失败", "hue": "Orange", "lightness": "Lighter"},
    {"name": "跳过", "hue": "Purple", "lightness": "Lighter"},
)

CANONICAL_WORK_FIELDS: tuple[dict[str, Any], ...] = (
    {"name": "摘要", "type": "text", "style": {"type": "plain"}},
    {"name": "作品标题", "type": "text", "style": {"type": "plain"}},
    {"name": "封面图URL", "type": "text", "style": {"type": "url"}},
    {"name": "标准化文案", "type": "text", "style": {"type": "plain"}},
    {"name": "是否置顶", "type": "select", "multiple": False, "options": (
        {"name": "是", "hue": "Orange", "lightness": "Lighter"},
        {"name": "否", "hue": "Turquoise", "lightness": "Lighter"},
    )},
    {"name": "收藏数", "type": "number", "style": {"type": "plain", "precision": 0, "percentage": False, "thousands_separator": False}},
    {"name": "本地知识库状态", "type": "select", "multiple": False, "options": (
        {"name": "待写入", "hue": "Yellow", "lightness": "Lighter"},
        {"name": "已写入", "hue": "Green", "lightness": "Lighter"},
        {"name": "失败", "hue": "Orange", "lightness": "Lighter"},
        {"name": "跳过", "hue": "Purple", "lightness": "Lighter"},
    )},
    {"name": "作品链接", "type": "text", "style": {"type": "url"}},
    {"name": "分享数", "type": "number", "style": {"type": "plain", "precision": 0, "percentage": False, "thousands_separator": False}},
    {"name": "抖音作品ID", "type": "text", "style": {"type": "plain"}},
    {"name": "记录时间", "type": "datetime", "style": {"format": "yyyy/MM/dd HH:mm"}},
    {"name": "评论数", "type": "number", "style": {"type": "plain", "precision": 0, "percentage": False, "thousands_separator": False}},
    {"name": "作品类型", "type": "select", "multiple": False, "options": (
        {"name": "视频", "hue": "Yellow", "lightness": "Lighter"},
        {"name": "图文", "hue": "Yellow", "lightness": "Lighter"},
        {"name": "未知", "hue": "Turquoise", "lightness": "Lighter"},
    )},
    {"name": "语音转写全文", "type": "text", "style": {"type": "plain"}},
    {"name": "原始作品数据", "type": "text", "style": {"type": "plain"}},
    {"name": "采集状态", "type": "select", "multiple": False, "options": (
        {"name": "新发现", "hue": "Yellow", "lightness": "Lighter"},
        {"name": "已抓文案", "hue": "Red", "lightness": "Lighter"},
        {"name": "待补详情", "hue": "Wathet", "lightness": "Lighter"},
        {"name": "已完成", "hue": "Blue", "lightness": "Lighter"},
        {"name": "失败", "hue": "Orange", "lightness": "Lighter"},
    )},
    {"name": "夸克网盘状态", "type": "select", "multiple": False, "options": STATUS_OPTIONS},
    {"name": "发布时间", "type": "datetime", "style": {"format": "yyyy/MM/dd HH:mm"}},
    {"name": "ima状态", "type": "select", "multiple": False, "options": STATUS_OPTIONS},
    {"name": "最近采集时间", "type": "datetime", "style": {"format": "yyyy/MM/dd HH:mm"}},
    {"name": "话题标签", "type": "text", "style": {"type": "plain"}},
    {"name": "原始文案", "type": "text", "style": {"type": "plain"}},
    {"name": "点赞数", "type": "number", "style": {"type": "plain", "precision": 0, "percentage": False, "thousands_separator": False}},
)

CANONICAL_WORK_FIELD_NAMES = frozenset(field["name"] for field in CANONICAL_WORK_FIELDS)
