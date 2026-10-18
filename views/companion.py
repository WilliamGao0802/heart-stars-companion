"""心与星寻 · 陪伴页（Streamlit 桌面版）。

与手机版（H5/小程序/App）共用同一套 core 业务逻辑：同一个人设提示词、
同一套情绪识别、同一套 RAG 知识卡（75 张）、同一套长期记忆与安全处置。

差异说明（写在明处，不假装一样）：
  · 界面是 Streamlit 桌面观感，不是手机版那套原生 UI；
  · 登录用「手机号 + 密码」（桌面浏览器拿不到微信授权，微信绑定仍只在手机端）。
"""

import html
import os
import time

import streamlit as st

from core import avatar as av
from core import chatflow, companion, safety
from core import memory as memmod
from core import moodcurve
from core.cloud import CloudClient, CloudError, memory_cloud

VERSION = "1.0.2"
GATE_SECONDS = int(os.environ.get("HSX_GATE_SECONDS", "300"))
SOURCE = "st"
AVATAR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "avatars")


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
    if "chat_state" not in ss:
        ss.chat_state = companion.create_state()
    if "mem_storage" not in ss:
        ss.mem_storage = {}
    if "avatar_id" not in ss:
        ss.avatar_id = av.DEFAULT_AVATAR_ID
    if "font_scale" not in ss:
        ss.font_scale = 1.0
    if "page" not in ss:
        ss.page = "对话"
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


# ============================ 样式 ============================

CSS = """
<style>
.stApp { background: radial-gradient(120% 80% at 50% 0%, #1b2440 0%, #131a2e 45%, #0e1322 100%); }
section[data-testid="stSidebar"] { background: rgba(20,27,47,.92); border-right: 1px solid rgba(255,214,143,.14); }
.hero { padding: 4px 0 12px; }
.hero h1 { font-size: 26px; color: #ffd68f; margin: 0 0 2px; letter-spacing: .5px; }
.hero p { color: #9aa6c4; font-size: 13px; margin: 0; }
.moodbar { display:flex; gap:6px; margin: 6px 0 2px; }
.moodbar div { flex:1; height:38px; border-radius:9px; display:flex; align-items:center; justify-content:center; font-size:15px; color:#0e1322; }
.ribbon { display:flex; gap:6px; margin:10px 0 4px; }
.ribbon .cell { flex:1; height:52px; border-radius:10px; border:1px solid rgba(255,255,255,.10);
  display:flex; flex-direction:column; align-items:center; justify-content:center; font-size:11px; color:#c9d2e8; }
.ribbon .cell b { font-size:15px; font-weight:600; }
.card { background: rgba(28,36,58,.72); border:1px solid rgba(255,214,143,.16);
  border-radius:14px; padding:14px 16px; margin: 8px 0; }
.card h4 { color:#ffd68f; margin:0 0 6px; font-size:15px; }
.card p { color:#c9d2e8; font-size:13px; margin:0 0 4px; line-height:1.7; }
.kv { color:#9aa6c4; font-size:12px; }
.tagline { color:#8e97ad; font-size:12px; }
div[data-testid="stChatMessage"] { background: transparent; }
.stButton button { border-radius: 20px; }
</style>
"""


# ============================ 聊天核心 ============================

def _report_alert(text, words):
    """极端内容上报（admin_alerts 表：RLS 允许任何身份 INSERT，不漏报）。"""
    try:
        st.session_state.cloud.db_insert("admin_alerts", [safety.build_alert(SOURCE, st.session_state.cloud.auth_uid(), text, words)])
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
            placeholder.markdown(full + " ▌")
        full = chatflow.finalize_reply(full, ask_mode)
        if not full:
            raise CloudError("空回复", "empty")
    except CloudError as e:
        # ③ 云端不可用 → 本地规则版兜底（安全网，不是故障）
        full = chatflow.local_fallback(prompt, st.session_state.chat_state)
        note = "云端暂时没连上，先用本地陪伴回你" + ("" if e.code in ("no_model", "gateway") else "")

    st.session_state.chat_state = companion.normalize_state(st.session_state.chat_state)

    # ④ 记忆落库 + 上云
    mem = store.save(mem)
    store.push_cloud(mem)
    if store.should_summarize(mem):
        res = store.summarize(st.session_state.messages, mem, lambda msgs: cli.chat_once(msgs, temperature=0.3))
        if res["changed"]:
            st.session_state.mem_storage[memmod.MEM_KEY] = res["memory"]
            store.push_cloud(res["memory"])

    # ⑤ 本轮小知识卡（危机不挂，危机卡是干预指引不是知识）
    kb_note = ""
    if analysis["mood"] != "crisis" and cards:
        kb_note = cards[0]["title"]

    _persist_msg("user", prompt)
    _persist_msg("ai", full)
    return full, (note or kb_note)


