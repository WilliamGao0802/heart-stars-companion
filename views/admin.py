"""心与星寻 · 管理后台（Streamlit）。

只有管理员能进：邮箱 + 密码（写在 Streamlit Secrets 里，不进代码库）。
数据全部经服务端 RPC 读取（密钥在请求体），浏览器拿不到任何库凭据。

功能（参考成熟后台的信息架构）：
  总览       KPI 卡片 + 注册趋势 + 数据分布 + 最近动态
  用户管理   搜索 / 排序 / 全量导出 CSV / 点进单个用户
  用户详情   账号、资料、长期记忆、对话记录、日记、路灯
  校方视图   只出聚合数、不含个人级内容（P12 师生隔离），可导出给老师
  警报中心   危机台账：触发→响应→转介→关闭 四状态 + 时间线 + 导出
  回应库     小程序端 116 条陪伴文案，按情绪/话题筛选
  系统自检   接口连通性 + 各表读数 + 密钥校验
"""

import hmac
import html as html_lib
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from core.admin import AdminAPI, AdminError

VERSION = "1.3.0"

# 侧栏功能清单。顺序即信息架构：先看总量，再看结构（占比/时段），
# 然后才是「校方视图」这类对外口径页，最后是逐人排查类页面。
PAGES = [
    "总览",
    "周度看板",
    "数量占比",
    "校方视图",
    "用户管理",
    "用户详情",
    "警报中心",
    "回应库",
    "系统自检",
]


# ============================ 输出转义 ============================
#
# 后台会把**用户原文**（对话、日记、警报）贴进 HTML 卡片里。如果直接插，
# 用户只要发一条 `<img src=x onerror=...>` 就能在管理员浏览器里执行脚本 ——
# 这是典型的存储型 XSS，且攻击者不需要任何账号权限。
# 所以：凡是进 unsafe_allow_html 的用户数据，一律先过 _esc()。

def _esc(v, br=True):
    t = html_lib.escape(str(v if v is not None else ''), quote=True)
    return t.replace('\n', '<br>') if br else t


# CSV 公式注入：Excel/Sheets 会把 = + - @ 开头的单元格当公式执行。
#   导出的表是给管理员看的，一旦被当成公式，轻则错乱，重则外连取数。
_RISKY_PREFIX = ('=', '+', '-', '@', '\t', '\r')


def _csv_safe(df):
    def fix(v):
        if isinstance(v, str) and v[:1] in _RISKY_PREFIX:
            return "'" + v
        return v
    return df.apply(lambda col: col.map(fix) if hasattr(col, 'map') else col.apply(fix))


# ============================ 登录 ============================
#
# 安全约定（v1.0.1 起，务必遵守）：
#   · 代码里**不再有任何默认口令**。缺配置就直接拒绝进入，而不是退化成弱口令。
#   · 口令只从 Streamlit Secrets / 环境变量读取，绝不写进仓库。
#   · 登录失败有递增锁定；登录成功有 30 分钟闲置超时。
#   · 比较用 hmac.compare_digest（定长比较，避免按字符早退泄露长度信息）。

MAX_TRIES = 5                 # 连续失败 5 次后开始锁定
LOCK_BASE_SEC = 60            # 首次锁 60 秒，之后翻倍
IDLE_TIMEOUT_SEC = 30 * 60    # 闲置 30 分钟自动登出

MISSING_CREDS_HINT = (
    "未配置管理员凭据。请在 `.streamlit/secrets.toml`（本地）或 Streamlit Cloud 的 Secrets（线上）"
    "里填写 `[admin]` 段的 email / password / db_key，然后重启应用。"
)


def _sec():
    try:
        return dict(st.secrets.get("admin", {}))
    except Exception:
        return {}


def _creds():
    """返回 (email, password, db_key)；任一缺失返回 (None, None, None)。"""
    sec = _sec()
    email = sec.get("email") or os.environ.get("HSX_ADMIN_EMAIL")
    pwd = sec.get("password") or os.environ.get("HSX_ADMIN_PASSWORD")
    key = sec.get("db_key") or os.environ.get("HSX_ADMIN_KEY")
    if not (email and pwd and key):
        return None, None, None
    return str(email), str(pwd), str(key)


def _safe_eq(a, b):
    """定长比较：长度不同也走一次比较，避免计时侧信道。"""
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


# 失败计数必须放在**进程级**，不能只放 session_state。
#   原因：st.session_state 是按浏览器会话隔离的，攻击者清一下 cookie / 开个无痕窗口，
#   计数就归零了 —— 「5 次失败锁 60 秒」等于没锁，可以无限撞库。
#   这里用 st.cache_resource 拿一个跨会话共享的 dict，并按来源 IP 分桶。
@st.cache_resource
def _fail_store():
    return {"by_ip": {}, "global": {"fails": 0, "until": 0.0}}


def _client_key():
    """来源 IP（拿不到就退化成全局桶）。"""
    try:
        ip = getattr(st.context, "ip_address", "") or ""
        return ip if isinstance(ip, str) and ip else "global"
    except Exception:
        return "global"


def _bucket():
    store = _fail_store()
    k = _client_key()
    if k == "global":
        return store["global"]
    return store["by_ip"].setdefault(k, {"fails": 0, "until": 0.0})


def _lock_left():
    b = _bucket()
    left = float(b.get("until") or 0) - time.time()
    return int(left) if left > 0 else 0


def _record_failure():
    b = _bucket()
    b["fails"] = int(b.get("fails") or 0) + 1
    n = b["fails"]
    if n >= MAX_TRIES:
        extra = n - MAX_TRIES
        b["until"] = time.time() + LOCK_BASE_SEC * (2 ** min(extra, 5))


def _clear_failures():
    b = _bucket()
    b["fails"] = 0
    b["until"] = 0.0


