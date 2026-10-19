"""心与星寻 · 陪伴页（Streamlit 网页版 / 桌面版）。

本文件是 **v0.12.0 的手机端还原版**：界面语言、信息层级、配色、tab 结构全部对齐
H5 / 小程序 / App 三端（见 core/theme.py 的说明）。业务逻辑仍然全部来自 core/，
本文件只做「把 core 的输出摆成手机的样子」。

三条不能破的约束（改动前务必读）：
  ① 红线：不诊断、不测评、不宣疗效、不冒用咨询身份、不做咨询关系 —— 全部由
     core/companion.py 的 SYSTEM_PROMPT 与 redline 过滤保证，UI 也不能"帮倒忙"
     （例如不得加「心理测试」「专业评估」这类入口或文案）。
  ② 隐私：用户原文进 HTML 前必须过 _esc()。日记/记忆/对话都是自由文本，
     直接拼进 unsafe_allow_html 就是存储型 XSS。
  ③ 危机：极端内容一律走 core/safety.py，先停生成、再上报、再给求助卡。
     任何"更友好"的改写都不允许绕过它。

登录说明：桌面浏览器拿不到微信授权，所以网页端只做「手机号 + 密码」。
与 App / 小程序共用同一个 auth 服务，因此 uid 相同 —— 这是四端同一数据系统的基础。
"""

import html
import os
import time
from datetime import datetime

import streamlit as st

from core import avatar as av
from core import chatflow, companion, safety, theme
from core import memory as memmod
from core import moodcurve
from core.cloud import CloudClient, CloudError, memory_cloud

VERSION = "1.3.0"
GATE_SECONDS = int(os.environ.get("HSX_GATE_SECONDS", "300"))
SOURCE = "st"
AVATAR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "avatars")

PAGES = ["书写", "对话", "我的"]
PAGE_ICON = {"书写": "✎", "对话": "☾", "我的": "☺"}


def _esc(v):
    """把任意值转成可安全放进 unsafe_allow_html 的 HTML 文本。

    为什么每个动态值都要过这一层：日记正文、长期记忆里的 facts/goals/asks
    都是**用户或模型写的自由文本**，直接拼进 st.markdown(..., unsafe_allow_html=True)
    就是存储型 XSS —— 一句 <img src=x onerror=...> 就能在这个域里执行脚本。
    换行转 <br> 是为了保住原来的排版观感。
    """
    s = html.escape(str(v if v is not None else ""), quote=True)
    return s.replace("\r\n", "\n").replace("\n", "<br>")


# ============================ 会话初始化 ============================

def _boot():
    ss = st.session_state
    if "cloud" not in ss:
        ss.cloud = CloudClient()
    if "messages" not in ss:
        ss.messages = []
    if "usage_first_at" not in ss:
        ss.usage_first_at = time.time()
        # 首次进入 = 一次打开。埋点失败不影响任何功能（track 内部全吞异常）。
        ss.cloud.track("app_open", {"v": VERSION}, platform=SOURCE)
    if "chat_state" not in ss:
        ss.chat_state = companion.create_state()
    if "mem_storage" not in ss:
        ss.mem_storage = {}
    if "avatar_id" not in ss:
        ss.avatar_id = av.DEFAULT_AVATAR_ID
    if "font_scale" not in ss:
        ss.font_scale = 1.0
    if "page" not in ss:
        ss.page = "书写"
    if "picked_mood" not in ss:
        ss.picked_mood = ""
    if "diary_local" not in ss:
        ss.diary_local = []
    if "last_claim" not in ss:
        ss.last_claim = None
    # 登录状态**只**放在 st.session_state（服务端会话）里。
    # 曾经为了「刷新后不用重新登录」把 refresh_token 写进 URL 的 ?rt= 参数，
    # 那是把长期凭据交给浏览器历史、访问日志和 Referer —— 等于可被接管账号。
    # 现在：刷新/关页即退出，公共电脑上更安全；要留住登录，重新输一次手机号即可。