# ============================ 各个板块 ============================

def _render_gate():
    st.markdown('<div class="card"><h4>免费体验时间到啦</h4>'
                '<p>你已经陪星寻聊了一会儿了。注册一下，星寻才能记住你——下次打开，它还认得。</p>'
                '<p class="kv">用手机号注册即可。登录状态只保留在当前这次浏览里，关掉页面就会退出（公共电脑上更安全）。</p></div>',
                unsafe_allow_html=True)
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
                st.session_state.cloud.verify_code(pend["phone"], code, pend["pwd"], pend["existing"], pend["vid"])
                _on_login_ok()
            except CloudError as e:
                st.error(e.message)


def _page_chat():
    ss = st.session_state
    st.markdown('<div class="hero"><h1>心与星寻</h1><p>一个会记住你的树洞</p></div>', unsafe_allow_html=True)

    if _gate_blocked():
        _render_gate()
        _render_signup_verify()
        return

    if not ss.messages:
        st.markdown('<p class="tagline">不知道从哪说起也没关系，点一句也行：</p>', unsafe_allow_html=True)
        starters = companion.pick_starters()
        cols = st.columns(3)
        for i, s in enumerate(starters):
            if cols[i % 3].button(s, key="st_" + str(i), width="stretch"):
                ss.pending_prompt = s
                st.rerun()

    for m in ss.messages:
        if m["role"] == "user":
            with st.chat_message("user"):
                st.write(m["text"])
        else:
            with st.chat_message("assistant", avatar=_avatar_path(ss.avatar_id)):
                st.write(m["text"])
                if m.get("note"):
                    st.caption("· " + m["note"])

    c1, c2 = st.columns([1, 1])
    if c1.button("我想找真人说说话", width="stretch"):
        card = companion.help_card()
        with st.chat_message("assistant", avatar=_avatar_path(ss.avatar_id)):
            st.markdown("**" + card["title"] + "**")
            st.write(card["text"])
    if c2.button("清空这次对话", width="stretch"):
        ss.messages = []
        ss.chat_state = companion.create_state()
        st.rerun()

    prompt = st.chat_input("说点什么…（不用组织语言）")
    if not prompt and ss.get("pending_prompt"):
        prompt = ss.pop("pending_prompt")

    if prompt:
        with st.chat_message("user"):
            st.write(prompt)
        with st.chat_message("assistant", avatar=_avatar_path(ss.avatar_id)):
            placeholder = st.empty()
            text, note = _generate(prompt, placeholder)
            placeholder.write(text)
            if note and note != "extreme":
                st.caption("· " + note)
        ss.messages.append({"role": "user", "text": prompt})
        ss.messages.append({"role": "ai", "text": text, "note": note})
        st.rerun()


def _page_mood():
    ss = st.session_state
    st.markdown('<div class="hero"><h1>今夜的心情</h1><p>写一条，心情的起伏就会被看见</p></div>', unsafe_allow_html=True)

    st.markdown('<p class="tagline">此刻，你更接近哪一种？</p>', unsafe_allow_html=True)
    cols = st.columns(6)
    picked = ss.get("picked_mood", "")
    for i, m in enumerate(moodcurve.MOOD_FACES):
        label = ("● " if picked == m["id"] else "") + m["face"] + " " + m["name"]
        if cols[i].button(label, key="mf_" + m["id"], width="stretch"):
            ss.picked_mood = m["id"]
            st.rerun()

    with st.form("write_diary", clear_on_submit=True):
        content = st.text_area("写下来吧", height=110, placeholder="今天……")
        ok = st.form_submit_button("点亮这一条")
    if ok and (content or picked):
        _save_diary(content, picked)
        st.success("收好了。")
        st.rerun()

    _render_ribbon()
    _render_recent()


