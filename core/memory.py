"""长期记忆系统（与 packages/core/src/memory.js 逐字同源）。

解决的核心问题：模型每次开口都是白纸，不记得你是谁。
做法：把「关于这个人的事」压成一份很短的档案，每次对话注入 system prompt。

三层结构：
  ① profile —— 稳定事实（称呼、长期困扰、重要关系、目标、用户的要求）。低频更新，最重要。
  ② mood    —— 近期状态（最近情绪基调、最近关注的话题）。高频更新，快速失效。
  ③ count   —— 计数与水位（累计轮数、上次摘要到第几轮、上次摘要时间）。

平台依赖全部注入：
  storage —— 任何有 get/set/remove 的对象（Streamlit 里用 session_state 包一层，或纯内存 dict）。
  cloud   —— 可选，「云客户端」对象，需有 auth_uid() / auth_signed_in() / db_select() / db_upsert() / db_delete()。
             不传 = 纯本地记忆（单设备有效），sync/push/clear 全部安静降级，绝不抛异常。
"""

import json
import re
import time

MEM_KEY = "xunxun_memory"
SUMMARY_EVERY_TURNS = 10                    # 每 10 轮摘要一次
SUMMARY_MIN_INTERVAL = 30 * 60 * 1000       # 且两次摘要至少间隔 30 分钟

# 摘要用的提示词。要求极严：只输出 JSON、不要解释、不确定就不写。
SUMMARY_PROMPT = "\n".join([
    "你在维护一份关于用户的极简档案，用于让陪伴者下次开口时记得这个人。",
    "",
    "请阅读下面的对话，输出一个 JSON 对象（不要任何解释文字、不要 markdown 代码块）：",
    "{",
    '  "call": "用户希望被怎么称呼；没提到就留空字符串",',
    '  "facts": ["关于这个人的稳定事实，每条不超过 20 字，最多 4 条"],',
    '  "matters": ["他身上长期在意的议题，最多 3 个，每个 2-6 字"],',
    '  "goals": ["他提到过想做的事，最多 2 条，每条不超过 20 字"],',
    '  "asks": ["用户对你明确提出的要求或约定（比如「别再问我这个」「不要给建议」「叫我小名」），最多 2 条，每条不超过 16 字"],',
    '  "tone": "他最近的情绪基调，一句话，不超过 20 字",',
    '  "topics": ["最近谈到的话题词，最多 4 个，每个 2-6 字"]',
    "}",
    "",
    "铁律：",
    "1. 只写对话里明确出现过的信息。绝对不要推测、不要补充你没有看到的细节。",
    "2. facts 只写相对稳定的（职业、城市、家庭结构、长期状况），不写\"今天心情不好\"这类一次性的。",
    "3. 涉及自伤、自杀、虐待等内容，不要写进 facts，只把 tone 写成\"近期状态需要关注\"。",
    "4. 如果某个字段没有可写的信息，就给空字符串或空数组。宁可空，不要编。",
    "5. 全部用第三人称，用\"他\"或\"用户\"，不要用\"你\"。",
])


# ==================== 档案结构 ====================
def create():
    return {
        "profile": {
            "call": "",      # 希望被怎么称呼
            "facts": [],     # 稳定事实，最多 8 条
            "matters": [],   # 长期在意的议题，最多 5 条
            "goals": [],     # 提到过的想做的事，最多 3 条
            "asks": [],      # 用户对你提出的要求/约定，最多 4 条（陪伴时必须遵守）
        },
        "mood": {"tone": "", "topics": [], "at": 0},
        "count": {"turns": 0, "summarizedAt": 0, "lastAt": 0},
    }


def _to_str_list(v, max_n):
    if not isinstance(v, list):
        return []
    out = []
    for item in v:
        if len(out) >= max_n:
            break
        s = str(item or "").strip()[:40]
        if s and s not in out:
            out.append(s)
    return out


def normalize(m):
    """把任意来源的对象补全成完整结构（本地存的可能是旧版本、云端可能缺字段）。"""
    base = create()
    if not isinstance(m, dict):
        return base

    p = m.get("profile") or {}
    base["profile"]["call"] = str(p.get("call") or "")[:20]
    base["profile"]["facts"] = _to_str_list(p.get("facts"), 8)
    base["profile"]["matters"] = _to_str_list(p.get("matters"), 5)
    base["profile"]["goals"] = _to_str_list(p.get("goals"), 3)
    base["profile"]["asks"] = _to_str_list(p.get("asks"), 4)

    mo = m.get("mood") or {}
    base["mood"]["tone"] = str(mo.get("tone") or "")[:60]
    base["mood"]["topics"] = _to_str_list(mo.get("topics"), 4)
    base["mood"]["at"] = int(mo.get("at") or 0)

    c = m.get("count") or {}
    base["count"]["turns"] = int(c.get("turns") or 0)
    base["count"]["summarizedAt"] = int(c.get("summarizedAt") or 0)
    base["count"]["lastAt"] = int(c.get("lastAt") or 0)

    return base