def _login_screen():
    st.markdown('<div class="hero"><h1>管理后台</h1><p>心与星寻 · 仅管理员可进</p></div>', unsafe_allow_html=True)
    ok_email, ok_pwd, _ = _creds()
    if not ok_email:
        st.error(MISSING_CREDS_HINT)
        st.stop()

    left, right = st.columns([1, 1.2])
    with left:
        left_sec = _lock_left()
        if left_sec:
            st.warning("尝试次数过多，请 %d 秒后再试。" % left_sec)
        with st.form("admin_login"):
            email = st.text_input("管理员邮箱")
            pwd = st.text_input("密码", type="password")
            submitted = st.form_submit_button("进入", width="stretch",
                                              disabled=bool(left_sec))
        if submitted:
            # 两个条件都算完再判断，失败信息统一，不透露"是邮箱错还是密码错"
            email_ok = _safe_eq(email.strip().lower(), ok_email.lower())
            pwd_ok = _safe_eq(pwd, ok_pwd)
            if email_ok and pwd_ok:
                _clear_failures()
                st.session_state.admin_ok = True
                st.session_state.admin_seen = time.time()
                st.rerun()
            else:
                _record_failure()
                left_sec = _lock_left()
                if left_sec:
                    st.error("邮箱或密码不对。已暂时锁定，请 %d 秒后再试。" % left_sec)
                else:
                    st.error("邮箱或密码不对")
    with right:
        st.markdown('<div class="card"><h4>这里能做什么</h4>'
                    '<p>· 看用户规模与增长趋势</p>'
                    '<p>· 查单个用户的资料、长期记忆与对话记录</p>'
                    '<p>· 接收极端内容警报，第一时间知道谁需要帮助</p>'
                    '<p>· 浏览小程序端 116 条陪伴文案</p></div>', unsafe_allow_html=True)
        st.caption("账号密码只从 Secrets 读取，不写在代码里。闲置 30 分钟会自动退出。")


# ============================ 小工具 ============================

def _api():
    if "admin_api" not in st.session_state:
        key = _creds()[2]
        if not key:
            st.error(MISSING_CREDS_HINT)
            st.stop()
        st.session_state.admin_api = AdminAPI(key)
    return st.session_state.admin_api


def _run(fn, *a, **kw):
    try:
        return fn(*a, **kw), None
    except AdminError as e:
        return None, e.friendly


def _fmt_dt(s):
    if not s:
        return ""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(s)[:16]


def _days_ago(n):
    return datetime.now().astimezone() - timedelta(days=n)


# ============================ 周度看板 ============================
# 数据源：analytics_events 埋点表（客户端 fire-and-forget 写入），admin_metrics RPC 聚合。
# 指标口径与《生死线 03 数据埋点》一致：次日/7 日留存、周活跃、人均对话轮次、
# 人均日记篇数、危机触发（分级）与转介闭环数。

def _page_metrics(api):
    """周度看板：严格按《马老师汇报》P9 的五个指标组织，外加 P14 的止损线。

    为什么要把指标顺序钉死：汇报里这五个是**对外承诺**的测量口径，
    老师会拿着这张表问「现在的数是多少」。所以页面结构必须与 PPT 一一对应，
    不能按"好看"重排，也不能偷偷换口径（换口径 = 换承诺）。

    主指标是「第 8 周主动周留存率」——注意是**主动**：一周内跨 ≥2 个不同日期
    打开才算，一天里反复打开不算。这是我们跟老师说过的定义，看板上要写出来。
    """
    m, err = _run(api.metrics)
    if err:
        st.error("周度看板暂不可用：%s" % _esc(err))
        st.caption("提示：需要云端已部署 admin_metrics RPC，且客户端已升级到带埋点的版本。")
        return

    st.subheader("周度看板")
    st.caption("口径与《汇报》P9 的五个指标一一对应。改口径等于改对外承诺，"
               "所以每一项下面都写清了分子分母。")

    # ---------- 主指标 ----------
    st.markdown("##### 主指标：主动周留存率（阈值 ≥ 25%）")
    wr = m.get("week_retention", 0) or 0
    base = m.get("week_retention_base", 0) or 0
    kept = m.get("week_retention_kept", 0) or 0
    k1, k2, k3 = st.columns([1, 1, 2])
    k1.metric("主动周留存", "%s%%" % wr)
    k2.metric("上周活跃 / 本周主动回来", "%d / %d" % (base, kept))
    with k3:
        if base == 0:
            st.caption("还没有满一周的 cohort——等第一周数据跑完再判断。")
        elif wr >= 25:
            st.success("达到阈值（≥25%%）：%s%%" % wr)
        else:
            st.warning("低于阈值（<25%%）：%s%% —— 按 P14 止损线要复盘留存，先别加大推广。" % wr)
    st.caption("分子 = 本周跨 ≥2 个不同日期打开的人；分母 = 上周有过打开的人。"
               "「主动」指跨天回来，一天里反复点开不算。")

    # ---------- 其余四个指标 ----------
    st.markdown("##### 指标 2–5")
    c1, c2, c3, c4 = st.columns(4)
    act = m.get("activation_7d", 0) or 0
    c1.metric("首周激活率（止损线 <30%）", "%s%%" % act)
    c2.metric("日均对话轮次", m.get("turns_per_day", 0))
    c3.metric("情绪记录连续性（天/周）", m.get("diary_days", 0))
    l2, l3 = m.get("crisis_l2_7d", 0) or 0, m.get("crisis_l3_7d", 0) or 0
    c4.metric("安全事件（L2+L3，7 天）", l2 + l3)

    st.caption(
        "② 首周激活率 = 注册后 7 天内写过至少一篇日记的人 ÷ 满 7 天的 cohort（%d / %d）。"
        "注意写日记不要求先登录，匿名期写的人不进分子——这是**偏保守**的口径。\n\n"
        "③ 日均对话轮次 = 近 7 天 chat_turn 总数 ÷ 7。\n\n"
        "④ 情绪记录连续性 = 本周写过日记的人，人均记录了几天（满分 7）。\n\n"
        "⑤ 安全事件 = L2（求助卡片）+ L3（阻断页）触发次数。"
        % (m.get("activation_kept", 0) or 0, m.get("activation_base", 0) or 0)
    )

    # ---------- 活跃与趋势 ----------
    st.markdown("##### 活跃规模")
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("日活 DAU", m.get("dau", 0))
    d2.metric("周活 WAU", m.get("wau", 0))
    d3.metric("月活 MAU", m.get("mau", 0))
    d4.metric("人均对话轮次（周）", m.get("avg_turns", 0))

    plat = m.get("platforms") or {}
    if plat:
        order = ["h5", "mp", "app"]
        names = {"h5": "网页 H5", "mp": "微信小程序", "app": "安卓 App", "unknown": "未标注"}
        cols = st.columns(len(plat))
        for i, k in enumerate(sorted(plat, key=lambda x: order.index(x) if x in order else 9)):
            cols[i].metric(names.get(k, k), plat[k])
        st.caption("近 7 天分端活跃人数——汇报里「三端都有量」就靠这一行。")

    st.markdown("##### 每日活跃（近 14 天）")
    series = m.get("trend") or []
    if series:
        df = pd.DataFrame(series).rename(columns={"d": "日期", "n": "活跃用户"})
        df["日期"] = pd.to_datetime(df["日期"])
        st.bar_chart(df.set_index("日期"))
    else:
        st.caption("还没有埋点数据——等客户端带埋点的版本上线后自动开始积累。")

    # ---------- 安全闭环 + 台账 ----------
    st.markdown("##### 安全事件与转介闭环（近 7 天）")
    a1, a2, a3, a4 = st.columns(4)
    a1.metric("L2 触发（求助卡片）", l2)
    a2.metric("L3 触发（阻断页）", l3)
    a3.metric("转介动作总数", m.get("referral_7d", 0))
    a4.metric("转介率", "%s%%" % (m.get("referral_rate", 0) or 0))
    st.caption("转介率 = 转介动作数 ÷ (L2 + L3) 触发数。用户的转介动作是「点了哪个求助入口」，"
               "不代表真的拨通了——真实跟进看下面的台账。")

    st.markdown("##### 危机台账（P10：老师可随时抽查）")
    t1, t2, t3 = st.columns(3)
    t1.metric("未关闭（open/acked/referred）", m.get("alert_open", 0))
    t2.metric("已关闭（closed）", m.get("alert_closed", 0))
    t3.metric("响应时长中位数（分钟）", m.get("alert_median_minutes", 0))
    st.caption("响应时长 = 从触发到「已响应」的中位数，只统计近 30 天且已响应的记录。"
               "推进状态去「警报中心」——每条都能看到触发 → 响应 → 转介 → 关闭的时间线。")
    st.caption("数据生成时间：%s" % _esc(m.get("generated_at", "-")))