def _save_diary(content, mood_id):
    ss = st.session_state
    ts = int(time.time() * 1000)
    entry = {"id": ts, "ts": ts, "content": content or "", "mood": mood_id}
    ss.diary_local = ss.get("diary_local", [])
    ss.diary_local.append(entry)
    if _signed_in():
        try:
            ss.cloud.db_insert("diary_entries", [{"client_id": "st_" + str(ts), "content": content or "", "source": "write"}])
        except CloudError:
            pass


def _render_ribbon():
    store = _store()
    items = list(st.session_state.get("diary_local", []))
    if _signed_in():
        try:
            rows = st.session_state.cloud.db_select("diary_entries", {"select": "id,content,created_at"}, limit=200, order="created_at.desc")
            for r in rows:
                items.append({"id": r.get("id"), "content": r.get("content", ""), "mood": "", "createdAt": _ts_ms(r.get("created_at"))})
        except CloudError:
            pass

    series = moodcurve.week_mood_series(items)
    cells = []
    for d in series:
        color = moodcurve.mood_color(d["mood"]) if d["mood"] else moodcurve.EMPTY_COLOR
        face = moodcurve.mood_face(d["mood"]) or "·"
        cells.append('<div class="cell" style="background:%s"><b>%s</b>%s %s</div>'
                     % (color, face, d["label"], d["date"]))
    st.markdown('<div class="ribbon">' + "".join(cells) + "</div>", unsafe_allow_html=True)

    summary = moodcurve.week_mood_summary(series, len(items))
    st.markdown('<p class="tagline">%s</p>' % moodcurve.week_mood_caption(summary), unsafe_allow_html=True)
    share = moodcurve.share_text(summary)
    if share:
        st.text_area("这一周的分享文案（复制即可）", share, height=90)


def _render_recent():
    items = st.session_state.get("diary_local", [])
    st.markdown("#### 最近写的")
    if not items:
        st.caption("还没有。写一条试试。")
        return
    for it in reversed(items[-8:]):
        face = moodcurve.mood_face(it.get("mood", "")) or "·"
        st.markdown('<div class="card"><p>%s %s</p><p class="kv">%s</p></div>'
                    % (face, _esc(it.get("content") or "（只记了心情）"),
                       time.strftime("%m-%d %H:%M", time.localtime((it.get("ts") or 0) / 1000))),
                    unsafe_allow_html=True)


