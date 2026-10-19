"""心与星寻 · 手机版视觉还原（Streamlit 桌面/浏览器版共用）。

为什么要有这一层：用户要求「网页的效果要和 app、小程序界面效果一样，不要管以前的其他效果页面」。
手机三端（H5 / 小程序 / App）的观感由四件事构成，缺一件就不像：

  ① 深空底色 + 星云 + 会呼吸的星星（`Sky.vue`）
  ② 暖金主色 #f4c877 / #ffd68f，正文米白 #f3ede2
  ③ 卡片 = 半透明深色 + 细金边 + 14px 圆角
  ④ 一个固定在底部的三格 tab（书写 / 对话 / 我的）

Streamlit 的组件结构是固定的，无法照搬 uni-app 的 DOM；这里做的是「同一套视觉语言」：
颜色、圆角、字号、间距、按压态全部对齐手机端，并额外用 CSS 把手机宽度夹在中间
（max-width 480px），避免在电脑上被拉成一张宽表——那正是"以前那些页面"不像手机的原因。
本机桌面版（沿用 .streamlit/config.toml 的轻色主题）可用 light_css() 再叠一层覆盖：
用户在家里 1440px 屏幕上要读的是「浅底深字」，而不是一条深色小窄条。

⚠️ 注意：本模块只输出 CSS 与几个 HTML 片段构造器，**不碰任何业务逻辑**。
   所有用户文本在进 HTML 前必须先过 _esc()（见 views/companion.py）。
"""

# ---------------------------------------------------------------- 调色板

INK = "#0b1020"          # 页面底色（与 App.vue 的 page background 一致）
INK_DEEP = "#070b18"     # 星空底色
CARD = "rgba(21,27,51,0.88)"
CARD_EDGE = "rgba(255,214,143,0.16)"
CREAM = "#f3ede2"        # 主文字（App 全站正文色）
CREAM_DIM = "rgba(243,237,226,0.55)"
CREAM_FAINT = "rgba(243,237,226,0.32)"
GOLD = "#f4c877"         # 主强调（tab 选中色、按钮）
GOLD_BRIGHT = "#ffd68f"
GOLD_SOFT = "rgba(255,214,143,0.14)"
DANGER = "#e0705f"

# 六种心情（与 core/moodcurve.py 的 MOOD_FACES 同源，不要在别处再定义一套）
MOODS = [
    ("good", "☀", "还不错", "#ffd68f"),
    ("neutral", "☁", "就那样", "#8e97ad"),
    ("low", "☂", "有点低落", "#6f8fd8"),
    ("anxious", "⚡", "有点焦虑", "#b07fd0"),
    ("angry", "✹", "有点生气", "#e0705f"),
    ("lonely", "☾", "有点孤独", "#56a3a3"),
]

PHONE_MAX = 480   # 手机端最大宽度（rpx→px 的经验换算：750rpx ≈ 480px）


# ---------------------------------------------------------------- 星空背景
#
# 不复刻 Sky.vue 的随机星星（Streamlit 每次 rerun 会重排，星星会"跳"）。
# 改为固定种子的伪随机序列：同一个页面每次渲染位置一致，看起来才像"那一片星空"。

def _stars(n=86):
    """确定性星星序列（LCG）。返回 [(left%, top%, size px, color, dur s, delay s, glow)]"""
    out = []
    seed = 20261018
    def nxt():
        nonlocal seed
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        return seed / 0x7FFFFFFF
    colors = ["#ffffff", "#ffffff", "#ffffff", "#cfe0ff", "#ffe9c4", "#e6d6ff"]
    for i in range(n):
        big = nxt() < 0.14
        size = round(5 + nxt() * 4, 1) if big else round(2 + nxt() * 3.5, 1)
        out.append((
            round(nxt() * 100, 2), round(nxt() * 100, 2), size,
            colors[int(nxt() * len(colors))],
            round(2.6 + nxt() * 5.4, 2), round(nxt() * 6, 2), big,
        ))
    return out