# ============================ 总览 ============================

def _page_overview(api):
    stats, err = _run(api.stats)
    if err:
        st.error(err)
        return
    stats = stats or {}

    st.markdown("### 总览")
    c = st.columns(4)
    c[0].metric("注册用户", stats.get("users", 0), help="不含 administrator 账号")
    c[1].metric("今日新增", stats.get("users_new_1d", 0))
    c[2].metric("近 7 天活跃", stats.get("active_7d", 0), help="7 天内有对话记录的用户数")
    c[3].metric("极端内容警报", stats.get("alerts", 0))

    c = st.columns(4)
    c[0].metric("对话消息", stats.get("messages", 0))
    c[1].metric("日记（蜡烛）", stats.get("diaries", 0))
    c[2].metric("周记（路灯）", stats.get("lamps", 0))
    c[3].metric("长期记忆档案", stats.get("memory", 0))

    st.caption("最近一条对话：%s ｜ 回应库文案：%d 条 ｜ 微信绑定：%d 条"
               % (_fmt_dt(stats.get("last_message_at")) or "—", stats.get("library", 0), stats.get("bindings", 0)))

    users, uerr = _run(api.users)
    if uerr:
        st.warning(uerr)
        return
    users = users or []

    st.markdown("#### 注册趋势")
    if users:
        df = pd.DataFrame(users)
        df["注册日"] = df["created_at"].apply(lambda s: str(s)[:10])
        trend = df.groupby("注册日").size().reset_index(name="新增").sort_values("注册日")
        st.bar_chart(trend.set_index("注册日"), height=220)

        col1, col2 = st.columns([1, 1])
        with col1:
            st.markdown("#### 账号来源")
            src = df["provider"].value_counts().rename(index={"wechat": "微信", "phone": "手机号", "unknown": "未知"})
            st.dataframe(src.rename("人数").reset_index().rename(columns={"index": "来源"}),
                         width="stretch", hide_index=True)
        with col2:
            st.markdown("#### 活跃度分布")
            live = pd.DataFrame({
                "指标": ["有对话", "有日记", "有周记", "有长期记忆"],
                "人数": [int(df["msg_count"].astype(int).gt(0).sum()),
                       int(df["diary_count"].astype(int).gt(0).sum()),
                       int(df["lamp_count"].astype(int).gt(0).sum()),
                       int(df["turns"].astype(int).gt(0).sum())],
            })
            st.dataframe(live, width="stretch", hide_index=True)


# ============================ 用户管理 ============================

