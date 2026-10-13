"""7 天心情曲线（与 packages/core/src/moodcurve.js 同源，按 UTC+8 计算）。

纯逻辑，三处共用同一份口径：书写页的心情选择、周记卡的 7 天色带、分享文案。
隐私红线：分享文案只带条数与心情分布，绝不带心里话原文。
"""

import re
from datetime import datetime, timedelta, timezone

from .config import SITE_URL, TZ_OFFSET_HOURS

DAY = 24 * 60 * 60 * 1000
_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

MOOD_FACES = [
    {"id": "good", "face": "☀", "name": "还不错", "color": "#ffd68f"},
    {"id": "neutral", "face": "☁", "name": "就那样", "color": "#8e97ad"},
    {"id": "low", "face": "☂", "name": "有点低落", "color": "#6f8fd8"},
    {"id": "anxious", "face": "⚡", "name": "有点焦虑", "color": "#b07fd0"},
    {"id": "angry", "face": "✹", "name": "有点生气", "color": "#e0705f"},
    {"id": "lonely", "face": "☾", "name": "有点孤独", "color": "#56a3a3"},
]

WEEK_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_MOOD_MAP = {m["id"]: m for m in MOOD_FACES}

EMPTY_COLOR = "rgba(243,237,226,0.10)"


def mood_face(mid):
    m = _MOOD_MAP.get(mid)
    return m["face"] if m else ""


def mood_name(mid):
    m = _MOOD_MAP.get(mid)
    return m["name"] if m else ""


def mood_color(mid):
    m = _MOOD_MAP.get(mid)
    return m["color"] if m else EMPTY_COLOR


def _to_dt(ts):
    return datetime.fromtimestamp(ts / 1000, tz=_TZ)


def month_day(ts):
    d = _to_dt(ts)
    return "%d/%d" % (d.month, d.day)


def month_day_cn(ts):
    d = _to_dt(ts)
    return "%d月%d日" % (d.month, d.day)


def get_week_start(ts):
    """该时间所在这一周的周一 00:00（UTC+8）。"""
    d = _to_dt(ts)
    dow = d.isoweekday()          # 1=周一 … 7=周日
    monday = d - timedelta(days=dow - 1)
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp() * 1000)


def week_mood_series(items, week_end_ts=None):
    """从 weekEndTs 所在周的周一出发，连数 7 天。同日多条取最后一条的心情。"""
    start = get_week_start(week_end_ts or int(datetime.now(tz=_TZ).timestamp() * 1000))
    lst = items or []

    series = []
    for i in range(7):
        day_start = start + i * DAY
        day_end = day_start + DAY
        mood = ""
        count = 0
        for it in lst:
            ts = it.get("ts") or it.get("createdAt") or it.get("id") or 0
            if day_start <= ts < day_end:
                count += 1
                if it.get("mood"):
                    mood = it["mood"]
        series.append({"i": i, "ts": day_start, "label": WEEK_LABELS[i],
                       "date": month_day(day_start), "mood": mood, "count": count})
    return series


def week_mood_summary(series, week_count=0):
    s = series or []
    dist = {}
    filled = 0
    good_days = 0
    for d in s:
        if not d.get("mood"):
            continue
        filled += 1
        if d["mood"] == "good":
            good_days += 1
        dist[d["mood"]] = dist.get(d["mood"], 0) + 1

    top = None
    for k in dist:
        if top is None or dist[k] > dist[top]:
            top = k

    return {
        "filled": filled,
        "goodDays": good_days,
        "moodTop": {"id": top, "name": mood_name(top), "count": dist[top]} if top else None,
        "weekCount": week_count or 0,
    }


def week_mood_caption(summary):
    """色带下方的一句话。没有记录时给的是邀请，不是批评。"""
    s = summary or {}
    if not s.get("filled") and not s.get("weekCount"):
        return "这一周还没有记录。写几条，就能看见心情的起伏。"
    if not s.get("filled"):
        return "这一周写下了 " + str(s["weekCount"]) + " 条。点亮时顺手选个心情，这里就会出现一条曲线。"

    text = "这一周有 " + str(s["filled"]) + " 天留下了心情"
    if s.get("moodTop"):
        text += "，最常是「" + s["moodTop"]["name"] + "」"
    if s.get("goodDays"):
        text += "，也有 " + str(s["goodDays"]) + " 天还不错"
    return text + "。起起伏伏，都算数。"


def share_text(summary, range_label=""):
    """分享只带条数与心情分布——绝不带心里话原文。"""
    s = summary or {}
    if not s.get("weekCount") and not s.get("filled"):
        return ""

    text = "这一周" + ("（" + range_label + "）" if range_label else "") + \
        "，我在「心与星寻」写下了 " + str(s.get("weekCount") or 0) + " 条心里话。"
    if s.get("moodTop"):
        text += "心情出现最多的是「" + s["moodTop"]["name"] + "」"
        text += ("，也有 " + str(s["goodDays"]) + " 天还不错。") if s.get("goodDays") else "。"
    text += "被记住的陪伴：" + SITE_URL
    return text