def _store():
    return memmod.create_memory(st.session_state.mem_storage, memory_cloud(st.session_state.cloud))


def _avatar_path(aid):
    return os.path.join(AVATAR_DIR, av.normalize_avatar_id(aid) + ".jpg")


def _signed_in():
    return st.session_state.cloud.auth_signed_in()


def _gate_blocked():
    """5 分钟使用门槛：首次使用起算，超时且未登录 → 拦下。"""
    if _signed_in():
        return False
    return (time.time() - st.session_state.usage_first_at) > GATE_SECONDS


# ============================ 云端数据 ============================

def _cloud_diaries():
    """已登录时拉云端日记（只读展示用）。失败返回 []，绝不让页面报错。"""
    if not _signed_in():
        return []
    try:
        rows = st.session_state.cloud.db_select(
            "diary_entries", {"select": "id,content,created_at"}, limit=200, order="created_at.desc")
    except CloudError:
        return []
    out = []
    for r in rows or []:
        ts = _ts_ms(r.get("created_at"))
        out.append({"id": r.get("id"), "content": r.get("content", ""), "mood": "", "createdAt": ts, "ts": ts})
    return out


def _all_diaries():
    """本地 + 云端合并（本地这份可能包含未登录时写的）。按时间倒序。"""
    items = list(st.session_state.get("diary_local", [])) + _cloud_diaries()
    # 去重：同一条既在本地又在云端时，按内容+分钟粒度合并
    seen = set()
    uniq = []
    for it in sorted(items, key=lambda x: -(x.get("ts") or x.get("createdAt") or 0)):
        k = (str(it.get("content") or "")[:40], int((it.get("ts") or it.get("createdAt") or 0) / 60000))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return uniq


def _ts_ms(s):
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


# ============================ 聊天核心 ============================

def _report_alert(text, words):
    """极端内容上报（admin_alerts 表：RLS 允许任何身份 INSERT，不漏报）。"""
    try:
        st.session_state.cloud.db_insert(
            "admin_alerts",
            [safety.build_alert(SOURCE, st.session_state.cloud.auth_uid(), text, words)])
    except CloudError:
        pass


def _persist_msg(role, text):
    """登录后把这一轮写进 chat_messages（管理员后台能看到）。"""
    if not _signed_in() or not text:
        return
    try:
        st.session_state.cloud.db_insert("chat_messages", [{
            "client_id": "st_" + str(int(time.time() * 1000)),
            "role": role, "content": str(text)[:2000],
        }])
    except CloudError:
        pass


def _generate(prompt, placeholder):
    """一轮完整对话。返回 (回复文本, 备注)。"""
    cli = st.session_state.cloud
    store = _store()
    mem = store.load()

    # ① 安全前置拦截：极端内容不生成、不入库、直接上报
    ex = safety.detect_extreme(prompt)
    if ex["extreme"]:
        _report_alert(prompt, ex["words"])
        cli.track("crisis_l2", {"words": ",".join(ex["words"])[:120]}, platform=SOURCE)
        return safety.SAFE_REPLY, "extreme"

    # ② 记忆计数 + 组装上下文（人设 → 记忆 → RAG → 行为约束 → 最近 12 轮）
    mem = store.bump(mem, 1)
    analysis = companion.analyze(prompt)
    ctx, cards, ask_mode = chatflow.build_context(store, mem, st.session_state.messages, prompt, analysis)

    note = ""
    full = ""
    try:
        for chunk in cli.chat_stream(ctx, temperature=0.85):
            full = chunk
            placeholder.markdown('<div class="hsx-bubble-ai">%s</div>' % _esc(full + " ▌"),
                                 unsafe_allow_html=True)
        full = chatflow.finalize_reply(full, ask_mode)
        if not full:
            raise CloudError("空回复", "empty")
    except CloudError as e:
        # ③ 云端不可用 → 本地规则版兜底（安全网，不是故障）
        full = chatflow.local_fallback(prompt, st.session_state.chat_state)
        note = "云端暂时没连上，先用本地陪伴回你"
        _ = e

    st.session_state.chat_state = companion.normalize_state(st.session_state.chat_state)

    # ④ 记忆落库 + 上云
    mem = store.save(mem)
    store.push_cloud(mem)
    if store.should_summarize(mem):
        res = store.summarize(st.session_state.messages, mem,
                              lambda msgs: cli.chat_once(msgs, temperature=0.3))
        if res["changed"]:
            st.session_state.mem_storage[memmod.MEM_KEY] = res["memory"]
            store.push_cloud(res["memory"])

    # ⑤ 本轮小知识卡（危机不挂，危机卡是干预指引不是知识）
    kb_note = ""
    if analysis["mood"] != "crisis" and cards:
        kb_note = cards[0]["title"]

    _persist_msg("user", prompt)
    _persist_msg("ai", full)
    cli.track("chat_turn", {"mood": analysis.get("mood", "")}, platform=SOURCE)
    return full, (note or kb_note)


