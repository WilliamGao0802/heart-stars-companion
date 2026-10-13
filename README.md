# 心与星寻 · 陪伴版（Streamlit 独立应用）

一个「会记住你」的情绪陪伴网页。三个页面：**对话 / 心情 / 我的**。
不含管理后台 —— 后台是另一个独立应用、另一条链接。

> **这个文件夹是完整可直接上传的**：整个文件夹传到 GitHub，再在 Streamlit Cloud 里
> 指向 `app.py`，就能跑起来。部署完成后它与 WorkBuddy 无关，**关掉 WorkBuddy 也照常运行**。

---

## 〇、文件夹里有什么

```
app.py                    ← 入口（云端 Main file path 就填这个）
run.py                    ← 通用启动器（读 PORT 环境变量再拉起 Streamlit，云端用不到）
requirements.txt          ← 依赖：streamlit + requests
core/                     ← 业务逻辑（config / cloud / companion / memory / kb / ...）
views/companion.py        ← 界面
data/cards.json           ← 75 张知识卡
assets/avatars/           ← 20 个形象图
.streamlit/config.toml    ← 深色主题（隐藏文件夹，必须一起传）
.streamlit/secrets.toml.example ← Secrets 模板（可传，里面没有真密码）
```

> `run.py` 是给「需要监听 `PORT` 的托管平台」准备的兜底启动器。
> Streamlit Cloud 直接跑 `app.py`，用不到它，放着无害。

---

## 一、先在本机跑通（可选，强烈建议）

```bash
# 在本文件夹里执行
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m streamlit run app.py
```

浏览器打开 http://localhost:8501 。看到「心与星寻」和一句问候就成功了。
随便打一句话，星寻能回你 → 大模型通了。

---

## 二、传到 GitHub

**先建仓库**：https://github.com/new → 仓库名 **`heart-stars-companion`** →
可见性选 **Private（私有）** → **不要**勾 Add a README → 点 **Create repository**

> 建议 **Private**：Streamlit Cloud 支持私有仓库，功能没有任何区别，
> 但不会把工程暴露给搜索引擎。

### 方式 A：网页拖拽（最简单，推荐）

1. 资源管理器打开本文件夹，**双击进入里面**
2. 顶部「**查看 → 显示 → 隐藏的项目**」打开，让 `.streamlit`、`.gitignore` 现形
3. **Ctrl+A 全选里面的全部内容**（注意：不是选中这个文件夹本身）
4. 浏览器进那个空仓库 → **Add file** → **Upload files** → 把选中的内容拖进去
5. 底下点 **Commit changes**

> ⚠️ 两个最容易踩的坑：
> ① 拖的是**文件夹里面的东西**，不是文件夹本身 —— 拖文件夹会多套一层，
> 云端就会报 `No module named 'core'`。
> ② `.streamlit/` 是**隐藏文件夹**，必须一起传，否则深色主题会丢。

### 方式 B：命令行

```bash
git init
git add .
git commit -m "心与星寻 陪伴版"
git branch -M main
git remote add origin https://github.com/你的用户名/heart-stars-companion.git
git push -u origin main
```

> 推之前敲一次 `git status` 看一眼：**不该出现** `.venv/` 和 `.streamlit/secrets.toml`。
> 本文件夹自带的 `.gitignore` 已经把它们排除了。
>
> 两个 `__init__.py`（`core/` 和 `views/` 里各一个）**必须在**，
> 漏了云端会报 `ModuleNotFoundError`。

---

## 三、部署到 Streamlit Cloud

1. 打开 https://share.streamlit.io ，用**你的 GitHub 账号**登录并授权
2. 点 **Create app** → 选 **Deploy a public app from GitHub**
3. 三项填好：
   - **Repository**：`你的用户名/heart-stars-companion`
   - **Branch**：`main`
   - **Main file path**：**`app.py`** ← 就填这个，**不要**加文件夹前缀
4. （可选）**Advanced settings** → Python version 选 3.11 或更高
5. 点 **Deploy!**，等 1–3 分钟装依赖

跑完会拿到一条 `https://xxx.streamlit.app` 的链接，这就是你的陪伴网页。

> 若 **Repository 下拉里看不到**你刚建的私有仓库：点页面上提示的 GitHub 授权入口，
> 在 GitHub 授权页里把这个仓库勾上，再回来刷新即可。

---

## 四、上线之后怎么用

- **直接能用**：打开链接就能聊天、记录心情，不需要登录。
- **5 分钟门槛**：连续使用超过 5 分钟仍未注册 → 会被拦下，引导你去「我的 → 账号」
  用**手机号 + 验证码**注册。这是刻意设计的安全门槛，不是故障。
  想提前解除：进「我的」注册一次即可，会话会记住。
- **关于微信绑定**：桌面浏览器拿不到微信授权，所以**这一端只做手机号注册**。
  要绑微信请用手机端（H5 / 小程序 / App）。
- **数据是通的**：和手机端共用同一个云端账号体系 —— 在手机上注册过的账号，
  在这里同样可以登录，资料、长期记忆、日记都在一起。

---

## 五、这个应用连的是什么

- 云端大模型：`hunyuan-chat`（密钥不在代码里，走应用网关）
- 数据库：用户档案 / 对话 / 日记 / 路灯 / 长期记忆
- 知识卡：`data/cards.json`（75 张，检索后注入提示词）

不需要你配任何环境变量 —— 网关地址与公开 key 已在 `core/config.py` 里。

---

## 六、改代码之后

在 GitHub 网页上直接改，或本地改完 `git push`，Streamlit Cloud **会自动重新部署**，
不用重新配置。

---

## 七、常见问题

**Q：部署后白屏 / 找不到模块**
→ 看 Main file path 是不是 `app.py`。填成 `心与星寻-陪伴版-上传GitHub/app.py` 会找不到。

**Q：报 `No module named 'core'`**
→ `core/` 文件夹没传上去，或者传成了 `core/core/`（拖拽时拖了文件夹本身）。
   仓库首页应该**直接**看到 `core`、`views`、`app.py`。

**Q：报 `ImportError` / `core` 不是包**
→ `core/__init__.py` 或 `views/__init__.py` 漏传了。

**Q：页面能开，但一直转圈 / 显示 connection error**
→ 依赖还在装，等 1–2 分钟刷新；或看右上角 **Manage app** 的日志。

**Q：聊天报错、回复不出来**
→ 云端网关按**来源域名**放行。若你把应用换到了别的域名，需要先把新域名加进云端白名单。

**Q：能像手机版一样绑微信吗**
→ 不能。桌面浏览器拿不到微信授权。微信绑定请用手机端（H5 / 小程序 / App）。