def _page_users(api):
    st.markdown("### 用户管理")
    q = st.text_input("搜索（手机号 / 昵称 / uid）", placeholder="输入后回车")
    users, err = _run(api.users, q)
    if err:
        st.error(err)
        return
    users = users or []

    if not users:
        st.info("没有匹配的用户。")
        return

    df = pd.DataFrame(users)
    show = pd.DataFrame({
        "选": False,
        "用户": [u["nick_name"] or (u["phone"] or u["uid"][:14]) for u in users],
        "手机号": [u["phone"] or "—" for u in users],
        "来源": [{"wechat": "微信", "phone": "手机号"}.get(u["provider"], "未知") for u in users],
        "注册时间": [_fmt_dt(u["created_at"]) for u in users],
        "对话": [u["msg_count"] for u in users],
        "日记": [u["diary_count"] for u in users],
        "周记": [u["lamp_count"] for u in users],
        "轮数": [u["turns"] for u in users],
        "星币": [u["coins"] for u in users],
        "会员": ["是" if u["is_member"] else "否" for u in users],
        "uid": [u["uid"] for u in users],
    })
    st.caption("共 %d 位用户" % len(users))
    edited = st.data_editor(show, width="stretch", hide_index=True, height=420,
                            column_config={
                                "选": st.column_config.CheckboxColumn(width="small"),
                                "uid": st.column_config.TextColumn(width="medium"),
                            },
                            disabled=[c for c in show.columns if c != "选"])
    picked = edited[edited["选"] == True] if "选" in edited else pd.DataFrame()
    if not picked.empty:
        st.session_state.admin_uid = picked.iloc[0]["uid"]
        st.session_state.admin_page = "用户详情"
        st.rerun()

    st.download_button("导出全部用户 CSV",
                       _csv_safe(show.drop(columns=["选", "uid"])).to_csv(index=False).encode("utf-8-sig"),
                       file_name="heart-stars-users.csv", mime="text/csv")

    st.divider()
    st.markdown("#### 直接打开某个用户")
    opts = {("%s ｜ %s ｜ %s" % (u["nick_name"] or "未起名", u["phone"] or u["uid"][:16], _fmt_dt(u["created_at"]))): u["uid"] for u in users}
    sel = st.selectbox("选择用户", ["—"] + list(opts.keys()))
    if sel != "—":
        st.session_state.admin_uid = opts[sel]
        st.session_state.admin_page = "用户详情"
        st.rerun()


# ============================ 用户详情 ============================

def _page_detail(api):
    st.markdown("### 用户详情")
    uid = st.session_state.get("admin_uid")
    if not uid:
        st.info("先在「用户管理」里选一个用户。")
        return
    if st.button("← 返回用户列表"):
        st.session_state.admin_page = "用户管理"
        st.rerun()

    d, err = _run(api.user_detail, uid)
    if err:
        st.error(err)
        return
    d = d or {}
    acc = d.get("account") or {}
    prof = d.get("profile") or {}
    mem = d.get("memory") or {}

    c = st.columns(4)
    c[0].metric("对话消息", len(d.get("messages") or []))
    c[1].metric("日记", len(d.get("diaries") or []))
    c[2].metric("周记", len(d.get("lamps") or []))
    c[3].metric("累计轮数", mem.get("turn_count", 0))

    st.markdown('<div class="card"><h4>账号</h4>'
                '<p class="kv">uid：%s</p><p class="kv">手机号：%s</p>'
                '<p class="kv">注册：%s ｜ 最近登录：%s</p></div>'
                % (_esc(uid), _esc(acc.get("phone") or "—"),
                   _esc(_fmt_dt(acc.get("created_at"))), _esc(_fmt_dt(acc.get("last_login")))),
                unsafe_allow_html=True)

    t1, t2, t3, t4 = st.tabs(["资料", "长期记忆", "对话记录", "日记 / 周记"])

    with t1:
        if prof:
            st.json(prof, expanded=True)
        else:
            st.caption("这位用户还没有个人资料行（可能是纯手机号新账号）。")

    with t2:
        if mem:
            st.markdown("**档案（注入给模型的版本）**")
            st.json(mem.get("profile") or {}, expanded=True)
            st.markdown("**近期状态**")
            st.json(mem.get("mood") or {}, expanded=True)
            st.caption("累计 %s 轮 ｜ 上次摘要于第 %s 轮 ｜ 更新于 %s"
                       % (mem.get("turn_count"), mem.get("summarized_at"), _fmt_dt(mem.get("updated_at"))))
        else:
            st.caption("还没有长期记忆档案（聊够 10 轮才会生成）。")

    with t3:
        msgs = d.get("messages") or []
        if not msgs:
            st.caption("还没有对话记录。")
        for m in reversed(msgs):
            who = "用户" if m["role"] == "user" else "星寻"
            st.markdown('<div class="card"><p class="kv">%s ｜ %s</p><p>%s</p></div>'
                        % (_esc(who), _esc(_fmt_dt(m.get("created_at"))), _esc(m.get("content", ""))),
                        unsafe_allow_html=True)
        if msgs:
            st.download_button("导出这位用户的对话 CSV",
                               _csv_safe(pd.DataFrame(msgs)).to_csv(index=False).encode("utf-8-sig"),
                               file_name="user-%s-chat.csv" % str(uid)[:10], mime="text/csv")

    with t4:
        diaries = d.get("diaries") or []
        lamps = d.get("lamps") or []
        st.markdown("**日记（蜡烛）**")
        for x in diaries:
            st.markdown('<div class="card"><p class="kv">%s ｜ %s</p><p>%s</p></div>'
                        % (_esc(_fmt_dt(x.get("created_at"))), _esc(x.get("source") or "write"),
                           _esc(x.get("content") or "")),
                        unsafe_allow_html=True)
        if not diaries:
            st.caption("没有日记。")
        st.markdown("**周记（路灯）**")
        for x in lamps:
            st.markdown('<div class="card"><p class="kv">%s ｜ %s ｜ 本周 %s 条</p><p>%s</p></div>'
                        % (_esc(_fmt_dt(x.get("created_at"))), _esc(x.get("week_label") or ""),
                           _esc(x.get("week_count") or 0), _esc(x.get("content") or "")),
                        unsafe_allow_html=True)
        if not lamps:
            st.caption("没有周记。")


