"""云服务公开配置（与三端统一仓 packages/adapters/src/cloud-config.js 同源）。

这三个值是云服务开通时返回的 publicConfig，属于「可以放在前端」的公开值：
  - endpoint       应用固定的网关地址（所有端共用同一个网关）
  - oauth_relay    账号登录中转地址（本期未用，先占位）
  - publishable_key 只用来标识「是哪个应用」，本身不带任何权限；
                     服务端会按 Origin 精确匹配来源，所以它可以躺在源码里。

千万不要往这里加任何密钥、AppSecret 或大模型 API Key —— 那些只能待在
Streamlit Secrets（云端）或环境变量（本地），由 core/admin 读取。
"""

ENDPOINT = "https://mp-api.app.workbuddy.host"
OAUTH_RELAY = "https://www.workbuddy.cn/v2/as/genie-baas/oauth"
PUBLISHABLE_KEY = "wbpk_udsc4Pu0hZdUiMpG3JcM2i_kwSNnWVnLzSsK8esS11B4LsY8w8byVgA"
APP_ID = "wbapp_udsc4Pu0hZdUiMpG3JcM2i"

# 已上线的手机版网页（分享文案里引用）
SITE_URL = "https://heart-and-stars.app.workbuddy.host/"

# 三端一致的时区（项目只面向国内用户）
TZ_OFFSET_HOURS = 8
