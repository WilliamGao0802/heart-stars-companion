"""通用启动器：读环境变量 PORT，再拉起 Streamlit。

为什么需要它：
    Streamlit 自己不认 PORT（只认 STREAMLIT_SERVER_PORT），而绝大多数托管平台
    （WorkBuddy 应用发布、各种 PaaS）都是用 PORT 来告诉服务「监听哪个端口」。
    用这个脚本启动，就不必依赖平台去展开 shell 变量。

本地也能用：
    python run.py          → 默认 8501
    PORT=8600 python run.py

等价于：
    streamlit run app.py
"""

import os
import subprocess
import sys

port = (
    os.environ.get("PORT")
    or os.environ.get("STREAMLIT_SERVER_PORT")
    or "8501"
)

cmd = [
    sys.executable, "-m", "streamlit", "run", "app.py",
    "--server.port", str(port),
    "--server.address", "0.0.0.0",
    "--server.headless", "true",
    "--server.enableCORS", "false",
    "--server.enableXsrfProtection", "false",
    "--browser.gatherUsageStats", "false",
]

print("启动心与星寻 · 陪伴版，端口 %s" % port, flush=True)
sys.exit(subprocess.call(cmd))