# ============================ 警报中心 ============================

ALERT_STATUS = {
    "open": ("待响应", "#c0392b"),
    "acked": ("已响应", "#d68910"),
    "referred": ("已转介", "#2471a3"),
    "closed": ("已关闭", "#1e8449"),
}

ALERT_NEXT = {
    "open": [("acked", "标记已响应")],
    "acked": [("referred", "标记已转介"), ("closed", "直接关闭")],
    "referred": [("closed", "完成关闭")],
    "closed": [],
}

REFERRAL_LABEL = {
    "hotline": "12356 热线", "emergency": "120 紧急",
    "school": "学校心理老师", "guardian": "监护人", "": "（未记录）",
}


def _page_alerts(api):
    """警报中心：从「只读列表」升级成可以推进的台账。

    为什么后台需要写能力：汇报 P7 承诺「每次 L2/L3 触发都留档」、P10 承诺
    「触发→响应→转介→关闭 的时间线，老师可随时抽查」。只有一条 created_at
    的记录不叫留档——必须有谁、什么时候、做了什么。

    写库严格收口：只走 admin_alert_update 这一个 RPC，只改四个白名单字段，
    时间戳由服务端落（客户端传时间可被伪造），已关闭的不可再改。
    """
    st.markdown("### 警报中心")
    st.caption("危机内容上报的台账。系统检测到 L2/L3 会立刻停止对话、不保存该条原文，"
               "并把账号、原文与分级推到这里。这里也是这条记录从「触发」走到「关闭」的完整时间线。")
    alerts, err = _run(api.alerts)
    if err:
        st.error(err)
        return
    alerts = alerts or []

    if not alerts:
        st.success("目前没有警报。")
        st.caption("台账为空是好事，但也意味着还没有真实危机数据可用于检验流程——"
                   "建议正式启动前做一次演练（写一条测试记录，走完四个状态，再删掉）。")
        return

    df = pd.DataFrame(alerts)
    for col, default in (("status", "open"), ("handler", ""), ("note", ""), ("referral_path", "")):
        if col not in df.columns:
            df[col] = default
    df["时间"] = df["created_at"].apply(_fmt_dt)
    df["命中词"] = df["words"].apply(lambda s: "、".join([w for w in str(s).split(",") if w]))
    df["分级"] = df["words"].apply(lambda s: str(s)[:2] if str(s)[:2] in ("L2", "L3") else "L2")
    df["状态"] = df["status"].map(lambda k: ALERT_STATUS.get(k, (k, "#555"))[0])

    pending = int((df["status"] != "closed").sum())
    if pending:
        st.error("有 %d 条未关闭（其中 %d 条还没人响应）"
                 % (pending, int((df["status"] == "open").sum())))
    else:
        st.success("全部 %d 条警报都已关闭归档。" % len(df))

    f1, f2, f3 = st.columns([1, 1, 2])
    with f1:
        ssel = st.selectbox("按状态筛选", ["全部", "待响应", "已响应", "已转介", "已关闭"])
    with f2:
        lsel = st.selectbox("按分级筛选", ["全部", "L3", "L2"])
    with f3:
        st.caption("L3 = 方式/计划/进行中（阻断页）；L2 = 明确表达（求助卡片）。"
                   "命中词前缀就是分级编码，与客户端上报口径一致。")

    view = df.copy()
    if ssel != "全部":
        view = view[view["状态"] == ssel]
    if lsel != "全部":
        view = view[view["分级"] == lsel]

    st.dataframe(view[["时间", "分级", "状态", "user_id", "source", "命中词", "content", "handler", "note"]].rename(
        columns={"user_id": "用户", "source": "来源", "content": "原话",
                 "handler": "处理人", "note": "处置备注"}),
        width="stretch", hide_index=True, height=320)
    st.download_button("导出台账 CSV", _csv_safe(df.drop(columns=["words"], errors="ignore"))
                       .to_csv(index=False).encode("utf-8-sig"),
                       file_name="heart-stars-alerts.csv", mime="text/csv")

    # ---------------- 单条推进 ----------------
    st.markdown("#### 推进处置（逐条留痕）")
    st.caption("选一条记录，按 触发 → 已响应 → 已转介 → 已关闭 推进。"
               "时间戳由服务端自动记录，处理人和备注会一起写进台账。")

    open_rows = [r for r in alerts if r.get("status") != "closed"]
    if not open_rows:
        st.info("所有记录都已关闭。已关闭的台账为归档，不能再改。")
    else:
        labels = {}
        for r in open_rows:
            lv = str(r.get("words") or "")[:2]
            labels["#%s · %s · %s · %s" % (r.get("id"), lv, _fmt_dt(r.get("created_at")),
                                           str(r.get("content") or "")[:18])] = r
        pick = st.selectbox("选择要处理的记录", list(labels.keys()))
        row = labels[pick]
        cur = row.get("status") or "open"
        st.markdown("当前状态：**%s**　｜　命中词：`%s`"
                    % (ALERT_STATUS.get(cur, (cur, ""))[0], _esc(row.get("words") or "-")))

        # 时间线
        tl = [("触发", row.get("created_at")), ("已响应", row.get("acked_at")),
              ("已转介", row.get("referred_at")), ("已关闭", row.get("closed_at"))]
        st.markdown(" · ".join(["**%s** %s" % (n, _fmt_dt(t) if t else "—") for n, t in tl]))

        with st.form("alert_advance"):
            c1, c2 = st.columns(2)
            handler = c1.text_input("处理人", value=row.get("handler") or "",
                                    placeholder="例如：马老师 / 研究者（本人）")
            path = c2.selectbox("转介去向（转介时填）", list(REFERRAL_LABEL.keys()),
                                index=list(REFERRAL_LABEL.keys()).index(
                                    row.get("referral_path") or ""))
            note = st.text_area("处置备注", value=row.get("note") or "",
                                placeholder="做了什么、对方怎么回应、下一步。这段会同步写入台账。",
                                height=90)
            opts = ALERT_NEXT.get(cur, [])
            target = st.selectbox("推进到", [t for t, _ in opts] or ["（已关闭，不可再改）"],
                                  format_func=lambda k: dict(opts).get(k, k))
            ok = st.form_submit_button("提交", type="primary",
                                       disabled=(not opts or not handler.strip()))

        if not opts:
            st.caption("这条已经关闭了。需要修改结论的话，请在别处留一份说明——台账本身不抹改。")
        elif not handler.strip():
            st.caption("提交前请填「处理人」——台账要能回答「谁处理了这个警报」。")

        if ok and opts and handler.strip():
            res, rerr = _run(lambda: api.alert_update(row.get("id"), target,
                                                      handler.strip(), note.strip(), path))
            if rerr:
                st.error("提交失败：%s" % rerr)
            else:
                st.success("已更新为「%s」。刷新后可在时间线里看到。" % ALERT_STATUS[target][0])
                st.rerun()

    st.markdown("#### 处置原则（与《危机响应 SOP》一致）")
    st.markdown('<div class="card">'
                '<p>1. <b>先看分级</b>：L3 是方式/计划/正在受伤害，优先级最高，先确认人是否安全，再谈其他。</p>'
                '<p>2. <b>不自行判断严重程度</b>：研究者不做临床判断，触发即按流程走；是否严重由专业人员判断。</p>'
                '<p>3. <b>告知优先</b>：L2/L3 按流程告知监护人（研究者经手）；L3 同时告知指导教师。</p>'
                '<p>4. <b>每一步都留痕</b>：处理人、时间、转介去向、对方回应，全部写进备注——这是老师抽查的依据。</p>'
                '<p>5. <b>关闭有条件</b>：确认当事人已获得真人支持（热线/学校/家长三者之一）才关闭；'
                '只知道「他点了热线按钮」不算。</p></div>',
                unsafe_allow_html=True)


