"""心与星寻 · Streamlit 版（陪伴应用）。

起服务：   streamlit run app.py

这是「陪伴版」独立应用：只有 对话 / 心情 / 我的 三个页面，不含管理后台。
管理后台是另一个独立应用（另一份文件夹 + 另一条链接）。
"""

import streamlit as st

st.set_page_config(
    page_title="心与星寻",
    page_icon="🌙",
    layout="centered",
    initial_sidebar_state="expanded",
)

from views import companion  # noqa: E402  （必须在 set_page_config 之后导入）

companion.render()