def sky_html(height_px=640):
    """生成一层星空背景（position:relative 的容器，放在每个页面顶部）。"""
    stars = "".join(
        '<i style="left:%s%%;top:%s%%;width:%spx;height:%spx;background:%s;'
        'animation-duration:%ss;animation-delay:%ss;%s"></i>'
        % (l, t, s, s, c, d, dl,
           "box-shadow:0 0 12px 2px rgba(255,255,255,.45);" if glow else "")
        for l, t, s, c, d, dl, glow in _stars()
    )
    return (
        '<div class="hsx-sky" style="height:%dpx">'
        '<span class="hsx-neb n1"></span><span class="hsx-neb n2"></span>'
        '<span class="hsx-neb n3"></span><span class="hsx-neb n4"></span>'
        '%s<span class="hsx-meteor m1"></span><span class="hsx-meteor m2"></span>'
        '<span class="hsx-vignette"></span></div>'
    ) % (height_px, stars)


# ---------------------------------------------------------------- CSS

CSS = """
<style>
/* ============ 一、页面外壳：把内容夹成手机宽度 ============ */
.stApp {
  background: #070b18;
}
.stApp > header { background: transparent; }
.block-container {
  max-width: %(phone)dpx !important;
  padding: 0 18px 96px !important;
}
section[data-testid="stSidebar"] { display: none; }

/* ============ 二、星空 ============ */
.hsx-sky {
  position: absolute; left: 0; right: 0; top: 0;
  overflow: hidden; z-index: 0; pointer-events: none;
  background-color: #070b18;
  background-image:
    radial-gradient(120%% 60%% at 22%% 0%%, rgba(38,48,104,.75) 0%%, rgba(7,11,24,0) 62%%),
    radial-gradient(100%% 55%% at 84%% 8%%, rgba(48,38,88,.6) 0%%, rgba(7,11,24,0) 58%%),
    radial-gradient(90%% 50%% at 50%% 100%%, rgba(56,40,74,.5) 0%%, rgba(7,11,24,0) 60%%),
    linear-gradient(180deg, #060a16 0%%, #0b1024 45%%, #120f24 100%%);
}
.hsx-sky i {
  position: absolute; border-radius: 50%%; display: block;
  animation-name: hsx-twinkle; animation-timing-function: ease-in-out;
  animation-iteration-count: infinite; opacity: .7;
}
@keyframes hsx-twinkle { 0%%,100%% { opacity:.3 } 50%% { opacity:1 } }
.hsx-neb { position: absolute; border-radius: 50%%; }
.hsx-neb.n1 { left:-6%%; top:-4%%;  width:520px; height:520px;
  box-shadow: 0 0 220px 60px rgba(120,145,255,.13); }
.hsx-neb.n2 { left:52%%; top:2%%;   width:440px; height:440px;
  box-shadow: 0 0 190px 50px rgba(255,190,120,.09); }
.hsx-neb.n3 { left:18%%; top:38%%;  width:560px; height:560px;
  box-shadow: 0 0 240px 70px rgba(185,150,255,.11); }
.hsx-neb.n4 { left:64%%; top:52%%;  width:380px; height:380px;
  box-shadow: 0 0 170px 44px rgba(120,220,210,.08); }
.hsx-meteor {
  position:absolute; width:76px; height:1px; border-radius:2px; opacity:0;
  background: linear-gradient(90deg, rgba(255,255,255,0) 0%%, rgba(255,233,196,.85) 100%%);
  animation: hsx-meteor 13s ease-in infinite;
}
.hsx-meteor.m1 { left:58%%; top:14%%; animation-delay:1.5s; }
.hsx-meteor.m2 { left:22%%; top:46%%; animation-delay:7s; }
@keyframes hsx-meteor {
  0%% { opacity:0; transform: translate3d(0,0,0) rotate(28deg); }
  4%% { opacity:.9; }
  14%% { opacity:0; transform: translate3d(140px,80px,0) rotate(28deg); }
  100%% { opacity:0; transform: translate3d(140px,80px,0) rotate(28deg); }
}
.hsx-vignette {
  position:absolute; left:-10%%; top:-10%%; width:120%%; height:120%%; border-radius:50%%;
  box-shadow: inset 0 0 220px 150px rgba(0,0,0,.55);
}

/* ============ 三、文字层级（与 App.vue 一致） ============ */
.stApp, .stApp p, .stApp li, .stApp label, .stApp span { color: %(cream)s; }
h1, h2, h3, h4, h5 { color: %(cream)s !important; letter-spacing: .2px; }
.hsx-hero { padding: 6px 0 14px; position: relative; z-index: 2; }
.hsx-hero h1 { font-size: 26px; margin: 0 0 4px; color: %(gold_bright)s !important; font-weight: 600; }
.hsx-hero p  { font-size: 13px; margin: 0; color: %(cream_dim)s; line-height: 1.7; }
.hsx-sub { font-size: 12px; color: %(cream_dim)s; line-height: 1.75; margin: 2px 0 10px; }
.hsx-faint { font-size: 11.5px; color: %(cream_faint)s; line-height: 1.7; }

/* ============ 四、卡片 ============ */
.hsx-card {
  background: %(card)s; border: 1px solid %(card_edge)s; border-radius: 14px;
  padding: 14px 16px; margin: 10px 0; position: relative; z-index: 2;
  box-shadow: 0 8px 22px rgba(0,0,0,.22);
}
.hsx-card.mini { padding: 10px 12px; margin: 8px 0; border-radius: 12px; }
.hsx-card h4 { margin: 0 0 6px; font-size: 14.5px; color: %(gold_bright)s !important; font-weight: 600; }
.hsx-card p { margin: 0 0 4px; font-size: 13.5px; line-height: 1.8; color: %(cream)s; }
.hsx-card p.kv { font-size: 11.5px; color: %(cream_dim)s; }
.hsx-card p.warn { color: #ffb08f; }

/* ============ 五、时间胶囊（首页今日卡） ============ */
.hsx-today { display:flex; align-items:center; gap:12px; }
.hsx-today .num { font-size: 30px; font-weight: 600; color: %(gold_bright)s; line-height:1; }
.hsx-today .txt { font-size: 12.5px; color: %(cream_dim)s; line-height:1.7; }

/* ============ 六、七日心情带 ============ */
.hsx-band { display:flex; gap:6px; margin: 10px 0 6px; }
.hsx-band .day { flex:1; text-align:center; min-width: 0; }
.hsx-band .day .face { font-size: 13px; color: %(cream_dim)s; margin-bottom:4px; }
.hsx-band .day .bar {
  height: 44px; border-radius: 9px; display:flex; align-items:flex-end;
  justify-content:center; font-size: 10px; color: rgba(11,16,32,.72); padding-bottom:3px;
}
.hsx-band .day .lbl { font-size: 10px; color: %(cream_faint)s; margin-top:4px; }
/* 六心情胶囊（书写页）：紧凑横排，避免 Streamlit 把 columns 拉成竖排 */
.hsx-moods { display:flex; gap:6px; margin: 6px 0 2px; flex-wrap: nowrap; }
.hsx-moods .m {
  flex:1; min-width:0; height:40px; border-radius:10px; display:flex; align-items:center;
  justify-content:center; font-size:16px; color:#0e1322; border:none; cursor:pointer;
  font-family: inherit;
}
.hsx-moods .m.off { opacity:.45; }
.hsx-moods .m.on { box-shadow: 0 0 0 2px %(gold_bright)s inset; opacity:1; }

/* ============ 七、聊天 ============ */
div[data-testid="stChatMessage"] {
  background: transparent !important; padding: 2px 0 !important;
}
div[data-testid="stChatMessage"] p { font-size: 14px; line-height: 1.85; }
div[data-testid="stChatMessage"][data-testid*="user"] { }
.hsx-bubble-me {
  background: %(gold_soft)s; border: 1px solid rgba(255,214,143,.28);
  border-radius: 14px 14px 4px 14px; padding: 10px 13px; font-size: 14px;
  line-height: 1.85; color: %(cream)s; margin: 6px 0 6px auto; max-width: 86%%;
  width: fit-content; white-space: pre-wrap; position: relative; z-index: 2;
}
.hsx-bubble-ai {
  background: %(card)s; border: 1px solid rgba(255,255,255,.07);
  border-radius: 14px 14px 14px 4px; padding: 11px 14px; font-size: 14px;
  line-height: 1.85; color: %(cream)s; margin: 6px 0; max-width: 92%%;
  white-space: pre-wrap; position: relative; z-index: 2;
}
.hsx-who { font-size: 11px; color: %(cream_faint)s; margin: 10px 0 0; }

/* ============ 八、按钮（圆角胶囊，按压有感） ============ */
.stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
  background: rgba(243,237,226,.08) !important; color: %(cream)s !important;
  border: 1px solid rgba(243,237,226,.16) !important;
  border-radius: 20px !important; font-size: 13.5px !important;
  padding: 7px 14px !important; transition: all .16s ease;
  white-space: nowrap;
}
/* 心情六选一：强制六列保持横排。锚点是元素容器上的 st-key-mf_* class
   （st.button(key=...) 会把它落到 DOM 上，比 aria-label/title 稳定）。 */
[data-testid="stHorizontalBlock"]:has(.st-key-mf_good) {
  flex-wrap: nowrap !important;
}
[data-testid="stHorizontalBlock"]:has(.st-key-mf_good) > div[data-testid="stColumn"] {
  min-width: 0 !important; flex: 1 1 0 !important;
}
[data-testid="stHorizontalBlock"]:has(.st-key-mf_good) button {
  padding: 6px 0 !important;
}
.stButton > button:hover, .stFormSubmitButton > button:hover {
  border-color: rgba(255,214,143,.5) !important; color: %(gold_bright)s !important;
}
.stButton > button:active, .stFormSubmitButton > button:active { transform: scale(.975); }
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  background: linear-gradient(180deg, #ffdc96 0%%, #eeb862 100%%) !important;
  color: #4a3308 !important; border: none !important; font-weight: 600;
}

/* ============ 九、输入框 ============ */
.stTextInput input, .stTextArea textarea, .stNumberInput input {
  background: rgba(243,237,226,.06) !important; color: %(cream)s !important;
  border: 1px solid rgba(243,237,226,.14) !important; border-radius: 12px !important;
  font-size: 14px !important;
}
.stTextInput input::placeholder, .stTextArea textarea::placeholder { color: %(cream_faint)s !important; }
div[data-testid="stChatInput"] {
  background: rgba(21,27,51,.92) !important; border: 1px solid rgba(255,214,143,.2) !important;
  border-radius: 18px !important;
}
div[data-testid="stChatInput"] textarea { color: %(cream)s !important; }

/* ============ 十、指标卡（我的 / 回顾） ============ */
div[data-testid="stMetric"] {
  background: %(card)s; border: 1px solid %(card_edge)s; border-radius: 12px;
  padding: 10px 12px;
}
div[data-testid="stMetric"] label { color: %(cream_dim)s !important; font-size: 11.5px !important; }
div[data-testid="stMetricValue"] { color: %(gold_bright)s !important; font-size: 21px !important; }

/* ============ 十一、底部三格 tab（复刻 uni-app tabBar） ============ */
.hsx-tabspace { height: 74px; }
div[data-testid="stHorizontalBlock"].hsx-tabs {
  position: fixed; left: 50%%; transform: translateX(-50%%);
  bottom: 0; width: 100%%; max-width: %(phone)dpx; z-index: 999;
  background: rgba(13,18,38,.97); border-top: 1px solid rgba(255,214,143,.14);
  padding: 8px 10px 10px; gap: 4px; backdrop-filter: blur(10px);
}
.hsx-tabs .stButton > button {
  border: none !important; background: transparent !important;
  color: #8a90a8 !important; font-size: 12px !important; padding: 4px 0 !important;
  line-height: 1.35 !important; min-height: 46px;
}
.hsx-tabs .stButton > button:hover { color: %(gold_bright)s !important; }
.hsx-tabs .stButton > button[kind="primary"] {
  background: transparent !important; color: %(gold)s !important; font-weight: 600;
}
.hsx-tabs .stButton > button p { font-size: 19px !important; margin: 0 0 1px !important; }
/* 底部 tab 三格等宽横排（同心情行的 nowrap 逻辑）。中文 key 会被转义成 --，
   所以用 [class*="st-key-tab_"] 子串匹配。 */
[data-testid="stHorizontalBlock"]:has([class*="st-key-tab_"]) {
  flex-wrap: nowrap !important;
  position: fixed; left: 50%%; transform: translateX(-50%%);
  bottom: 0; width: 100%%; max-width: %(phone)dpx; z-index: 999;
  background: rgba(13,18,38,.97); border-top: 1px solid rgba(255,214,143,.14);
  padding: 8px 10px 10px; gap: 4px; backdrop-filter: blur(10px);
  margin: 0;
}
[data-testid="stHorizontalBlock"]:has([class*="st-key-tab_"]) > div[data-testid="stColumn"] {
  min-width: 0 !important; flex: 1 1 0 !important;
}

/* ============ 十二、其它组件外观 ============ */
/* 对话页顶部形象：居中（st.image 默认左对齐，外层 stColumn 也要并排居中） */
[data-testid="stImage"] { display: flex; justify-content: center; }
[data-testid="stImage"] img { display: block; margin: 0 auto; border-radius: 16px; }
div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
  background: rgba(243,237,226,.06) !important; border-color: rgba(243,237,226,.14) !important;
  border-radius: 12px !important; color: %(cream)s !important;
}
div[data-testid="stExpander"] {
  background: %(card)s !important; border: 1px solid rgba(255,255,255,.07) !important;
  border-radius: 12px !important;
}
hr { border-color: rgba(243,237,226,.08) !important; }
.hsx-about { text-align:center; font-size: 11px; color: %(cream_faint)s; margin: 18px 0 6px; line-height: 1.8; }
</style>
""" % {
    "phone": PHONE_MAX,
    "cream": CREAM,
    "cream_dim": CREAM_DIM,
    "cream_faint": CREAM_FAINT,
    "gold": GOLD,
    "gold_bright": GOLD_BRIGHT,
    "gold_soft": GOLD_SOFT,
    "card": CARD,
    "card_edge": CARD_EDGE,
}