# ============================ 登录 ============================

def _do_login(phone, pwd):
    cli = st.session_state.cloud
    try:
        # 先试密码登录；失败且像新号 → 走注册（发码 + 验码）
        try:
            cli.sign_in(phone, pwd)
        except CloudError:
            code_info = cli.send_code(phone)
            st.session_state.pending_signup = {"phone": phone, "pwd": pwd,
                                               "vid": code_info["verificationId"],
                                               "existing": code_info["isExistingUser"]}
            st.info("首次使用需要验证一下手机号，验证码已经发出。")
            return
        _on_login_ok()
    except CloudError as e:
        st.error("没登上：" + e.message)


def _on_login_ok():
    st.session_state.pending_signup = None
    store = _store()
    res = store.sync_cloud(store.load())
    if res.get("memory"):
        st.session_state.mem_storage[memmod.MEM_KEY] = res["memory"]
    st.session_state.cloud.track("login", {"v": VERSION}, platform=SOURCE)
    st.success("登上了。星寻会记住你。")
    st.rerun()


def _render_signup_verify():
    pend = st.session_state.get("pending_signup")
    if not pend:
        return
    with st.form("verify_form"):
        st.caption("给 %s 发的验证码" % pend["phone"])
        code = st.text_input("验证码", max_chars=6)
        if st.form_submit_button("确定"):
            try:
                st.session_state.cloud.verify_code(
                    pend["phone"], code, pend["pwd"], pend["existing"], pend["vid"])
                _on_login_ok()
            except CloudError as e:
                st.error(e.message)


def _render_gate():
    """5 分钟免费体验到期后的门槛（与手机端 UsageGate 同语义）。"""
    st.markdown(theme.card(
        "免费体验时间到啦",
        [theme.p("你已经陪星寻聊了一会儿了。注册一下，星寻才能记住你——下次打开，它还认得。"),
         theme.kv("用手机号注册即可。登录状态只保留在当前这次浏览里，关掉页面就会退出（公共电脑上更安全）。")],
    ), unsafe_allow_html=True)
    with st.form("gate_login", clear_on_submit=False):
        phone = st.text_input("手机号", placeholder="13800000000")
        pwd = st.text_input("密码", type="password")
        c1, c2 = st.columns(2)
        go = c1.form_submit_button("登录 / 注册", width="stretch")
        if c2.form_submit_button("先看看我记住了什么", width="stretch"):
            st.session_state.page = "我的"
            st.rerun()
    if go:
        _do_login(phone, pwd)
    _render_signup_verify()


# ============================ 页面一：书写 ============================

