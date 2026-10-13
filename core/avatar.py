"""星寻形象目录与表情动画调度（与 packages/core/src/avatar.js 同源）。

本文件只放纯逻辑与数据 —— 形象清单、情绪→动画映射。
图片资产在 assets/avatars/<id>.jpg（由三端统一仓的 256px JPEG 拷来）。
"""

# ================= 一、形象分组 =================
AVATAR_GROUPS = [
    {"id": "01", "name": "毛茸茸小队", "hint": "软乎乎的，想抱一只回家"},
    {"id": "02", "name": "天空梦境", "hint": "云朵、星星和月亮"},
    {"id": "03", "name": "彩色毛球", "hint": "一团软软的情绪"},
    {"id": "04", "name": "人类朋友", "hint": "像同桌一样的存在"},
    {"id": "05", "name": "星球宇宙", "hint": "远方其实也很近"},
]

# ================= 二、形象目录 =================
AVATARS = [
    {"id": "a01-1", "name": "奶油色小水獭", "group": "01"},
    {"id": "a01-2", "name": "垂耳兔抱云", "group": "01"},
    {"id": "a01-3", "name": "短腿柯基幼犬", "group": "01"},
    {"id": "a01-4", "name": "围巾小熊", "group": "01"},
    {"id": "a02-1", "name": "发光小云朵", "group": "02"},
    {"id": "a02-2", "name": "星星小精灵", "group": "02"},
    {"id": "a02-3", "name": "月亮小夜灯", "group": "02"},
    {"id": "a02-4", "name": "小水滴伙伴", "group": "02"},
    {"id": "a03-1", "name": "暖黄毛球", "group": "03"},
    {"id": "a03-2", "name": "忧郁蓝毛球", "group": "03"},
    {"id": "a03-3", "name": "粉紫渐变精灵", "group": "03"},
    {"id": "a03-4", "name": "青苔绿毛球", "group": "03"},
    {"id": "a04-1", "name": "耳机卫衣少年", "group": "04"},
    {"id": "a04-2", "name": "米色毛衣朋友", "group": "04"},
    {"id": "a04-3", "name": "中性卷发朋友", "group": "04"},
    {"id": "a04-4", "name": "抱抱枕伙伴", "group": "04"},
    {"id": "a05-1", "name": "深蓝小星球", "group": "05"},
    {"id": "a05-2", "name": "发光小月球", "group": "05"},
    {"id": "a05-3", "name": "宇航头盔光团", "group": "05"},
    {"id": "a05-4", "name": "银河小精灵", "group": "05"},
]

DEFAULT_AVATAR_ID = "a01-1"


def avatar_by_id(aid):
    for a in AVATARS:
        if a["id"] == aid:
            return a
    return None


def normalize_avatar_id(aid):
    """坏数据收敛：空/不存在的一律回默认形象。"""
    if not isinstance(aid, str) or not aid:
        return DEFAULT_AVATAR_ID
    return aid if avatar_by_id(aid) else DEFAULT_AVATAR_ID


def avatars_by_group(group_id):
    return [a for a in AVATARS if a["group"] == group_id]


# ================= 三、情绪 → 动画 =================
def mood_to_anim(mood):
    """八种情绪收拢成四种动画状态。"""
    if mood == "good":
        return "happy"
    if mood in ("heavy", "crisis"):
        return "heavy"
    if mood in ("low", "lonely", "anxious", "angry"):
        return "low"
    return "neutral"


ANIM_INFO = {
    "neutral": {"name": "安静待着", "desc": "轻轻呼吸，偶尔眨一下眼", "emoji": "🌙"},
    "happy": {"name": "为你高兴", "desc": "轻轻弹跳，冒出小星星", "emoji": "✨"},
    "low": {"name": "陪你低落", "desc": "慢慢垂下眼，掉几颗眼泪", "emoji": "💧"},
    "heavy": {"name": "认真在听", "desc": "放慢摇摆，光也暗下来", "emoji": "🌫"},
}
