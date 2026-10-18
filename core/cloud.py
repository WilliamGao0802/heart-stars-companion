"""云端客户端（服务端直连，与 packages/adapters/src/app-auth.js / app-llm.js 协议对齐）。

为什么服务端直连可行：浏览器受 CORS 约束（云网关按 Origin 精确匹配白名单，
streamlit.app 不在白名单里会被 403），而服务端 Python 发出的请求**没有 Origin**，
网关放行（实测 Origin:null 也是 200）。所以 Streamlit 版把所有云端调用放在服务端。

协议（与 workbuddy-cloud-sdk / app-auth.js 逐字对齐，端点/字段名勿凭印象改）：
  模型  GET  /.cloud/llm/models                          → 模型目录
  对话  POST /.cloud/llm/chat/completions                → SSE 流式
  发码  POST /.cloud/auth/v1/verification
  验码  POST /.cloud/auth/v1/verification/verify
  注册  POST /.cloud/auth/v1/signup
  登录  POST /.cloud/auth/v1/signin
  续期  POST /.cloud/auth/v1/token
  数据  <METHOD> /.cloud/database/rest/<table>           → PostgREST
"""

import json
import re
import time
from urllib.parse import urlencode

import requests

from .config import ENDPOINT, PUBLISHABLE_KEY

EXPIRY_BUFFER_MS = 60 * 1000   # 提前 1 分钟算过期，避免边界 401
_TIMEOUT = 30
_LLM_TIMEOUT = 180

# ---- PostgREST 入参白名单 ----------------------------------------------------
# 风险：表名、列名、order 都是**直接拼进 URL 路径/查询串**的。只要有一个调用点
#   把用户输入透进来，就能构造出 table=&or=(...) 这类越权查询（PostgREST 的
#   过滤语法本身就是「可编程」的），甚至换一张表读别人的数据。
# 做法：表名只认我们自己的业务表；列名只认标识符；值一律 urlencode。
_TABLES = {
    "chat_messages", "diary_entries", "lamp_entries", "user_profiles",
    "user_memory", "chat_library", "admin_alerts", "user_bindings", "analytics_events",
}
_RPC_FNS = {
    "admin_stats", "admin_users", "admin_user_detail",
    "admin_alerts_list", "admin_alert_update", "admin_library",
    "admin_ping", "admin_metrics",
}
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class CloudError(Exception):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.message = message
        self.code = code


def normalize_phone(phone):
    """手机号归一化（与 SDK identifier.ts 逐字一致）：国内 11 位 → '+86 13800000000'。"""
    digits = re.sub(r"\D", "", str(phone or ""))
    local = digits
    if local.startswith("0086"):
        local = local[4:]
    elif local.startswith("86") and len(local) == 13:
        local = local[2:]
    return ("+86 " + local) if re.fullmatch(r"1\d{10}", local) else str(phone or "")


def _table(name):
    t = str(name or "")
    if t not in _TABLES:
        raise CloudError("表名不在允许范围内", "bad_table")
    return t


def _col(name):
    c = str(name or "")
    if not _IDENT_RE.match(c):
        raise CloudError("列名不合法", "bad_column")
    return c


def _order(expr):
    parts = str(expr or "").split(".")
    if len(parts) == 2 and parts[1] in ("asc", "desc"):
        return _col(parts[0]) + "." + parts[1]
    if len(parts) == 1:
        return _col(parts[0])
    raise CloudError("排序表达式不合法", "bad_sort")


def _qs(params, extra=None):
    """把查询参数编码成查询串：key 走白名单，value 一律百分号编码。"""
    pairs = [(_col(k), str(v)) for k, v in (params or {}).items()]
    if extra:
        pairs.extend(extra)
    return urlencode(pairs)


class Session:
    def __init__(self, access_token="", refresh_token="", expires_at=0, uid="", phone=""):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires_at = expires_at
        self.uid = uid
        self.phone = phone

    @property
    def signed_in(self):
        return bool(self.access_token) and time.time() * 1000 < self.expires_at

    def to_dict(self):
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "expires_at": self.expires_at, "uid": self.uid, "phone": self.phone}

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return None
        s = cls(d.get("access_token", ""), d.get("refresh_token", ""),
                d.get("expires_at", 0), d.get("uid", ""), d.get("phone", ""))
        return s if s.access_token else None