def _page_index():
    ss = st.session_state
    diaries = _all_diaries()

    st.markdown(theme.sky_html(560), unsafe_allow_html=True)
    st.markdown(theme.hero("今天，心里是什么天气？", "点亮一支蜡烛，路就会往前长一点"),
                unsafe_allow_html=True)

    # 今日卡：已写 / 还没写
    today = datetime.now().strftime("%m-%d")
    today_items = [d for d in diaries
                   if datetime.fromtimestamp((d.get("ts") or 0) / 1000).strftime("%m-%d") == today]
    if today_items:
        st.markdown(theme.card("今天已经点过灯了",
                               [theme.p("今天写下了 %d 条。再写一条也可以，路会接着长。" % len(today_items))]),
                    unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="hsx-card"><div class="hsx-today"><div class="num">%d</div>'
            '<div class="txt">条记录已经在这条路上。<br>今天还没有点灯。</div></div></div>'
            % len(diaries), unsafe_allow_html=True)

    # ---- 写一条 ----
    st.markdown('<div class="hsx-sub">此刻心里在想什么？（想记心情就点一个，不想点也完全没关系）</div>',
                unsafe_allow_html=True)
    picked = ss.get("picked_mood", "")
    cols = st.columns(6)
    for i, (mid, _face, name, _color) in enumerate(theme.MOODS):
        on = picked == mid
        # aria-label = "选择 还不错" 等（help 参数同时生成 tooltip 与 aria-label）。
        # theme.CSS 里用 [aria-label^=选择] 定位这一行，强制六列不折行。
        if cols[i].button("●" if on else "○", key="mf_" + mid,
                          width="stretch", help=("取消选择 " if on else "选择 ") + name,
                          type=("primary" if on else "secondary")):
            ss.picked_mood = "" if on else mid
            st.rerun()
    if picked:
        st.caption("已选：%s（轻点再点一次可取消）" % moodcurve.mood_name(picked))

    with st.form("write_diary", clear_on_submit=True):
        content = st.text_area("写下来", height=120,
                               placeholder="写下来就好，不用讲道理。", label_visibility="collapsed")
        ok = st.form_submit_button("点亮蜡烛", type="primary", width="stretch")
    if ok:
        if not (content or "").strip() and not picked:
            st.warning("写一句，或者点一个心情都行。")
        else:
            _save_diary(content, picked)
            st.success("收好了。")
            st.rerun()

    # ---- 七日心情带 ----
    series = moodcurve.week_mood_series(diaries)
    st.markdown('<div class="hsx-sub">这七天</div>', unsafe_allow_html=True)
    st.markdown(theme.band(series), unsafe_allow_html=True)
    summary = moodcurve.week_mood_summary(series, len(diaries))
    st.markdown('<div class="hsx-faint">%s</div>' % _esc(moodcurve.week_mood_caption(summary)),
                unsafe_allow_html=True)

    # ---- 最近写的 ----
    st.markdown('<div class="hsx-sub">最近写的</div>', unsafe_allow_html=True)
    if not diaries:
        st.markdown(theme.card("", [theme.p("还没有。写一条试试。")]), unsafe_allow_html=True)
    else:
        for it in diaries[:8]:
            ts = it.get("ts") or it.get("createdAt") or 0
            when = time.strftime("%m-%d %H:%M", time.localtime(ts / 1000)) if ts else ""
            face = moodcurve.mood_face(it.get("mood", "")) or "·"
            st.markdown(
                '<div class="hsx-card mini"><p>%s %s</p><p class="kv">%s</p></div>'
                % (face, _esc(it.get("content") or "（只记了心情）"), _esc(when)),
                unsafe_allow_html=True)


def _save_diary(content, mood_id):
    ss = st.session_state
    ts = int(time.time() * 1000)
    entry = {"id": ts, "ts": ts, "content": (content or "").strip(), "mood": mood_id or ""}
    ss.diary_local = list(ss.get("diary_local", [])) + [entry]
    ss.picked_mood = ""
    if _signed_in():
        try:
            ss.cloud.db_insert("diary_entries", [{
                "client_id": "st_" + str(ts),
                "content": entry["content"],
                "source": "write",
                "created_at": datetime.fromtimestamp(ts / 1000).astimezone().isoformat(),
            }])
        except CloudError:
            pass
    ss.cloud.track("diary_create", {"mood": mood_id or "", "len": len(entry["content"])}, platform=SOURCE)


