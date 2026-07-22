"""Collect a Douyin creator's HOME PAGE profile through the logged-in browser.

This standardizes the "达人基础信息" capture that was previously done by an
out-of-repo one-off (the legacy ``authenticated_chrome_fallback`` that produced
``aligc-profile-update.json`` etc.). It is now a first-class, repeatable step:

- reuses the same logged-in Chromium ``user_data_dir`` that MediaCrawler uses
  for this creator (so no re-login is needed when the session is still valid);
- opens the creator homepage and reads the VISIBLE page text (DOM innerText) as
  the PRIMARY source of truth — Douyin's embedded SSR JSON (``RENDER_DATA`` /
  ``window._ROUTER_DATA``) frequently contains a DIFFERENT user object (e.g. the
  logged-in account or a recommended creator), so trusting it blindly yields the
  wrong nickname / counts. The DOM shows exactly what the visitor actually sees;
- the embedded JSON is used ONLY as a SECONDARY supplement (avatar URL, secUid,
  bio) and ONLY when its object unambiguously matches the visited creator
  (sec_uid / 抖音号 / nickname match);
- writes ``runtime/profile-<key>-update.json`` using the SAME field names as the
  飞书 达人基础信息表 (see ``docs/feishu-schema.md``), so
  ``check_and_onboard_new_creators.sync_profile_to_feishu`` can push it straight
  to Feishu.

Why a dedicated step and not part of the works collection:
- works collection (MediaCrawler) returns per-aweme statistics, NOT the creator
  card (nickname / follower_count / total_favorited / ip_location / ...). Those
  live on the creator homepage and were simply never wired into the pipeline,
  which is why new creators got an empty/placeholder profile.

Run with MediaCrawler's python (has Playwright):
    python collect_douyin_creator_profile.py \
        --creator-url "https://www.douyin.com/user/<sec_uid>" \
        --user-data-dir "D:/JR_project/MediaCrawler/browser_data/cdp_c8b016f_dy_user_data_dir" \
        --output runtime/profile-c8b016f-update.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> str:
    return datetime.now(tz=BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")


def sec_uid_from_url(url: str) -> str:
    m = re.search(r"user/([A-Za-z0-9_-]+)", str(url or ""))
    if m:
        return m.group(1)
    return ""


def platform_from_url(url: str) -> str:
    text = str(url or "").lower()
    if "douyin.com" in text:
        return "抖音"
    if "tiktok.com" in text:
        return "TikTok"
    return "未知"


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return None
    multiplier = 1
    if text.endswith("万"):
        multiplier = 10_000
        text = text[:-1]
    elif text.endswith("w") or text.endswith("W"):
        multiplier = 10_000
        text = text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return None


def gender_to_text(value: Any) -> str:
    # Douyin: 0 未知/未设置, 1 男, 2 女
    if value in (1, "1"):
        return "男"
    if value in (2, "2"):
        return "女"
    return "未知"


# --------------------------------------------------------------------------
# Extract the user-info object from whatever Douyin shipped in the page.
# --------------------------------------------------------------------------
PROFILE_KEYS_SCORE = [
    "nickname", "nick_name", "sec_uid", "secUid", "unique_id", "uniqueId",
    "follower_count", "followerCount", "following_count", "followingCount",
    "total_favorited", "totalFavorited", "aweme_count", "awemeCount", "item_count",
    "ip_location", "ipLocation", "province", "city", "gender",
    "signature", "bio", "avatar_300_url", "avatar_larger", "avatar_medium",
]


def _candidate_score(u: dict[str, Any]) -> int:
    return sum(1 for k in PROFILE_KEYS_SCORE if u.get(k) not in (None, "", [], {}))


def _collect_user_candidates(node: Any, depth: int = 0, found: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Gather every dict that looks like a Douyin user object (debug/reporting only)."""
    if found is None:
        found = []
    if depth > 14:
        return found
    if isinstance(node, dict):
        keys = set(node.keys())
        if "nickname" in keys or "nick_name" in keys:
            if ("sec_uid" in keys or "secUid" in keys or "follower_count" in keys
                    or "followerCount" in keys or "aweme_count" in keys or "awemeCount" in keys
                    or "unique_id" in keys or "uniqueId" in keys):
                found.append(node)
        for v in node.values():
            _collect_user_candidates(v, depth + 1, found)
    elif isinstance(node, list):
        for v in node:
            _collect_user_candidates(v, depth + 1, found)
    return found