# ============================ 回应库 ============================

def _page_library(api):
    st.markdown("### 回应库")
    st.caption("小程序端不使用 AI 生成回复，改为从这张表里按情绪/话题选句（个人主体小程序不能过 AI 问答类目）。")
    q = st.text_input("搜索文案 / 情绪 / 话题", placeholder="例如：孤独")
    rows, err = _run(api.library, q)
    if err:
        st.error(err)
        return
    rows = rows or []
    if not rows:
        st.info("没有匹配的文案。")
        return

    df = pd.DataFrame(rows)
    st.caption("共 %d 条" % len(df))
    col1, col2 = st.columns(2)
    with col1:
        moods = ["全部"] + sorted(df["mood"].unique().tolist())
        msel = st.selectbox("按情绪筛选", moods)
    with col2:
        topics = ["全部"] + sorted(df["topic"].unique().tolist())
        tsel = st.selectbox("按话题筛选", topics)
    if msel != "全部":
        df = df[df["mood"] == msel]
    if tsel != "全部":
        df = df[df["topic"] == tsel]

    st.dataframe(df.rename(columns={"id": "ID", "mood": "情绪", "topic": "话题", "text": "文案",
                                    "weight": "权重", "enabled": "启用"}),
                 width="stretch", hide_index=True, height=460)


# ============================ 系统自检 ============================

def _page_health(api):
    st.markdown("### 系统自检")
    st.caption("后台读数据走的是数据库函数（RPC），密钥在请求体里传——云网关不透传自定义请求头，所以没用表直读。")
    ok = st.button("跑一次自检")
    if not ok and not st.session_state.get("health_done"):
        return
    st.session_state.health_done = True

    with st.spinner("检查中…"):
        stats, err = _run(api.stats)

    if err:
        st.error(err)
        st.markdown('<div class="card"><h4>排查顺序</h4>'
                    '<p>1. 密钥：DB 里的函数密钥与 Secrets 里的 <code>db_key</code> 是否一致</p>'
                    '<p>2. 函数：是否已创建 admin_stats / admin_users / admin_user_detail / admin_alerts_list / admin_library</p>'
                    '<p>3. 网络：应用能否访问 mp-api.app.workbuddy.host</p></div>', unsafe_allow_html=True)
        return

    st.success("接口连通，密钥正确。")
    st.json(stats or {}, expanded=True)
    st.caption("版本 v%s" % VERSION)


# ============================ 数量占比 ============================
#
# PPT P12 的追问是「第八周还剩多少人」；投资对话的追问是「四端有没有同一个系统」。
# 这一页用**占比**回答这两问：四端分布（是不是真的同源）、活跃度分层（用到什么深度）、
# 对话轮次分布（浅聊还是深聊）、留存（还剩多少）、时段分布（是不是深夜来用）。
#
# 数据源只有 admin_metrics（纯聚合 RPC）——这一页虽然管理员可见，
# 但保持"不含个人级内容"的习惯，整页可以直接截图进汇报。

