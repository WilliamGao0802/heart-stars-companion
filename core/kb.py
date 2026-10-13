"""知识检索（RAG 的「检索」这一半，与 packages/core/src/knowledge.js 同源）。

职责：拿到用户这句话 + 本地引擎分析出的情绪/话题，从卡片库里挑出最相关的 2-3 张，
      渲染成一段参考资料，注入 system prompt。

为什么不用向量检索：卡片只有几十张，标签匹配 + 评分完全够用，且可解释、零网络依赖。
"""

import json
import os

MAX_PICK = 3      # 最多注入几张（多了会稀释重点、也吃 token）
MIN_SCORE = 4     # 低于这个分数宁可不注入

# 只在「求建议」时才允许注入的知识卡（讲道理型）
ADVICE_ONLY_TAGS = ["compare", "study", "work", "phone", "money"]

_CARDS = None


def cards():
    """懒加载 cards.json（与 kb-cards.js 同源，75 张）。"""
    global _CARDS
    if _CARDS is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cards.json")
        with open(path, "r", encoding="utf-8") as f:
            _CARDS = json.load(f)
    return _CARDS


def score_of(card, analysis, text):
    """标签权重：命中用户当前情绪的卡，比命中话题的卡更该被选中。"""
    a = analysis or {}
    mood = a.get("mood") or "neutral"
    tags = a.get("tags") if a.get("tags") else []
    topics = a.get("topics") or []
    score = 0

    for t in card["tags"]:
        # neutral 不是一种「情绪命中」，它只是「通用卡」的标记。
        if t == "neutral":
            if mood == "neutral":
                score += 1
            continue
        if t == mood:
            score += 6
        elif t in tags:
            score += 5
        elif t in topics:
            score += 5

    # 危机卡片只能由危机情绪触发，不能「顺带」被普通对话选中。
    if "crisis" in card["tags"] and mood != "crisis":
        return -1
    if mood == "crisis" and "crisis" in card["tags"]:
        score += 10

    # 正文里有用户原话中出现的词，小幅加分
    t0 = text or ""
    if t0 and len(card["title"]) >= 2 and card["title"][:3] in t0:
        score += 1

    return score


def _has_any_tag(card, tags):
    return any(t in card["tags"] for t in tags)


def pick(text, analysis):
    a = analysis or {}
    seeking = a.get("seeking") is True

    scored = []
    for card in cards():
        # 吐槽模式下过滤掉「讲道理型」卡片：他只想被听见，不想被教育。
        if not seeking and _has_any_tag(card, ADVICE_ONLY_TAGS):
            continue
        s = score_of(card, a, text)
        if s >= MIN_SCORE:
            scored.append({"card": card, "score": s})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return [x["card"] for x in scored[:MAX_PICK]]


def render_for_prompt(cards_list, analysis):
    """渲染成给模型看的参考段。关键是最后那句「别照搬」。"""
    if not cards_list:
        return ""

    seeking = bool((analysis or {}).get("seeking"))

    lines = []
    lines.append("【这一轮你可以参考的视角】" if seeking else "【这一轮你可以参考的视角（仅用于理解他，不要讲出来）】")
    lines.append("")
    for c in cards_list:
        lines.append("· " + c["title"] + "：" + c["body"])
    lines.append("")

    if seeking:
        lines.append("他现在是在真的求解，所以可以给他一点东西——但要\"化\"进去：")
        lines.append("用他自己的说法讲，像朋友顺手提一句，不要列条、不要说\"你应该\"、不要收尾总结。")
        lines.append("绝对不要提\"研究\"\"心理学\"\"方法\"，也不要说\"我建议你试试\"。")
        lines.append("一次只给一个点，讲完就停。给多了他会觉得被教育。")
    else:
        lines.append("他现在只是想被听见，不是想听道理。")
        lines.append("这些内容仅供你理解他的处境，**不要把它们讲出来**，不要建议、不要分析、不要给方法。")
        lines.append("你要做的是：先接住他的情绪，回应他此刻的感受。两到四句，别只回一句就完了。")
        lines.append("不要建议、不要分析、不要讲道理。想让他往下说，就顺着他的话接，而不是一句接一句地追问。")

    return "\n".join(lines)


def search(text, analysis):
    picked = pick(text, analysis)
    return {"cards": picked, "text": render_for_prompt(picked, analysis), "seeking": bool((analysis or {}).get("seeking"))}