def _page_mine():
    ss = st.session_state
    st.markdown('<div class="hero"><h1>我的</h1><p>星寻对我的了解，都在这儿</p></div>', unsafe_allow_html=True)

    # 账号
    st.markdown("#### 账号")
    if _signed_in():
        uid = ss.cloud.auth_uid()
        st.markdown('<div class="card"><h4>已登录</h4><p class="kv">ID：%s</p><p class="kv">%s</p></div>'
                    % (_esc(uid), _esc(ss.cloud.session.phone or "手机号账号")), unsafe_allow_html=True)
        if st.button("退出登录"):
            ss.cloud.sign_out()
            st.query_params.clear()
            st.rerun()
    else:
        st.markdown('<div class="card"><p>还没登录——登录后星寻才能跨设备记住你。</p>'
                    '<p class="kv">手机号 + 密码即可；登录状态只保留在当前这次浏览里，关掉页面就会退出。</p></div>', unsafe_allow_html=True)
        with st.form("mine_login"):
            phone = st.text_input("手机号", key="mine_phone", placeholder="13800000000")
            pwd = st.text_input("密码", type="password", key="mine_pwd")
            if st.form_submit_button("登录 / 注册"):
                _do_login(phone, pwd)
        _render_signup_verify()

    # 形象
    st.markdown("#### 星寻的样子")
    groups = av.AVATAR_GROUPS
    gsel = st.selectbox("分组", [g["name"] + " · " + g["hint"] for g in groups], key="avatar_group")
    gid = groups[[g["name"] + " · " + g["hint"] for g in groups].index(gsel)]["id"]
    items = av.avatars_by_group(gid)
    cols = st.columns(4)
    for i, a in enumerate(items):
        with cols[i % 4]:
            st.image(_avatar_path(a["id"]), width="stretch")
            if st.button(("✓ " if ss.avatar_id == a["id"] else "") + a["name"], key="av_" + a["id"], width="stretch"):
                ss.avatar_id = a["id"]
                st.rerun()

    # 星寻对我的了解
    st.markdown("#### 星寻对我的了解")
    store = _store()
    mem = store.load()
    mem_text = store.render_for_prompt(mem)
    if not mem_text:
        st.markdown('<div class="card"><p>还是一片空白。聊过几次、星寻记住了东西之后，这里会长出来。</p></div>', unsafe_allow_html=True)
    else:
        p = mem["profile"]
        # 这些字段来自「用户原话 + 模型总结」，一律转义后再进 HTML。
        rows = []
        if p["call"]:
            rows.append("习惯怎么称呼你：<b>%s</b>" % _esc(p["call"]))
        if p["facts"]:
            rows.append("记得的事：%s" % "；".join(_esc(x) for x in p["facts"]))
        if p["matters"]:
            rows.append("你放不下的：%s" % "、".join(_esc(x) for x in p["matters"]))
        if p["goals"]:
            rows.append("你想做的：%s" % "；".join(_esc(x) for x in p["goals"]))
        if p["asks"]:
            rows.append("你要求过星寻的：%s" % "；".join(_esc(x) for x in p["asks"]))
        if mem["mood"]["tone"]:
            rows.append("最近的状态：%s" % _esc(mem["mood"]["tone"]))
        st.markdown('<div class="card"><h4>共 %d 轮对话</h4>%s</div>'
                    % (mem["count"]["turns"], "".join('<p>%s</p>' % r for r in rows)), unsafe_allow_html=True)
        if st.button("让星寻忘掉这些"):
            store.clear()
            store.clear_cloud()
            st.success("忘掉了。")
            st.rerun()

    # 字体大小（保留手机端的可调项）
    st.markdown("#### 字体大小")
    steps = [("小", 0.9), ("标准", 1.0), ("大", 1.15), ("特大", 1.3)]
    cols = st.columns(4)
    for i, (name, v) in enumerate(steps):
        if cols[i].button(("● " if abs(ss.font_scale - v) < 0.01 else "") + name, key="fs_" + name, width="stretch"):
            ss.font_scale = v
            st.rerun()

    st.markdown('<p class="kv">心与星寻 · Streamlit 桌面版 v%s ｜ 手机端另有 H5 / 小程序 / App 三个版本</p>' % VERSION,
                unsafe_allow_html=True)


def _ts_ms(s):
    if not s:
        return 0
    try:
        from datetime import datetime, timezone, timedelta
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


# ============================ 入口 ============================

def render():
    st.markdown(CSS, unsafe_allow_html=True)
    _boot()

    with st.sidebar:
        st.markdown('<div class="hero"><h1>心与星寻</h1><p>v%s</p></div>' % VERSION, unsafe_allow_html=True)
        st.image(_avatar_path(st.session_state.avatar_id), width=96)
        st.session_state.page = st.radio("去哪儿", ["对话", "心情", "我的"],
                                         index=["对话", "心情", "我的"].index(st.session_state.page))
        if _signed_in():
            st.caption("已登录：" + (st.session_state.cloud.session.phone or st.session_state.cloud.auth_uid()[:10]))
        else:
            st.caption("未登录（前 %d 分钟免费体验）" % (GATE_SECONDS // 60))
        st.divider()
        st.caption("手机版：heart-and-stars.app.workbuddy.host")
        st.caption("手机号+密码登录；微信绑定请用手机端")

    page = st.session_state.page
    if page == "对话":
        _page_chat()
    elif page == "心情":
        _page_mood()
    else:
        _page_mine()