def _page_share(api):
    m, err = _run(api.metrics)
    if err:
        st.error("数据暂不可用：%s" % _esc(err))
        return

    st.markdown("### 数量占比")
    st.caption("回答两件事：四端是不是同一个系统、用户用到了什么深度。数据全部来自 admin_metrics 聚合接口，"
               "本页不出现任何个人标识，可直接截图放进汇报材料。")

    # ---- 一、四端分布 ----
    st.markdown("#### 一、客户端分布（近 7 天活跃）")
    plat = m.get("platforms") or {}
    if plat:
        names = {"h5": "网页 H5", "mp": "微信小程序", "app": "安卓 App", "st": "网页版（桌面）", "unknown": "未标注"}
        total = sum(plat.values()) or 1
        rows = [{"端": names.get(k, k), "人数": v, "占比": "%s%%" % round(v * 100 / total, 1)}
                for k, v in sorted(plat.items(), key=lambda x: -x[1])]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        c1, c2 = st.columns([2, 1])
        with c1:
            st.bar_chart(pd.DataFrame({"人数": {names.get(k, k): v for k, v in plat.items()}}))
        with c2:
            st.metric("覆盖端数", "%d / 4" % len([k for k in plat if k in ("h5", "mp", "app", "st")]))
        st.caption("四个端都应出现——出现「未标注」说明有旧版客户端还在跑，升级后一周内应消失。")
    else:
        st.caption("还没有埋点数据。")

    # ---- 二、活跃度分层 ----
    st.markdown("#### 二、活跃度分层（全部注册用户）")
    stats, serr = _run(api.stats)
    if not serr and stats:
        users = stats.get("users", 0) or 0
        layered = pd.DataFrame({
            "层级": ["只注册（看过就走）", "写过日记", "聊过天", "有长期记忆（≥10 轮）"],
            "人数": [users, stats.get("diaries", 0), stats.get("messages", 0), stats.get("memory", 0)],
        })
        st.dataframe(layered, width="stretch", hide_index=True)
        st.caption("注意四行**不是互斥**的：一个人可以同时写日记和聊天。这张表看的是「每个功能被用到的深度」，"
                   "不是把人分类。用户总数 %d。" % users)

    # ---- 三、对话轮次分布 ----
    st.markdown("#### 三、对话深度（有对话的用户轮次分布）")
    turns, terr = _run(api.turn_distribution)
    buckets = (turns or {}).get("buckets") or []
    if terr:
        st.caption("轮次分布接口未部署（admin_turn_distribution），不影响其它页面。")
    elif buckets:
        df = pd.DataFrame([{"档位": b.get("label"), "人数": b.get("users", 0)} for b in buckets])
        df["占比"] = df["人数"].apply(lambda v: "%s%%" % round(v * 100 / (sum(df["人数"]) or 1), 1))
        st.bar_chart(df.set_index("档位")[["人数"]])
        st.dataframe(df, width="stretch", hide_index=True)
        st.caption("P9 的承诺是「先接住情绪」——1–5 轮是浅聊，21 轮以上说明真的有人把它当树洞。")
    else:
        st.info("还没有人聊过天（chat_messages 里没有 role=user 的记录）。这张表会在第一批对话出现后自动长出来。")

    # ---- 四、留存 ----
    st.markdown("#### 四、留存（回答 P12：第八周还剩多少人）")
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("次日留存", "%s%%" % (m.get("retention_d1", 0) or 0))
    d2.metric("7 日留存", "%s%%" % (m.get("retention_d7", 0) or 0))
    d3.metric("14 日留存", "%s%%" % (m.get("retention_d14", 0) or 0))
    d4.metric("30 日留存", "%s%%" % (m.get("retention_d30", 0) or 0))
    st.caption("第 8 周（56 天）留存要等数据积累满 8 周——现在显示的每一天都是在给那个数打地基。")

    # ---- 五、时段分布 ----
    st.markdown("#### 五、时段分布（近 7 天活跃）")
    hours, herr = _run(api.hour_distribution)
    hrows = (hours or {}).get("hours") or []
    if herr:
        st.caption("时段分布接口未部署（admin_hour_distribution），不影响其它页面。")
    elif hrows:
        # 补齐 0–23 全时段，否则没人的小时在图上直接消失，看起来像"没有凌晨使用"
        got = {int(r.get("h", 0)): int(r.get("n", 0)) for r in hrows}
        df = pd.DataFrame({"hour": list(range(24))})
        df["次数"] = df["hour"].map(lambda h: got.get(h, 0))
        df["时段"] = df["hour"].apply(lambda h: "%02d" % h)
        st.bar_chart(df.set_index("时段")[["次数"]])
        deep = int(df[df["hour"].between(0, 5)]["次数"].sum())
        alln = int(df["次数"].sum()) or 1
        st.metric("凌晨 0–6 点占比", "%s%%" % round(deep * 100 / alln, 1),
                  help="P9 的场景「凌晨两点想说话」有多少是真的——这个数字直接回答它。")
        st.caption("数据源：app_open 埋点（每端每天去重一次），时区按 Asia/Shanghai。")
    else:
        st.info("近 7 天还没有 app_open 埋点。")





# ============================ 校方视图（P12：师生物理隔离）============================
#
# 汇报 P12 的原话是「师生版本分开，给老师看的报告看不到个人级内容」。
# 这条承诺不能只写在 PPT 里——后台必须真的存在一个**只出聚合数**的视图，
# 而且它能被单独截图/导出给老师看。
#
# 实现上不是"权限开关"而是"另一套数据源"：校方视图不调用任何带 user_id 的接口，
# 只调 admin_metrics（纯聚合）与 admin_alerts_list 的**计数**（不展示原文）。
# 这样即使有人误操作，也不可能在这个页面上把某个学生的日记翻出来——
# 物理隔离的意思是：路径不存在，而不是"有权限但没点"。