class CloudClient:
    """一个 CloudClient 实例 = 一个用户会话（Streamlit 里存在 session_state）。"""

    def __init__(self, session=None, admin_key=""):
        self.session = session or Session()
        self.admin_key = admin_key
        self._model_id = ""
        self._http = requests.Session()

    # ---------------- 内部 ----------------

    def _headers(self, extra=None, with_auth=True):
        h = {"x-wb-webapp-access-key": PUBLISHABLE_KEY}
        if with_auth and self.session.access_token:
            h["Authorization"] = "Bearer " + self.session.access_token
        # 注意：这里**不放** x-admin-key。云网关不会透传自定义请求头（实测
        #   current_setting('request.headers') 为空），放上去既无效、又会让
        #   管理密钥出现在每一个普通请求的 header 里，白白扩大暴露面。
        #   管理员鉴权统一走 rpc() 的请求体参数 p_key。
        if extra:
            h.update(extra)
        return h

    def _post(self, path, body, extra_headers=None, with_auth=True):
        url = ENDPOINT + path
        headers = self._headers({"Content-Type": "application/json", "Accept": "application/json"}, with_auth)
        if extra_headers:
            headers.update(extra_headers)
        try:
            r = self._http.post(url, data=json.dumps(body), headers=headers, timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise CloudError("网络不太顺：" + str(e), "network")
        if 200 <= r.status_code < 300:
            try:
                return r.json()
            except Exception:
                return {}
        raise CloudError(self._err_text(r), self._err_code(r))

    @staticmethod
    def _err_text(r):
        try:
            d = r.json()
            return d.get("msg") or d.get("error") or d.get("message") or ("请求失败 %s" % r.status_code)
        except Exception:
            return "请求失败 %s" % r.status_code

    @staticmethod
    def _err_code(r):
        try:
            d = r.json()
            return str(d.get("code") or "")
        except Exception:
            return ""

    # ---------------- 账号 ----------------

    def send_code(self, phone):
        p = normalize_phone(phone)
        d = self._post("/.cloud/auth/v1/verification", {"phone_number": p, "target": "ANY"}, with_auth=False)
        return {
            "verificationId": d.get("verification_id") or d.get("verificationId") or "",
            "isExistingUser": bool(d.get("is_existing_user", d.get("isExistingUser"))),
        }

    def verify_code(self, phone, code, password, is_existing, verification_id):
        p = normalize_phone(phone)
        if not verification_id:
            raise CloudError("缺少发码凭据，请重新发送验证码", "bad_request")
        v = self._post("/.cloud/auth/v1/verification/verify",
                       {"verification_id": verification_id, "verification_code": str(code or "")},
                       with_auth=False)
        vt = v.get("verification_token") or v.get("verificationToken") or ""
        if not vt:
            raise CloudError("验证码校验没通过", "bad_request")
        if is_existing:
            s = self._post("/.cloud/auth/v1/signin", {"username": p, "verification_token": vt}, with_auth=False)
        else:
            s = self._post("/.cloud/auth/v1/signup",
                           {"verification_token": vt, "phone_number": p, "password": password}, with_auth=False)
        self._set_session(s, p)
        return {"uid": self.session.uid, "phone": self.session.phone}

    def sign_in(self, phone, password):
        p = normalize_phone(phone)
        s = self._post("/.cloud/auth/v1/signin", {"username": p, "password": password}, with_auth=False)
        self._set_session(s, p)
        return {"uid": self.session.uid, "phone": self.session.phone}

    def _set_session(self, data, phone=""):
        if not data or not data.get("access_token"):
            raise CloudError("登录没有返回会话", "bad_request")
        user = data.get("user") or {}
        self.session = Session(
            data["access_token"],
            data.get("refresh_token", ""),
            time.time() * 1000 + (int(data.get("expires_in") or 3600) * 1000) - EXPIRY_BUFFER_MS,
            user.get("id") or data.get("sub") or "",
            phone or user.get("phone") or "",
        )
        return self.session

    def ensure(self):
        """确保有可用会话：有效直接用；过期静默续期；都没有返回 None（不报错）。"""
        s = self.session
        if not s or not s.access_token:
            return None
        if time.time() * 1000 < s.expires_at:
            return s
        if not s.refresh_token:
            self.session = Session()
            return None
        try:
            d = self._post("/.cloud/auth/v1/token",
                           {"grant_type": "refresh_token", "refresh_token": s.refresh_token},
                           with_auth=False)
            return self._set_session(d, s.phone)
        except CloudError:
            self.session = Session()
            return None

    def sign_out(self):
        self.session = Session()
        return True

    def auth_signed_in(self):
        return self.ensure() is not None

    def auth_uid(self):
        s = self.ensure()
        return (s.uid if s else "") or ""

    # ---------------- 数据（PostgREST） ----------------

    def db_select(self, table, params=None, limit=100, order=None):
        t = _table(table)
        extra = []
        if order:
            extra.append(("order", _order(order)))
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            limit = 100
        extra.append(("limit", str(limit)))
        url = "%s/.cloud/database/rest/%s?%s" % (ENDPOINT, t, _qs(params, extra))
        try:
            r = self._http.get(url, headers=self._headers({"Accept": "application/json"}), timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise CloudError("网络不太顺：" + str(e), "network")
        if 200 <= r.status_code < 300:
            try:
                d = r.json()
                return d if isinstance(d, list) else []
            except Exception:
                return []
        raise CloudError(self._err_text(r), self._err_code(r))

    def db_count(self, table, params=None):
        """用 PostgREST 的 count 精确计数。"""
        t = _table(table)
        url = "%s/.cloud/database/rest/%s?%s" % (ENDPOINT, t, _qs(params, [("select", "*")]))
        try:
            r = self._http.get(url, headers=self._headers({
                "Accept": "application/json",
                "Prefer": "count=exact",
                "Range-Unit": "items",
                "Range": "0-0",
            }), timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise CloudError("网络不太顺：" + str(e), "network")
        cr = r.headers.get("Content-Range", "")   # 形如 "0-0/123"
        if "/" in cr:
            try:
                return int(cr.split("/")[-1])
            except Exception:
                return -1
        return -1

    def db_insert(self, table, rows):
        t = _table(table)
        url = "%s/.cloud/database/rest/%s" % (ENDPOINT, t)
        try:
            r = self._http.post(url, data=json.dumps(rows),
                                headers=self._headers({"Content-Type": "application/json",
                                                       "Accept": "application/json",
                                                       "Prefer": "return=minimal"}), timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise CloudError("网络不太顺：" + str(e), "network")
        if 200 <= r.status_code < 300:
            return True
        raise CloudError(self._err_text(r), self._err_code(r))

    def db_update(self, table, params, patch):
        t = _table(table)
        url = "%s/.cloud/database/rest/%s?%s" % (ENDPOINT, t, _qs(params))
        try:
            r = self._http.patch(url, data=json.dumps(patch),
                                 headers=self._headers({"Content-Type": "application/json",
                                                        "Accept": "application/json",
                                                        "Prefer": "return=minimal"}), timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise CloudError("网络不太顺：" + str(e), "network")
        if 200 <= r.status_code < 300:
            return True
        raise CloudError(self._err_text(r), self._err_code(r))

    def db_upsert(self, table, params, row):
        """有则更新、无则插入（供 memory 用）。"""
        t = _table(table)
        try:
            existing = self.db_select(t, params, limit=1)
        except CloudError:
            existing = []
        if existing:
            return self.db_update(t, params, row)
        payload = dict(row)
        for k, v in (params or {}).items():
            v = str(v)
            if v.startswith("eq."):
                payload[k] = v[3:]
        return self.db_insert(t, payload)

    def db_delete(self, table, params):
        t = _table(table)
        url = "%s/.cloud/database/rest/%s?%s" % (ENDPOINT, t, _qs(params))
        try:
            r = self._http.delete(url, headers=self._headers({"Accept": "application/json"}), timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise CloudError("网络不太顺：" + str(e), "network")
        if 200 <= r.status_code < 300:
            return True
        raise CloudError(self._err_text(r), self._err_code(r))

    # ---------------- RPC（管理员专用） ----------------

    def rpc(self, fn_name, params):
        """调用 PostgREST RPC（SECURITY DEFINER 函数）。

        为什么管理员数据走 RPC 而不是表直读：云网关**不透传自定义请求头**
        （实测 current_setting('request.headers') 为空），所以「x-admin-key 头 +
        RLS 策略」这条路走不通；把密钥放进**请求体参数**里就稳了，函数内部校验密钥。
        """
        if fn_name not in _RPC_FNS:
            raise CloudError("RPC 不在允许范围内", "bad_rpc")
        url = "%s/.cloud/database/rest/rpc/%s" % (ENDPOINT, fn_name)
        try:
            r = self._http.post(url, data=json.dumps(params),
                                headers=self._headers({"Content-Type": "application/json",
                                                       "Accept": "application/json"}, with_auth=False),
                                timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise CloudError("网络不太顺：" + str(e), "network")
        if 200 <= r.status_code < 300:
            try:
                return r.json()
            except Exception:
                return None
        if r.status_code == 404:
            raise CloudError("管理接口不存在（数据库函数未创建）", "no_rpc")
        raise CloudError(self._err_text(r), self._err_code(r))

    # ---------------- 大模型 ----------------

    def models(self):
        url = ENDPOINT + "/.cloud/llm/models"
        try:
            r = self._http.get(url, headers=self._headers({"Accept": "application/json"}), timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise CloudError("网络不太顺：" + str(e), "network")
        if not (200 <= r.status_code < 300):
            raise CloudError(self._err_text(r), "gateway")
        d = r.json()
        raw = d if isinstance(d, list) else (d.get("data") if isinstance(d, dict) else []) or []
        return [{"id": m.get("id") or m.get("model_id"), "disabled": m.get("disabled"),
                 "onlyReasoning": m.get("onlyReasoning")} for m in raw if isinstance(m, dict)]

    def resolve_model(self):
        """选型规则与 model-pick.js 一致：偏好 hunyuan-chat，其次不带 onlyReasoning 的。"""
        if self._model_id:
            return self._model_id
        PREFERRED = ["hunyuan-chat"]
        try:
            models = self.models()
        except CloudError:
            return ""
        usable = [m for m in models if m.get("disabled") is not True and m.get("id")]
        if not usable:
            return ""
        for pid in PREFERRED:
            for m in usable:
                if m["id"] == pid:
                    self._model_id = m["id"]
                    return self._model_id
        for m in usable:
            if m.get("onlyReasoning") is not True:
                self._model_id = m["id"]
                return self._model_id
        self._model_id = usable[0]["id"]
        return self._model_id

    def chat_stream(self, messages, temperature=0.85, conversation_id=None):
        """生成器：逐段 yield 累积后的完整文本（与三端 onDelta(full) 语义一致）。"""
        model_id = self.resolve_model()
        if not model_id:
            raise CloudError("云端模型还没开放", "no_model")
        if not messages or messages[0].get("role") != "system":
            raise CloudError("缺少 system 开头的 messages", "bad_request")

        body = {"model": model_id, "messages": messages, "stream": True, "temperature": temperature}
        if conversation_id:
            body["conversationId"] = conversation_id

        url = ENDPOINT + "/.cloud/llm/chat/completions"
        headers = self._headers({"Content-Type": "application/json", "Accept": "text/event-stream"})
        try:
            r = self._http.post(url, data=json.dumps(body), headers=headers, stream=True, timeout=_LLM_TIMEOUT)
        except requests.RequestException as e:
            raise CloudError("网络不太顺：" + str(e), "request")
        if not (200 <= r.status_code < 300):
            raise CloudError(self._err_text(r), "gateway")

        full = ""
        buf = ""
        for raw in r.iter_content(chunk_size=None):
            if not raw:
                continue
            buf += raw.decode("utf-8", "ignore").replace("\r\n", "\n")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                delta = self._parse_sse(block)
                if delta is None:
                    continue
                if delta == "[DONE]":
                    return
                full += delta
                yield full
        # 尾部可能没有空行分隔
        if buf.strip():
            delta = self._parse_sse(buf)
            if delta and delta != "[DONE]":
                full += delta
                yield full

    @staticmethod
    def _parse_sse(block):
        """解析一段 SSE 事件块，返回增量文本 / '[DONE]' / None。"""
        if not block:
            return None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        data = "\n".join(data_lines)
        if not data:
            return None
        if data == "[DONE]":
            return "[DONE]"
        try:
            chunk = json.loads(data)
        except Exception:
            return None
        if isinstance(chunk, dict) and chunk.get("error"):
            raise CloudError((chunk["error"] or {}).get("message") or "模型那边卡了一下", "gateway")
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        return delta.get("content") or ""

    def chat_once(self, messages, temperature=0.85):
        """非流式封装：把流式拉完返回整段（摘要等短任务用）。"""
        out = ""
        for chunk in self.chat_stream(messages, temperature):
            out = chunk
        return out


def memory_cloud(client):
    """把 CloudClient 包成 memory.py 需要的注入接口（auth_uid/auth_signed_in/db_*）。"""
    from types import SimpleNamespace
    return SimpleNamespace(
        auth_signed_in=client.auth_signed_in,
        auth_uid=client.auth_uid,
        db_select=lambda t, p, limit=1: client.db_select(t, p, limit=limit),
        db_upsert=client.db_upsert,
        db_delete=client.db_delete,
    )