# ==================== 工厂：注入平台依赖 ====================
def create_memory(storage=None, cloud=None):
    # ⚠️ 必须用 is None 判断，不能写 `storage or {}`：空字典是 falsy，
    #    那样每次调用都会新建一个字典，「星寻记住你」会静默失效（踩过）。
    storage = storage if storage is not None else {}
    cloud = cloud if cloud is not None else {}

    def _get(k):
        if hasattr(storage, "get"):
            try:
                return storage.get(k)
            except Exception:
                return None
        return storage.get(k)

    def _set(k, v):
        if hasattr(storage, "set"):
            try:
                storage.set(k, v)
            except Exception:
                pass
        else:
            storage[k] = v

    def _remove(k):
        if hasattr(storage, "remove"):
            try:
                storage.remove(k)
            except Exception:
                pass
        else:
            storage.pop(k, None)

    def _fn(name):
        """取注入的 cloud 成员：兼容对象属性与字典两种写法。"""
        v = getattr(cloud, name, None)
        if v is None and isinstance(cloud, dict):
            v = cloud.get(name)
        return v

    def can_sync():
        return bool(cloud) and callable(_fn("db_select")) and callable(_fn("auth_signed_in"))

    # ---- 读写：本地 ----
    def load():
        try:
            saved = _get(MEM_KEY)
        except Exception:
            saved = None
        if isinstance(saved, str):
            try:
                saved = json.loads(saved)
            except Exception:
                saved = None
        return normalize(saved)

    def save(m):
        n = normalize(m)
        try:
            _set(MEM_KEY, n)
        except Exception:
            pass
        return n

    def clear():
        try:
            _remove(MEM_KEY)
        except Exception:
            pass
        return create()

    # ---- 是否该摘要 ----
    def should_summarize(m, now_ms=None):
        n = normalize(m)
        t = now_ms if now_ms else int(time.time() * 1000)
        if n["count"]["turns"] - n["count"]["summarizedAt"] < SUMMARY_EVERY_TURNS:
            return False
        if n["count"]["lastAt"] and t - n["count"]["lastAt"] < SUMMARY_MIN_INTERVAL:
            return False
        return True

    def bump(m, n=1):
        x = normalize(m)
        x["count"]["turns"] += (n or 1)
        return x

    # ---- 摘要 ----
    def digest(messages, limit=30):
        msgs = [m for m in (messages or []) if m and m.get("role") in ("user", "ai") and m.get("text")]
        start = max(0, len(msgs) - limit)
        lines = []
        for m in msgs[start:]:
            who = "用户" if m["role"] == "user" else "星寻"
            lines.append(who + "：" + re.sub(r"\s+", " ", str(m["text"]))[:120])
        return "\n".join(lines)

    def parse_json(text):
        if not text:
            return None
        s = str(text).strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, re.I)
        if fence:
            s = fence.group(1).strip()
        start = s.find("{")
        end = s.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            return None

    def merge(current, update):
        m = normalize(current)
        if not isinstance(update, dict):
            return m

        if update.get("call"):
            m["profile"]["call"] = str(update["call"]).strip()[:20]

        m["profile"]["facts"] = _to_str_list(m["profile"]["facts"] + list(update.get("facts") or []), 8)
        m["profile"]["matters"] = _to_str_list(m["profile"]["matters"] + list(update.get("matters") or []), 5)
        m["profile"]["goals"] = _to_str_list(m["profile"]["goals"] + list(update.get("goals") or []), 3)
        m["profile"]["asks"] = _to_str_list(m["profile"]["asks"] + list(update.get("asks") or []), 4)

        if update.get("tone"):
            m["mood"]["tone"] = str(update["tone"]).strip()[:60]
        t = _to_str_list(update.get("topics") or [], 4)
        if t:
            m["mood"]["topics"] = t
        m["mood"]["at"] = int(time.time() * 1000)

        return m

    def summarize(messages, current, ask):
        """执行一次摘要。ask(messages) -> str | None。永不抛异常。"""
        base = normalize(current)
        if not callable(ask):
            return {"memory": base, "changed": False, "reason": "no-asker"}

        text = digest(messages)
        if not text or len(text) < 40:
            return {"memory": base, "changed": False, "reason": "too-little"}

        try:
            reply = ask([
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": "这是最近的对话：\n\n" + text},
            ])
        except Exception as err:
            return {"memory": base, "changed": False, "reason": _describe(err)}

        js = parse_json(reply)
        if not js:
            return {"memory": base, "changed": False, "reason": "bad-json"}

        merged = merge(base, js)
        merged["count"]["summarizedAt"] = base["count"]["turns"]
        merged["count"]["lastAt"] = int(time.time() * 1000)

        changed = (json.dumps(merged["profile"], ensure_ascii=False) != json.dumps(base["profile"], ensure_ascii=False)
                   or json.dumps(merged["mood"], ensure_ascii=False) != json.dumps(base["mood"], ensure_ascii=False))

        return {"memory": merged, "changed": changed, "reason": ""}

    # ---- 渲染：注入 system prompt ----
    def render_for_prompt(m):
        n = normalize(m)
        p = n["profile"]
        has_profile = bool(p["call"] or p["facts"] or p["matters"] or p["goals"] or p["asks"])
        has_mood = bool(n["mood"]["tone"] or n["mood"]["topics"])
        if not has_profile and not has_mood:
            return ""

        lines = []
        lines.append("【关于这个人，你已经知道的】")
        lines.append("（这是你以前陪他时记下来的。自然地用，不要复述这份档案，不要说\"我记得你说过\"这种刻意的话。）")

        if p["call"]:
            lines.append("· 你以前称呼他：" + p["call"])
        if p["facts"]:
            lines.append("· 你知道的：" + "；".join(p["facts"]))
        if p["matters"]:
            lines.append("· 他一直放不下的：" + "、".join(p["matters"]))
        if p["goals"]:
            lines.append("· 他提过想做的：" + "；".join(p["goals"]))
        if p["asks"]:
            lines.append("· 他要求过你的（务必遵守）：" + "；".join(p["asks"]))

        if has_mood:
            parts = []
            if n["mood"]["tone"]:
                parts.append("最近的状态是「" + n["mood"]["tone"] + "」")
            if n["mood"]["topics"]:
                parts.append("最近常说到「" + "、".join(n["mood"]["topics"]) + "」")
            if parts:
                lines.append("· " + "，".join(parts) + "。")

        lines.append("")
        lines.append("用这些信息让对话更连贯，但别显得刻意。如果他这次说的和档案里的情况不一样，以他此刻说的为准。")
        return "\n".join(lines)

    # ---- 云端同步 ----
    def to_cloud(m):
        n = normalize(m)
        return {
            "profile": n["profile"],
            "mood": n["mood"],
            "turn_count": n["count"]["turns"],
            "summarized_at": n["count"]["summarizedAt"],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        }

    def from_cloud(row, local):
        if not row:
            return normalize(local)
        return normalize({
            "profile": row.get("profile"),
            "mood": row.get("mood"),
            "count": {
                "turns": row.get("turn_count"),
                "summarizedAt": row.get("summarized_at"),
                "lastAt": _parse_ts(row.get("updated_at")),
            },
        })

    def push_cloud(m):
        if not can_sync() or not _fn("auth_signed_in")():
            return False
        row = to_cloud(m)
        try:
            uid = _fn("auth_uid")()
            _fn("db_upsert")("user_memory", {"user_id": uid}, row)
            return True
        except Exception:
            return False

    def sync_cloud(local=None):
        mine = normalize(local if local is not None else load())
        if not can_sync():
            return {"synced": False, "memory": mine, "reason": "cloud-disabled"}
        if not _fn("auth_signed_in")():
            return {"synced": False, "memory": mine, "reason": "not-signed-in"}

        try:
            rows = _fn("db_select")("user_memory", {"user_id": "eq." + _fn("auth_uid")()}, limit=1)
        except Exception as err:
            return {"synced": False, "memory": mine, "reason": _describe(err)}

        if not rows:
            push_cloud(mine)
            return {"synced": True, "memory": mine, "created": True}

        remote = from_cloud(rows[0], mine)
        if remote["count"]["turns"] > mine["count"]["turns"]:
            save(remote)
            return {"synced": True, "memory": remote, "source": "cloud"}
        if mine["count"]["turns"] > remote["count"]["turns"]:
            push_cloud(mine)
        return {"synced": True, "memory": mine, "source": "local"}

    def clear_cloud():
        if not can_sync():
            return {"ok": True, "cloud": False}
        uid = _fn("auth_uid")()
        if not uid:
            return {"ok": True, "cloud": False}
        try:
            _fn("db_delete")("user_memory", {"user_id": "eq." + uid})
            return {"ok": True, "cloud": True}
        except Exception as err:
            return {"ok": False, "cloud": False, "reason": _describe(err)}

    from types import SimpleNamespace
    return SimpleNamespace(
        load=load, save=save, clear=clear,
        bump=bump, should_summarize=should_summarize,
        summarize=summarize, digest=digest, parse_json=parse_json,
        merge=merge, render_for_prompt=render_for_prompt,
        to_cloud=to_cloud, from_cloud=from_cloud,
        push_cloud=push_cloud, sync_cloud=sync_cloud, clear_cloud=clear_cloud,
        SUMMARY_EVERY_TURNS=SUMMARY_EVERY_TURNS, SUMMARY_PROMPT=SUMMARY_PROMPT, MEM_KEY=MEM_KEY,
    )


def _parse_ts(s):
    if not s:
        return 0
    try:
        t = str(s).replace("Z", "+00:00")
        from datetime import datetime
        return int(datetime.fromisoformat(t).timestamp() * 1000)
    except Exception:
        return 0


def _describe(err):
    if not err:
        return "unknown"
    code = getattr(err, "code", None) or ""
    if code == "42P01":
        return "no-table"
    if code == "42501":
        return "no-perm"
    if str(code).startswith("quota_"):
        return "quota"
    if str(code).startswith("model_"):
        return "model"
    return "unknown"