def _page_school(api):
    m, err = _run(api.metrics)
    if err:
        st.error("数据暂不可用：%s" % _esc(err))
        return

    st.markdown("### 校方 / 导师视图")
    st.caption("本页只呈现聚合数据与流程执行情况，**不包含任何个人级内容**"
               "（对话原文、日记内容、账号标识一律不出现）。可直接截图作为阶段性汇报材料。")

    st.markdown("#### 一、使用规模（近 7 天）")
    c1, c2, c3 = st.columns(3)
    c1.metric("周活跃用户数", m.get("wau", 0))
    c2.metric("月活跃用户数", m.get("mau", 0))
    c3.metric("人均对话轮次", m.get("avg_turns", 0))
    plat = m.get("platforms") or {}
    if plat:
        names = {"h5": "网页", "mp": "小程序", "app": "安卓 App", "unknown": "未标注"}
        st.markdown("客户端分布：" + "　".join(
            ["%s **%d 人**" % (names.get(k, k), v) for k, v in plat.items()]))

    st.markdown("#### 二、五个研究指标（与 P9 一致）")
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("主动周留存率", "%s%%" % (m.get("week_retention", 0) or 0))
    d2.metric("首周激活率", "%s%%" % (m.get("activation_7d", 0) or 0))
    d3.metric("日均对话轮次", m.get("turns_per_day", 0))
    d4.metric("情绪记录连续性", "%s 天/周" % (m.get("diary_days", 0) or 0))
    st.caption("口径说明见《研究方案》第 4 节。主动周留存 = 一周内跨 ≥2 天打开的人数占比；"
               "首周激活 = 注册后 7 天内首次记录情绪的比例。样本量小于 20 时比率波动很大，不宜下结论。")

    st.markdown("#### 三、安全流程执行情况")
    e1, e2, e3, e4 = st.columns(4)
    e1.metric("近 7 天 L2/L3 触发", (m.get("crisis_l2_7d", 0) or 0) + (m.get("crisis_l3_7d", 0) or 0))
    e2.metric("未关闭警报", m.get("alert_open", 0))
    e3.metric("已关闭警报", m.get("alert_closed", 0))
    e4.metric("平均响应时长（分钟）", m.get("alert_median_minutes", 0))
    st.caption("这里只报**数量**与**流程时效**，不报内容。具体个案由研究者按《危机响应 SOP》"
               "单独向您汇报，且遵循知情同意的范围。")

    st.markdown("#### 四、结论与下一步")
    base = m.get("week_retention_base", 0) or 0
    act_base = m.get("activation_base", 0) or 0
    if base == 0 and act_base == 0:
        st.info("尚未进入正式观察期：还没有满一周的 cohort。")
    else:
        lines = []
        if act_base:
            lines.append("首周激活率 %s%%（止损线 30%%）" % (m.get("activation_7d", 0) or 0))
        if base:
            lines.append("主动周留存 %s%%（阈值 25%%）" % (m.get("week_retention", 0) or 0))
        st.markdown("本期关键读数：" + "；".join(lines) + "。")

    st.caption("生成时间：%s ｜ 数据来源：客户端埋点（不含任何内容字段）"
               % _esc(m.get("generated_at", "-")))

    # 导出：给老师的那份，永远只有聚合数
    agg = pd.DataFrame([
        {"指标": "周活跃用户", "数值": m.get("wau", 0)},
        {"指标": "月活跃用户", "数值": m.get("mau", 0)},
        {"指标": "主动周留存率(%)", "数值": m.get("week_retention", 0)},
        {"指标": "首周激活率(%)", "数值": m.get("activation_7d", 0)},
        {"指标": "日均对话轮次", "数值": m.get("turns_per_day", 0)},
        {"指标": "情绪记录连续性(天/周)", "数值": m.get("diary_days", 0)},
        {"指标": "L2触发(7天)", "数值": m.get("crisis_l2_7d", 0)},
        {"指标": "L3触发(7天)", "数值": m.get("crisis_l3_7d", 0)},
        {"指标": "未关闭警报", "数值": m.get("alert_open", 0)},
        {"指标": "已关闭警报", "数值": m.get("alert_closed", 0)},
        {"指标": "响应时长中位数(分钟)", "数值": m.get("alert_median_minutes", 0)},
    ])
    st.download_button("导出校方汇总 CSV（无个人数据）", _csv_safe(agg).to_csv(index=False).encode("utf-8-sig"),
                       file_name="heart-stars-school-summary.csv", mime="text/csv")




def render():
    st.markdown("""
    <style>
    .hero h1 { color:#ffd68f; margin:0; font-size:24px; }
    .hero p { color:#9aa6c4; margin:0; font-size:13px; }
    .card { background: rgba(28,36,58,.06); border:1px solid rgba(120,130,160,.25);
            border-radius:12px; padding:12px 14px; margin:8px 0; }
    .card h4 { margin:0 0 6px; font-size:14px; }
    </style>
    """, unsafe_allow_html=True)

    if not st.session_state.get("admin_ok"):
        _login_screen()
        return

    # 闲置超时：后台能看到全站最敏感的内容，离开电脑就必须自己锁上
    seen = float(st.session_state.get("admin_seen") or 0)
    if seen and (time.time() - seen) > IDLE_TIMEOUT_SEC:
        st.session_state.admin_ok = False
        st.session_state.admin_api = None
        st.warning("闲置超过 30 分钟，已自动退出，请重新登录。")
        _login_screen()
        return
    st.session_state.admin_seen = time.time()

    with st.sidebar:
        st.markdown("### 心与星寻 · 后台")
        st.caption("管理员：%s" % (_creds()[0] or "—"))
        st.session_state.admin_page = st.radio("功能", PAGES,
                                               index=PAGES.index(st.session_state.get("admin_page", "总览")))
        st.divider()
        if st.button("退出登录"):
            st.session_state.admin_ok = False
            st.session_state.admin_api = None
            st.session_state.admin_uid = None
            _clear_failures()
            st.rerun()

    api = _api()
    page = st.session_state.get("admin_page", "总览")
    if page == "总览":
        _page_overview(api)
    elif page == "周度看板":
        _page_metrics(api)
    elif page == "数量占比":
        _page_share(api)
    elif page == "校方视图":
        _page_school(api)
    elif page == "用户管理":
        _page_users(api)
    elif page == "用户详情":
        _page_detail(api)
    elif page == "警报中心":
        _page_alerts(api)
    elif page == "回应库":
        _page_library(api)
    else:
        _page_health(api)
