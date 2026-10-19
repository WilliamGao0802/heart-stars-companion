"""管理后台数据访问（core 层，不依赖 streamlit）。

所有读操作走 PostgREST RPC（SECURITY DEFINER 函数），密钥在请求体里传。
写操作（禁用/备注等）暂时不做——后台定位是「看」，不误伤线上数据。
"""

import os

from .cloud import CloudClient, CloudError


def default_admin_key():
    """只从环境变量读取。**不再有内置默认值**。

    安全要求：密钥一旦写进源码，就等于把全站数据（日记、对话、记忆）
    的读权限一起公开——仓库可能被分享、被 fork、被打包上传。
    所以缺配置时宁可报错停摆，也不退化成弱口令。
    """
    key = os.environ.get("HSX_ADMIN_KEY", "").strip()
    if not key:
        raise AdminError(
            "未配置 HSX_ADMIN_KEY。请在 .streamlit/secrets.toml 的 [admin].db_key "
            "或环境变量里提供管理密钥。",
            "no_key",
        )
    return key


def make_admin_client(key=None):
    """管理员客户端：不带用户会话，只带管理密钥。"""
    return _client(key)


def _client(key):
    c = CloudClient()
    c._admin_key = key or default_admin_key()
    return c


class AdminAPI:
    def __init__(self, key=None):
        self.key = key or default_admin_key()
        self.client = _client(self.key)

    def _rpc(self, fn, params=None):
        p = {"p_key": self.key}
        if params:
            p.update(params)
        try:
            return self.client.rpc(fn, p)
        except CloudError as e:
            raise AdminError(e.message, e.code)

    # ---------------- 接口 ----------------

    def stats(self):
        return self._rpc("admin_stats") or {}

    def metrics(self):
        return self._rpc("admin_metrics") or {}

    def users(self, q="", limit=200, offset=0):
        return self._rpc("admin_users", {"p_limit": limit, "p_offset": offset, "p_q": q}) or []

    def user_detail(self, uid):
        return self._rpc("admin_user_detail", {"p_uid": uid}) or {}

    def alerts(self, limit=200):
        return self._rpc("admin_alerts_list", {"p_limit": limit}) or []

    def alert_update(self, alert_id, status, handler="", note="", referral_path=""):
        """推进警报台账状态（唯一允许后台写库的入口）。

        白名单只有四个字段，时间戳由服务端按状态自动落 —— 客户端传时间会被伪造，
        所以这里刻意不开放 created_at / acked_at 之类的参数。
        """
        if status not in ("open", "acked", "referred", "closed"):
            raise AdminError("非法的状态值：%s" % status, "bad_status")
        if not str(handler or "").strip():
            raise AdminError("必须填写处理人（台账要能回答「谁处理了」）", "no_handler")
        return self._rpc("admin_alert_update", {
            "p_id": int(alert_id),
            "p_status": status,
            "p_handler": str(handler).strip(),
            "p_note": str(note or "").strip(),
            "p_referral_path": str(referral_path or "").strip(),
        }) or {}

    def library(self, q="", limit=800):
        return self._rpc("admin_library", {"p_q": q, "p_limit": limit}) or []

    def turn_distribution(self):
        """对话轮次档位分布（数量占比页）。云端未部署时 AdminError(no_rpc)。"""
        return self._rpc("admin_turn_distribution") or {}

    def hour_distribution(self):
        """近 7 天活跃的时段分布（数量占比页）。云端未部署时 AdminError(no_rpc)。"""
        return self._rpc("admin_hour_distribution") or {}

    def ping(self):
        try:
            return self.client.rpc("admin_stats", {"p_key": self.key}) is not None
        except CloudError:
            return False


class AdminError(Exception):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.message = message
        self.code = code

    @property
    def friendly(self):
        if self.code == "DATABASE_P0001" or "forbidden" in self.message:
            return "管理密钥不正确"
        if self.code == "no_rpc" or "does not exist" in self.message:
            return "后台数据库函数还没建好（请先执行建函数脚本）"
        return self.message