# ============================ 页面二：对话 ============================

def _page_chat():
    ss = st.session_state
    st.markdown(theme.sky_html(520), unsafe_allow_html=True)
    st.markdown(theme.hero("有人在听", "不评判 · 不打断 · 随时可以走"), unsafe_allow_html=True)

    if _gate_blocked():
        _render_gate()
        return

    # 形象：顶部中央（与手机端一致）。用 base64 内联 + text-align:center，
    # 因为 st.image 在窄列里始终左对齐，靠 CSS 拗不过它。
    import base64
    try:
        with open(_avatar_path(ss.avatar_id), "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        st.markdown('<div style="text-align:center;margin:2px 0 6px">'
                    '<img src="data:image/jpeg;base64,%s" alt="星寻" '
                    'style="width:132px;height:132px;object-fit:cover;border-radius:18px"/></div>'
                    % b64, unsafe_allow_html=True)
    except OSError:
        pass

    st.markdown(
        '<div class="hsx-faint" style="text-align:center">%s</div>'
        % ("星寻的回复来自云端大模型" if os.environ.get("HSX_LLM", "on") != "off"
           else "正在用本地陪伴"),
        unsafe_allow_html=True)

    if not ss.messages:
        st.markdown('<div class="hsx-sub">不知道从哪说起也没关系，点一句也行：</div>',
                    unsafe_allow_html=True)
        starters = companion.pick_starters()
        for i, s in enumerate(starters):
            if st.button(s, key="st_" + str(i), width="stretch"):
                ss.pending_prompt = s
                st.rerun()

    # 历史消息：用户右侧金气泡，星寻左侧深卡（对齐手机聊天页）
    for m in ss.messages:
        if m["role"] == "user":
            st.markdown('<div class="hsx-bubble-me">%s</div>' % _esc(m["text"]), unsafe_allow_html=True)
        else:
            st.markdown('<div class="hsx-bubble-ai">%s</div>' % _esc(m["text"]), unsafe_allow_html=True)
            if m.get("note"):
                st.markdown('<div class="hsx-who">· %s</div>' % _esc(m["note"]), unsafe_allow_html=True)

    b1, b2 = st.columns(2)
    if b1.button("求助", width="stretch"):
        card = companion.help_card()
        ss.cloud.track("referral_hotline", {"from": "chat_help"}, platform=SOURCE)
        st.markdown(theme.card(card["title"], [theme.p(card["text"])]), unsafe_allow_html=True)
        st.caption("12356 全国统一心理援助热线 · 24 小时 · 免费匿名；紧急情况请直接拨 120。")
    if b2.button("清空", width="stretch"):
        ss.messages = []
        ss.chat_state = companion.create_state()
        st.rerun()

    prompt = st.chat_input("说点什么…（不用组织语言）")
    if not prompt and ss.get("pending_prompt"):
        prompt = ss.pop("pending_prompt")

    if prompt:
        ss.messages.append({"role": "user", "text": prompt})
        st.markdown('<div class="hsx-bubble-me">%s</div>' % _esc(prompt), unsafe_allow_html=True)
        placeholder = st.empty()
        placeholder.markdown('<div class="hsx-bubble-ai">…</div>', unsafe_allow_html=True)
        text, note = _generate(prompt, placeholder)
        placeholder.markdown('<div class="hsx-bubble-ai">%s</div>' % _esc(text), unsafe_allow_html=True)
        if note and note != "extreme":
            st.markdown('<div class="hsx-who">· %s</div>' % _esc(note), unsafe_allow_html=True)
        ss.messages.append({"role": "ai", "text": text, "note": note})
        st.rerun()


# ============================ 页面三：我的 ============================