# ---------------------------------------------------------------- 浅色覆盖（本机桌面版）
#
# 为什么需要：本机 `.streamlit/config.toml` 是浅色主题（用户 1440px 屏幕）。
# 若把整套深空配色强压上去，会得到"深色小窄条 + 白色大片空白"的割裂观感。
# 这里把「同一套视觉语言」翻译成浅色：米白底、深墨字、暖金强调、白卡片 + 细边。
# **不换结构、不换字号、不换圆角** —— 换的只是颜色，所以仍然是"同一个产品"。

LIGHT_CSS = """
<style>
.stApp { background: #fbf8f2 !important; }
.block-container { max-width: 760px !important; padding-top: 1.4rem !important; }
.hsx-sky { opacity: .22; filter: saturate(.55); }
.hsx-sky .hsx-vignette { box-shadow: none; background: linear-gradient(180deg, rgba(251,248,242,0) 0%, rgba(251,248,242,.92) 100%); }
.hsx-hero h1 { color: #2b2519 !important; }
.hsx-hero p, .hsx-sub, .hsx-faint, .hsx-about { color: #7a705f !important; }
.hsx-card {
  background: #ffffff !important; border: 1px solid #ece4d4 !important;
  box-shadow: 0 2px 10px rgba(90,70,30,.06) !important;
}
.hsx-card h4 { color: #8a6a1f !important; }
.hsx-card p { color: #3a342a !important; }
.hsx-card p.kv { color: #857b68 !important; }
.hsx-card p.warn { color: #b8442e !important; }
.hsx-bubble-me {
  background: #fdf3dc !important; border-color: #f0dfb6 !important; color: #3a342a !important;
}
.hsx-bubble-ai { background: #ffffff !important; border-color: #ece4d4 !important; color: #3a342a !important; }
.hsx-who { color: #9c9280 !important; }
.hsx-band .day .face { color: #857b68 !important; }
.hsx-band .day .lbl { color: #a2977f !important; }
div[data-testid="stMetric"] { background: #ffffff !important; border-color: #ece4d4 !important; }
div[data-testid="stMetric"] label { color: #857b68 !important; }
div[data-testid="stMetricValue"] { color: #8a6a1f !important; }
.stApp p, .stApp li, .stApp label, .stApp span { color: #3a342a; }
h1, h2, h3, h4, h5 { color: #2b2519 !important; }
.stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
  background: #ffffff !important; color: #3a342a !important; border: 1px solid #e3d9c6 !important;
}
.stButton > button:hover, .stFormSubmitButton > button:hover {
  border-color: #e2b968 !important; color: #8a6a1f !important;
}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  background: linear-gradient(180deg, #ffe1a4 0%, #f2c774 100%) !important;
  color: #4a3308 !important; border: none !important;
}
div[data-testid="stHorizontalBlock"].hsx-tabs {
  background: rgba(255,255,255,.97) !important; border-top: 1px solid #ece4d4 !important;
}
.hsx-tabs .stButton > button { background: transparent !important; color: #9c9280 !important; border: none !important; }
.hsx-tabs .stButton > button[kind="primary"] { background: transparent !important; color: #b8860b !important; }
.stTextInput input, .stTextArea textarea, .stNumberInput input {
  background: #ffffff !important; color: #3a342a !important; border-color: #e3d9c6 !important;
}
.stTextInput input::placeholder, .stTextArea textarea::placeholder { color: #b3a992 !important; }
div[data-testid="stChatInput"] { background: #ffffff !important; border-color: #e3d9c6 !important; }
div[data-testid="stChatInput"] textarea { color: #3a342a !important; }
div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {
  background: #ffffff !important; border-color: #e3d9c6 !important; color: #3a342a !important;
}
div[data-testid="stExpander"] { background: #ffffff !important; border-color: #ece4d4 !important; }
hr { border-color: #ece4d4 !important; }
</style>
"""


