"""对话编排（core 层，不依赖 streamlit）。

把三端 chat.vue 里的 buildContext / finishAI 逻辑抽到这里，view 只负责渲染与收集输入。
"""

from . import companion, kb

MAX_HISTORY = 12


def build_context(memory_store, memory, messages, last_text, last_analysis):
    """组装 messages（system 必须第一）。

    顺序与三端完全一致：基础人设 → 长期记忆 → RAG 知识参考 → 本轮行为约束 → 最近 12 轮。
    返回 (messages, kb_cards, ask_mode)。
    """
    sys_parts = [companion.SYSTEM_PROMPT]

    # ① 长期记忆
    mem_text = memory_store.render_for_prompt(memory)
    if mem_text:
        sys_parts += ["", mem_text]

    # ② RAG 知识参考
    kb_res = kb.search(last_text or "", last_analysis or {"mood": "neutral", "topics": []})
    if kb_res["text"]:
        sys_parts += ["", kb_res["text"]]

    # ③ 本轮行为约束（追问抑制 + 情形 C）
    hints = []
    ask_mode = ""
    ai_texts = [m.get("text") for m in messages if m.get("role") == "ai" and m.get("text")]

    ask_hint = companion.ask_pressure_hint(ai_texts)
    if ask_hint:
        hints.append(ask_hint)
        ask_mode = "any"

    low_hint = companion.low_mood_hint(last_analysis, last_text)
    if low_hint:
        hints.append(low_hint)
        if not ask_mode:
            ask_mode = "why"

    if hints:
        sys_parts += ["", "\n\n".join(hints)]

    out = [{"role": "system", "content": "\n".join(sys_parts)}]
    for m in messages[-MAX_HISTORY:]:
        if m.get("role") == "user" and m.get("text"):
            out.append({"role": "user", "content": m["text"]})
        elif m.get("role") == "ai" and m.get("text"):
            out.append({"role": "assistant", "content": m["text"]})

    return out, kb_res["cards"], ask_mode


def finalize_reply(text, ask_mode=""):
    """回复落地前整平 + 本地剪掉多余的结尾问句。"""
    text = companion.tidy_reply(text)
    if ask_mode and text.rstrip()[-1:] in ("？", "?") and text.rstrip().endswith(("？", "?")):
        text = companion.strip_trailing_question(text, ask_mode)
    return text


def local_fallback(text, state):
    """AI 不可用时的本地规则版回复（与小程序路线 B 同一套文案）。"""
    msgs = companion.reply_to(text, state)
    out = []
    for m in msgs:
        if m.get("role") == "ai":
            out.append(m["text"])
        else:
            out.append(m.get("title", "") + "\n\n" + m.get("text", ""))
    return "\n\n".join(out)