def _pick_best_user_object(
    candidates: list[dict[str, Any]],
    target_sec_uid: str,
    expected_nickname: str = "",
    expected_unique_id: str = "",
) -> dict[str, Any] | None:
    """Prefer the object that is unambiguously the visited profile, then the richest one."""
    if not candidates:
        return None

    def sort_key(u: dict[str, Any]) -> tuple[int, int, int]:
        nick = str(u.get("nickname") or u.get("nick_name") or "")
        uid = str(u.get("unique_id") or u.get("uniqueId") or "")
        sec = str(u.get("sec_uid") or u.get("secUid") or "")
        nick_hit = 1 if (expected_nickname and expected_nickname in nick) else 0
        uid_hit = 1 if (expected_unique_id and expected_unique_id in uid) else 0
        sec_hit = 1 if sec == target_sec_uid else 0
        return (nick_hit, uid_hit, sec_hit)

    # Group by (nick_hit, uid_hit, sec_hit); among the best group pick the richest.
    ranked = sorted(candidates, key=lambda u: (sort_key(u), _candidate_score(u)), reverse=True)
    return ranked[0]


def _find_dict_with_value(node: Any, targets: set[str], depth: int = 0) -> dict[str, Any] | None:
    """Return the first dict that directly contains one of ``targets`` as a value."""
    if depth > 16 or not targets:
        return None
    if isinstance(node, dict):
        for v in node.values():
            if isinstance(v, str) and v in targets:
                return node
        for v in node.values():
            r = _find_dict_with_value(v, targets, depth + 1)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_dict_with_value(v, targets, depth + 1)
            if r is not None:
                return r
    return None