# ---------------------------------------------------------------- 片段构造器

def hero(title, sub=""):
    import html
    h = '<div class="hsx-hero"><h1>%s</h1>' % html.escape(title)
    if sub:
        h += "<p>%s</p>" % html.escape(sub)
    return h + "</div>"


def card(head, lines=(), cls=""):
    """一张卡片。head 与 lines 都视为**已转义**的 HTML（调用方负责转义用户文本）。"""
    h = '<div class="hsx-card %s">' % cls
    if head:
        h += "<h4>%s</h4>" % head
    for ln in lines:
        h += ln
    return h + "</div>"


def kv(text):
    import html
    return '<p class="kv">%s</p>' % html.escape(text)


def p(text):
    import html
    return "<p>%s</p>" % html.escape(text)


def band(series):
    """七日心情带。series: [{label, date, mood, color}]（来自 moodcurve.week_mood_series）"""
    import html
    cells = []
    for d in series:
        face = {"good": "☀", "neutral": "☁", "low": "☂",
                "anxious": "⚡", "angry": "✹", "lonely": "☾"}.get(d.get("mood"), "·")
        color = d.get("color") or "rgba(243,237,226,0.10)"
        cells.append(
            '<div class="day"><div class="face">%s</div>'
            '<div class="bar" style="background:%s">%s</div>'
            '<div class="lbl">%s</div></div>'
            % (face, color, html.escape(str(d.get("date") or "")), html.escape(str(d.get("label") or "")))
        )
    return '<div class="hsx-band">%s</div>' % "".join(cells)


def moods_bar(picked=""):
    """六心情胶囊（与 App 书写页 mood-bar 同构）。picked: 当前选中 id。"""
    cells = []
    for mid, face, _name, color in MOODS:
        cls = "m" + (" on" if picked == mid else " off")
        cells.append('<button class="%s" style="background:%s">%s</button>' % (cls, color, face))
    return '<div class="hsx-moods">%s</div>' % "".join(cells)