def _page_mine():
    ss = st.session_state
    st.markdown(theme.sky_html(420), unsafe_allow_html=True)
    st.markdown(theme.hero("我的", "星寻对我的了解，都在这儿"), unsafe_allow_html=True)

    # ---- 账号 ----
    if _signed_in():
        uid = ss.cloud.auth_uid()
        phone = ss.cloud.session.phone or ""
        masked = (phone[:3] + "****" + phone[-4:]) if len(phone) >= 11 else (phone or "手机号账号")
        st.markdown(theme.card("账号 · 已登录", [
            theme.p(masked),
            theme.kv("ID：%s" % uid),
            theme.kv("登录状态只保留在当前这次浏览里，关掉页面就会退出。"),
        ]), unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        if c1.button("退出登录", width="stretch"):
            ss.cloud.sign_out()
            st.rerun()
        if c2.button("同步一次云端", width="stretch"):
            r = _store().sync_cloud(_store().load())
            st.toast("同步过了" if r else "云端暂时连不上")
    else:
        st.markdown(theme.card("账号", [
            theme.p("注册一个账号，换设备也不丢记录。"),
            theme.kv("手机号 + 密码即可。小程序里用微信登录后绑定同一个手机号，记录就合在一起了。"),
        ]), unsafe_allow_html=True)
        with st.form("mine_login"):
            phone = st.text_input("手机号", key="mine_phone", placeholder="13800000000")
            pwd = st.text_input("密码", type="password", key="mine_pwd")
            if st.form_submit_button("登录 / 注册", width="stretch"):
                _do_login(phone, pwd)
        _render_signup_verify()

    # ---- 星寻的样子 ----
    st.markdown('<div class="hsx-sub">星寻的样子</div>', unsafe_allow_html=True)
    groups = av.AVATAR_GROUPS
    labels = [g["name"] + " · " + g["hint"] for g in groups]
    gsel = st.selectbox("分组", labels, key="avatar_group", label_visibility="collapsed")
    gid = groups[labels.index(gsel)]["id"]
    items = av.avatars_by_group(gid)
    cols = st.columns(4)
    for i, a in enumerate(items):
        with cols[i % 4]:
            st.image(_avatar_path(a["id"]), width="stretch")
            if st.button(("✓ " if ss.avatar_id == a["id"] else "") + a["name"],
                         key="av_" + a["id"], width="stretch"):
                ss.avatar_id = a["id"]
                st.rerun()

    # ---- 我的记录 ----
    diaries = _all_diaries()
    week_start = moodcurve.get_week_start(int(time.time() * 1000))
    week_n = sum(1 for d in diaries if (d.get("ts") or 0) >= week_start)
    m1, m2, m3 = st.columns(3)
    m1.metric("已写条数", len(diaries))
    m2.metric("本周条数", week_n)
    m3.metric("对话轮数", ss.get("mem_storage", {}).get(memmod.MEM_KEY, {}).get("count", {}).get("turns", 0)
              if ss.get("mem_storage") else 0)

    # ---- 星寻对我的了解 ----
    store = _store()
    mem = store.load()
    mem_text = store.render_for_prompt(mem)
    if not mem_text:
        st.markdown(theme.card("星寻对我的了解",
                               [theme.p("星寻还在认识你。聊上几次，这里就会慢慢有你。")]),
                    unsafe_allow_html=True)
    else:
        p = mem["profile"]
        lines = []
        if p["call"]:
            lines.append("<p>称呼：<b>%s</b></p>" % _esc(p["call"]))
        if p["facts"]:
            lines.append("<p>记得的事：%s</p>" % "；".join(_esc(x) for x in p["facts"]))
        if p["matters"]:
            lines.append("<p>一直在意的：%s</p>" % "、".join(_esc(x) for x in p["matters"]))
        if p["goals"]:
            lines.append("<p>想做还没做的：%s</p>" % "；".join(_esc(x) for x in p["goals"]))
        if mem["mood"]["tone"]:
            lines.append("<p>最近的状态：%s</p>" % _esc(mem["mood"]["tone"]))
        st.markdown(theme.card("星寻对我的了解 · 共 %d 轮" % mem["count"]["turns"], lines),
                    unsafe_allow_html=True)
        st.markdown('<div class="hsx-faint">这些只在你自己账户里，别人看不到。</div>',
                    unsafe_allow_html=True)
        if st.button("让星寻忘掉这些"):
            store.clear()
            store.clear_cloud()
            st.success("忘掉了。")
            st.rerun()

    # ---- 设置 ----
    st.markdown('<div class="hsx-sub">设置</div>', unsafe_allow_html=True)
    steps = [("小", 0.9), ("标准", 1.0), ("大", 1.15), ("特大", 1.3)]
    cols = st.columns(4)
    for i, (name, v) in enumerate(steps):
        if cols[i].button(("● " if abs(ss.font_scale - v) < 0.01 else "") + name,
                          key="fs_" + name, width="stretch"):
            ss.font_scale = v
            st.rerun()

    with st.expander("导出我的全部记录"):
        st.caption("导出的是你自己写的文字，用于换机或自己留档。不会上传到任何地方。")
        txt = _export_text(diaries)
        st.text_area("记录（可全选复制）", txt, height=200, label_visibility="collapsed")
        st.download_button("下载 txt", txt.encode("utf-8"), file_name="心与星寻_我的记录.txt",
                           mime="text/plain")

    with st.expander("数据与隐私说明"):
        st.markdown(
            "· 我们收集：手机号 / 微信标识（登录）、你写下的对话与日记（同步）、匿名使用统计（不含内容原文）。\n\n"
            "· 我们不收集：真实姓名、身份证、位置、通讯录、设备指纹。没有广告与第三方追踪。\n\n"
            "· 模型服务在**境内**（腾讯混元），你的文字不出境。\n\n"
            "· 你可以随时导出、清空、注销。注销会删除云端全部记录。\n\n"
            "· 14 岁以下需监护人知情同意后使用。\n\n"
            "· 心与星寻是情感陪伴与自我书写工具，**不是医疗服务**；不提供诊断、测评或治疗建议。"
            "遇到紧急情况请拨 120 或 12356。")

    st.markdown('<div class="hsx-about">心与星寻 v%s · 网页版 ｜ 手机版：App / 微信小程序<br>'
                '现阶段为内测，不推广</div>' % VERSION, unsafe_allow_html=True)


def _export_text(diaries):
    lines = ["心与星寻 · 我的记录导出", "导出时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
    for d in diaries:
        ts = d.get("ts") or 0
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000)) if ts else ""
        mood = moodcurve.mood_name(d.get("mood", "")) or ""
        lines.append("[%s]%s" % (when, (" " + mood) if mood else ""))
        lines.append(d.get("content") or "（只记了心情）")
        lines.append("")
    return "\n".join(lines)


# ============================ 入口 ============================

def _tabbar():
    """底部三格 tab（复刻 uni-app tabBar：书写 / 对话 / 我的）。

    key 前缀 tab_ 会落在 DOM 的 .st-key-tab_* class 上，
    theme.CSS 用它定位这一行并固定到底部（比额外包一层 div 稳）。
    """
    st.markdown('<div class="hsx-tabspace"></div>', unsafe_allow_html=True)
    cols = st.columns(3)
    cur = st.session_state.page
    for i, name in enumerate(PAGES):
        with cols[i]:
            if st.button("%s\n%s" % (PAGE_ICON[name], name), key="tab_" + name, width="stretch",
                         type=("primary" if cur == name else "secondary")):
                st.session_state.page = name
                st.rerun()


def render():
    light = os.environ.get("HSX_THEME", "dark").lower() == "light"
    st.markdown(theme.CSS, unsafe_allow_html=True)
    if light:
        st.markdown(theme.LIGHT_CSS, unsafe_allow_html=True)
    _boot()

    page = st.session_state.page
    if page == "书写":
        _page_index()
    elif page == "对话":
        _page_chat()
    else:
        _page_mine()

    _tabbar()