def _dig(node: Any, *path: str) -> Any:
    cur = node
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _first_url(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                return item
            if isinstance(item, dict):
                for k in ("url", "uri", "url_list"):
                    u = _first_url(item.get(k))
                    if u:
                        return u
    if isinstance(value, dict):
        for k in ("url", "uri", "url_list", "avatar_300_url", "avatar_medium", "avatar_larger"):
            u = _first_url(value.get(k))
            if u:
                return u
    return None


def parse_user_object(u: dict[str, Any], homepage: str) -> dict[str, Any]:
    sec_uid = (
        u.get("sec_uid")
        or u.get("secUid")
        or sec_uid_from_url(homepage)
    )
    avatar = (
        _first_url(u.get("avatar_300_url"))
        or _first_url(u.get("avatar300Url"))
        or _first_url(u.get("avatarUrl"))
        or _first_url(u.get("avatar_medium"))
        or _first_url(u.get("avatar_larger"))
        or _first_url(u.get("avatar_thumb"))
    )
    province = u.get("province") or ""
    city = u.get("city") or ""
    district = u.get("district") or ""
    region_parts = [p for p in (province, city, district) if p]
    region = "·".join(region_parts) if region_parts else ""
    counters_text = ""
    # Some pages nest counters under "stats" / "user_stats"
    stats = u.get("stats") or u.get("user_stats") or {}
    if isinstance(stats, dict):
        counters_text = json.dumps(stats, ensure_ascii=False)
    profile = {
        "达人昵称": u.get("nickname") or u.get("nick_name") or "",
        "达人主页地址": homepage,
        "所属平台": platform_from_url(homepage),
        "账号ID": u.get("unique_id") or u.get("uniqueId") or u.get("short_id") or "",
        "SecUID": sec_uid or u.get("secUid") or u.get("sec_uid") or "",
        "IP属地": u.get("ip_location") or u.get("ipLocation") or "",
        "所在地区": region,
        "性别": gender_to_text(u.get("gender")),
        "关注数": as_int(u.get("following_count") or u.get("followingCount") or stats.get("following_count")),
        "粉丝数": as_int(u.get("follower_count") or u.get("followerCount") or stats.get("follower_count")),
        "获赞数": as_int(u.get("total_favorited") or u.get("totalFavorited") or stats.get("total_favorited")),
        "作品数": as_int(u.get("aweme_count") or u.get("awemeCount") or u.get("item_count") or stats.get("aweme_count")),
        "账号简介": (u.get("signature") or u.get("bio") or u.get("desc") or "").strip(),
        "头像URL": avatar or "",
        "最近检查时间": beijing_now(),
        "主页采集状态": "正常",
        "账号状态": "正常",
        "主页原始数据": {
            "source": "douyin_web_profile",
            "counters_text": counters_text[:500],
            "captured_at": beijing_now() + "+08:00",
        },
    }
    # Drop empties so Feishu sync only writes real values.
    return {k: v for k, v in profile.items() if v not in (None, "", {}, [])}


def merge_profile_sources(
    dom_profile: dict[str, Any] | None,
    embedded_profile: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Prefer visible DOM values and fill only missing fields from a verified object."""
    if dom_profile is None and embedded_profile is None:
        return None
    profile = dict(dom_profile or {})
    for key, value in (embedded_profile or {}).items():
        if profile.get(key) in (None, "", {}, []):
            profile[key] = value
    return profile


# --------------------------------------------------------------------------
# DOM (visible page text) -> profile. This is the PRIMARY, authoritative source.
# --------------------------------------------------------------------------
def _nickname_from_title(title: str) -> str:
    if not title:
        return ""
    t = title.strip()
    # "昵称的抖音 - 抖音" / "昵称的个人的主页 - 抖音" / "昵称的主页 - 抖音"
    m = re.match(r"^(.+?)(?:的(?:抖音|个人的主页|个人主页|主页)| - 抖音| \| 抖音)", t)
    if m:
        return m.group(1).strip()
    return t.replace(" - 抖音", "").replace(" | 抖音", "").strip()


def _nickname_from_text(text: str) -> str:
    """Fallback: the token on the line just before '关注'."""
    m = re.search(r"([^\n]{1,40})\n\s*关注", text)
    if m:
        cand = m.group(1).strip()
        if cand and cand not in ("精选", "推荐", "关注", "朋友", "我的", "直播"):
            return cand
    return ""


def _bio_from_dom(text: str, region: str, gender: str) -> str:
    """Extract the profile bio (账号简介) from the visible page text.

    The bio sits between the header block (nickname / 抖音号 / IP属地 / region /
    gender) and the stats line or tab navigation. We scan lines after the header
    ends, stopping at the stats line (关注/粉丝/获赞 + digits) or a tab keyword
    (作品/喜欢/...). This is the authoritative source — the embedded JSON's
    signature field belongs to a fixed placeholder account and must NOT be used.
    """
    if not text:
        return ""
    TAB = ("作品", "喜欢", "推荐", "精选", "关注", "粉丝", "获赞", "直播", "商品", "动态", "收藏")
    ACTION = ("更多", "分享主页", "私信", "已关注", "取消关注", "编辑资料",
              "加好友", "发私信", "转发", "举报", "设置", "他的群聊", "粉丝群")
    started = False
    bio: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if started and bio and bio[-1] != "":
                bio.append("")
            continue
        if not started:
            if (region and region in s) or (gender and re.fullmatch(r"(男|女)", s)) or \
               (gender and gender in s and ("·" in s or "IP" in s or "抖音号" in s)):
                started = True
            continue
        # started: skip any stray header bits that trail the bio
        if region and region in s:
            continue
        if re.fullmatch(r"(男|女)", s):
            continue
        # stop at stats / tab navigation / action buttons
        if s in TAB or s in ACTION or \
           re.search(r"(关注|粉丝|获赞)\D*\d", s) or \
           re.search(r"^[\d.,万wW]+\s*(关注|粉丝|获赞|作品|喜欢)$", s) or \
           re.search(r"^(关注|粉丝|获赞|作品|喜欢)\s*[\d.,万wW]+", s):
            break
        bio.append(s)
    return "\n".join(bio).strip()


def _parse_dom_profile(dom_text: str, homepage: str, page_title: str, avatar_url: str = "") -> dict[str, Any] | None:
    """Build the profile from the VISIBLE page text (authoritative for the visited creator)."""
    if not dom_text:
        return None
    text = dom_text
    counters: dict[str, Any] = {}

    # Stats block (order is usually 关注 / 粉丝 / 获赞, possibly 作品 too).
    m = re.search(
        r"关注\s*([\d.,万wW]+)\s*粉丝\s*([\d.,万wW]+)\s*获赞\s*([\d.,万wW]+)", text
    )
    if m:
        counters["关注数"] = as_int(m.group(1))
        counters["粉丝数"] = as_int(m.group(2))
        counters["获赞数"] = as_int(m.group(3))
    # Individual labels (fallback / 作品 count which is on its own tab).
    # Handle both "粉丝 5.4万" and "5.4万 粉丝" layouts.
    for label, key in (("粉丝", "粉丝数"), ("获赞", "获赞数"), ("作品", "作品数"), ("关注", "关注数")):
        if key not in counters:
            mm = re.search(rf"{label}\s*([\d.,万wW]+)", text) or re.search(rf"([\d.,万wW]+)\s*{label}", text)
            if mm:
                counters[key] = as_int(mm.group(1))

    # 抖音号：zaoweilai8  ->  账号ID (the real handle, not the numeric uid)
    account_id = ""
    mu = re.search(r"抖音号[：:]\s*([^\s\n]+)", text)
    if mu:
        account_id = mu.group(1).strip()

    # IP属地：江苏
    ip_loc = ""
    mip = re.search(r"IP属地[：:]\s*([^\s\n]+)", text)
    if mip:
        ip_loc = mip.group(1).strip()

    # 性别: a standalone 男 / 女 line
    gender = ""
    mg = re.search(r"(?:^|\n)\s*(男|女)\s*(?:\n|$)", text)
    if mg:
        gender = mg.group(1)

    # 所在地区: a "省份·城市" line (e.g. 江苏·扬州)
    region = ""
    mreg = re.search(r"([^\s\n，,]{1,10}·[^\s\n，,]{1,10})", text)
    if mreg:
        region = mreg.group(1).strip()

    nickname = _nickname_from_title(page_title) or _nickname_from_text(text)

    if not (nickname or account_id or counters or ip_loc or region or gender):
        return None

    profile: dict[str, Any] = {
        "达人昵称": nickname,
        "达人主页地址": homepage,
        "所属平台": platform_from_url(homepage),
        "账号ID": account_id,
        "SecUID": sec_uid_from_url(homepage),
        "IP属地": ip_loc,
        "所在地区": region,
        "性别": gender,
        "最近检查时间": beijing_now(),
        "主页采集状态": "正常",
        "账号状态": "正常",
        "主页原始数据": {
            "source": "douyin_web_profile_dom",
            "captured_at": beijing_now() + "+08:00",
        },
    }
    profile.update(counters)
    bio = _bio_from_dom(text, region, gender)
    if bio:
        profile["账号简介"] = bio
    if avatar_url:
        profile["头像URL"] = avatar_url
    return {k: v for k, v in profile.items() if v not in (None, "", {}, [])}


# --------------------------------------------------------------------------
# Browser collection
# --------------------------------------------------------------------------
def collect(creator_url: str, user_data_dir: str, output: Path, *, headless: bool = True, timeout: int = 60) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    sec_uid = sec_uid_from_url(creator_url)
    url = f"https://www.douyin.com/user/{sec_uid}" if sec_uid else creator_url

    router_data = None
    render_data = None
    dom_text = ""
    page_title = ""
    avatar_url = ""

    with sync_playwright() as p:
        # Playwright requires the persistent profile via launch_persistent_context,
        # not the raw --user-data-dir chromium flag.
        context = p.chromium.launch_persistent_context(
            user_data_dir,
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        page = context.new_page()
        # ---- 拦截抖音 aweme/post 接口，抓取真实数字 UID ----
        # RENDER_DATA / _ROUTER_DATA 里注入的是固定假账号「出钱一丁」的 uid，绝不可用；
        # 而 aweme/post 接口返回的 aweme_list[].author.uid 才是「被访问达人」本人的真实 uid。
        # 用 sec_uid 比对确认是本人后才采用，且校验为纯数字、与 SecUID 不同、位数合理。
        captured_uid: dict[str, Any] = {"value": None}

        def _maybe_capture_uid(response) -> None:
            u = response.url or ""
            if "aweme/post" not in u and "user/info" not in u and "user/profile" not in u:
                return
            try:
                data = response.json()
            except Exception:
                return
            if not isinstance(data, dict):
                return
            author = None
            # 路径1：aweme/post -> aweme_list[].author
            for aw in (data.get("aweme_list") or [])[:3]:
                if not isinstance(aw, dict):
                    continue
                a = aw.get("author") or {}
                if str(a.get("sec_uid") or a.get("secUid") or "") == sec_uid:
                    author = a
                    break
            # 路径2：user/info -> data.user
            if author is None and isinstance(data.get("user"), dict):
                a = data["user"]
                if str(a.get("sec_uid") or a.get("secUid") or "") == sec_uid:
                    author = a
            if author is None:
                return
            uid = author.get("uid") or author.get("uid_str")
            if uid is None:
                return
            uid_s = str(uid).strip()
            # 必须纯数字、与 SecUID 不同、位数合理（抖音数字 UID 通常 8~19 位）
            if uid_s.isdigit() and uid_s != sec_uid and 8 <= len(uid_s) <= 19:
                captured_uid["value"] = uid_s

        page.on("response", _maybe_capture_uid)
        page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)

        # nickname / avatar don't depend on async stat loading — read once.
        try:
            page_title = page.title() or ""
        except Exception:
            page_title = ""
        try:
            avatar_url = page.evaluate(
                """() => {
                  const sel = 'img[class*="avatar"], [class*="avatar"] img, img[data-e2e*="avatar"]';
                  const el = document.querySelector(sel);
                  if (el && el.src) return el.src;
                  const all = Array.from(document.querySelectorAll('img'));
                  for (const im of all) {
                    const s = im.getAttribute('src') || im.src || '';
                    if (s && s.indexOf('douyinpic.com') !== -1) return s;
                  }
                  return '';
                }"""
            ) or ""
        except Exception:
            avatar_url = ""

        # The follower/like counters render ASYNCHRONOUSLY after the labels and are
        # flaky (sometimes take >10s, and the video grid also shows "作品" + like
        # counts, so we must require 粉丝 AND 获赞 specifically). Retry a few times,
        # each time waiting for 粉丝+获赞 to have a digit nearby, then read the
        # visible DOM and KEEP THE PARSE WITH THE MOST STATS FILLED.
        #
        # NOTE: the embedded RENDER_DATA/_ROUTER_DATA is NOT used for stats. Douyin
        # injects a fixed placeholder account ("出钱一丁", uid 631261637) into
        # RENDER_DATA regardless of which profile is visited, so it is never the
        # real creator — using it would corrupt the numbers.
        dom_text = ""
        best_score = -1
        for _attempt in range(4):
            try:
                page.wait_for_function(
                    """() => {
                        const t = document.body ? document.body.innerText : '';
                        const near = (label) => {
                            const i = t.indexOf(label);
                            if (i < 0) return false;
                            const before = t.slice(Math.max(0, i - 12), i).replace(/\\s/g, '');
                            const after = t.slice(i, i + 24).replace(/\\s/g, '');
                            return /[0-9]/.test(before) || /[0-9]/.test(after.replace(label, ''));
                        };
                        return near('粉丝') && near('获赞');
                    }""",
                    timeout=4000,
                )
            except Exception:
                pass
            try:
                dom = page.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                dom = ""
            prof = _parse_dom_profile(dom, url, page_title, avatar_url)
            if prof:
                score = sum(1 for k in ("粉丝数", "获赞数", "作品数") if prof.get(k))
                if score > best_score:
                    best_score = score
                    dom_text = dom
            if best_score >= 2:  # fans + likes captured -> good enough
                break

        # Embedded JSON (kept only for debugging; NOT used for stats — see note above)
        try:
            router_data = page.evaluate("() => window._ROUTER_DATA ?? null")
        except Exception:
            router_data = None
        try:
            raw = page.evaluate(
                "() => { const el = document.getElementById('RENDER_DATA'); return el ? el.textContent : null; }"
            )
            if raw:
                import urllib.parse
                render_data = json.loads(urllib.parse.unquote(raw))
        except Exception:
            render_data = None

        browser_close = getattr(context, "close", None)
        if callable(browser_close):
            browser_close()

    # ---- PRIMARY: build from the visible DOM (authoritative for this creator) ----
    dom_profile = _parse_dom_profile(dom_text, url, page_title, avatar_url)

    # ---- SECONDARY: only use embedded JSON if it really is this creator ----
    embedded_profile: dict[str, Any] | None = None
    expected_nick = (dom_profile or {}).get("达人昵称", "")
    expected_uid = (dom_profile or {}).get("账号ID", "")
    targets = {t for t in (sec_uid, expected_uid, expected_nick) if t}
    matched = None
    if targets:
        matched = _find_dict_with_value(render_data, targets) or _find_dict_with_value(router_data, targets)
    if matched:
        m_sec = str(matched.get("sec_uid") or matched.get("secUid") or "")
        m_uid = str(matched.get("unique_id") or matched.get("uniqueId") or "")
        m_nick = str(matched.get("nickname") or matched.get("nick_name") or "")
        if (m_sec == sec_uid) or (expected_uid and expected_uid in m_uid) or (expected_nick and expected_nick in m_nick):
            embedded_profile = parse_user_object(matched, url)

    # Report-only candidates (debug), but NEVER let a mismatched object win.
    candidates = _collect_user_candidates(router_data) + _collect_user_candidates(render_data)
    _seen: set[str] = set()
    _uniq: list[dict[str, Any]] = []
    for c in candidates:
        cid = str(c.get("sec_uid") or c.get("secUid") or c.get("unique_id") or c.get("uniqueId") or id(c))
        if cid in _seen:
            continue
        _seen.add(cid)
        _uniq.append(c)

    profile = merge_profile_sources(dom_profile, embedded_profile)

    # 真实数字 UID：来自 aweme/post 接口（已用 sec_uid 校验过是本人，且为纯数字）。
    # 绝不来自 RENDER_DATA 的污染假账号；抓不到则留空（抖音UID 不在 PROFILE 必填列表）。
    if profile is not None and captured_uid["value"]:
        profile["抖音UID"] = captured_uid["value"]

    debug = {
        "url": url,
        "sec_uid": sec_uid,
        "page_title": page_title,
        "dom_has_login_button": ("登录" in dom_text) or ("扫码" in dom_text),
        "router_data_present": router_data is not None,
        "render_data_present": render_data is not None,
        "candidate_count": len(_uniq),
        "sec_uid_matches": sum(1 for c in _uniq if str(c.get("sec_uid") or c.get("secUid") or "") == sec_uid),
        "embedded_matched": embedded_profile is not None,
        "used_dom_as_primary": dom_profile is not None,
        "chosen_object": (embedded_profile or dom_profile),
        "dom_sample": dom_text[:3000],
    }
    debug_path = output.with_suffix(".debug.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    debug_tmp = debug_path.with_suffix(debug_path.suffix + ".tmp")
    debug_tmp.write_text(json.dumps(debug, ensure_ascii=False, indent=2), encoding="utf-8")
    debug_tmp.replace(debug_path)

    login_expired = debug["dom_has_login_button"] or (not dom_text and not router_data and not render_data)
    if not profile or not profile.get("达人昵称"):
        return {
            "ok": False,
            "reason": "未从主页解析到用户资料（可能登录态失效，需重新扫码登录）" if login_expired
                      else "主页已加载但未解析到资料字段，请检查 DOM 结构",
            "debug_file": str(debug_path),
            "page_title": page_title,
        }

    output_tmp = output.with_suffix(output.suffix + ".tmp")
    output_tmp.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    output_tmp.replace(output)
    return {
        "ok": True,
        "output": str(output),
        "debug_file": str(debug_path),
        "nickname": profile.get("达人昵称"),
        "粉丝数": profile.get("粉丝数"),
        "获赞数": profile.get("获赞数"),
        "作品数": profile.get("作品数"),
        "账号ID": profile.get("账号ID"),
        "抖音UID": profile.get("抖音UID"),
        "IP属地": profile.get("IP属地"),
        "性别": profile.get("性别"),
        "所在地区": profile.get("所在地区"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="通过已登录浏览器抓取抖音达人主页资料")
    parser.add_argument("--creator-url", required=True)
    parser.add_argument("--user-data-dir", required=True, help="复用 MediaCrawler 的 Chromium 登录态目录")
    parser.add_argument("--output", required=True, help="profile-<key>-update.json 输出路径")
    parser.add_argument("--headless", action="store_true", default=True, help="无头模式（默认）")
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    result = collect(
        args.creator_url, args.user_data_dir, Path(args.output),
        headless=args.headless, timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
