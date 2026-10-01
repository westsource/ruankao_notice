"""安全相关：限流、CSRF、后台登录态。

限流用进程内内存计数就够了：这个站点单进程、单机部署，订阅接口一天
也不会被调用几次。真到了要多实例的水平，再换 Redis 也不迟——现在
为了「以后可能扩容」而引入外部依赖，是过度设计。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from functools import wraps
from typing import Callable

from flask import current_app, has_request_context, jsonify, redirect, request, session, url_for

_buckets: dict[str, deque[float]] = defaultdict(deque)


def client_ip() -> str:
    """取真实客户端 IP。部署在 Nginx 后面时依赖 X-Forwarded-For 的第一段。

    没有请求上下文时（命令行任务、测试）返回空串——让 service 层
    不必绑死在 HTTP 上，也省得到处塞 try/except。
    """
    if not has_request_context():
        return ""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def rate_limited(bucket: str, limit: int, window_seconds: int) -> bool:
    """返回 True 表示已超限。"""
    key = f"{bucket}:{client_ip()}"
    now_ts = time.monotonic()
    dq = _buckets[key]
    while dq and now_ts - dq[0] > window_seconds:
        dq.popleft()
    if len(dq) >= limit:
        return True
    dq.append(now_ts)
    return False


def reset_rate_limits() -> None:
    _buckets.clear()


# ---------------------------------------------------------------------------
# 无状态 CSRF：令牌由「服务端密钥 + 主体标识」派生，不依赖会话
# ---------------------------------------------------------------------------

def csrf_token(subject: str) -> str:
    secret = current_app.config["SECRET_KEY"]
    return hmac.new(secret.encode(), f"csrf:{subject}".encode(), hashlib.sha256).hexdigest()[:32]


def csrf_ok(subject: str, token: str | None) -> bool:
    if not token:
        return False
    return hmac.compare_digest(csrf_token(subject), token)


# ---------------------------------------------------------------------------
# 后台登录态
# ---------------------------------------------------------------------------

def admin_logged_in() -> bool:
    return bool(session.get("is_admin"))


def login_admin(token: str) -> bool:
    expected = current_app.config["ADMIN_TOKEN"]
    if not hmac.compare_digest(str(token or ""), str(expected)):
        return False
    session["is_admin"] = True
    session.permanent = False
    return True


def logout_admin() -> None:
    session.pop("is_admin", None)


def require_admin(view: Callable) -> Callable:
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not admin_logged_in():
            if request.accept_mimetypes.best == "application/json":
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return redirect(url_for("admin.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# 订阅者登录态（免密码）
#
# 不存密码，也就没有密码库、找回流程、撞库风险。登录唯一凭据是
# 「邮箱里的一次性链接」，点开即建立会话；会话本身是签过名的 cookie，
# 服务端不留任何会话数据。
# ---------------------------------------------------------------------------

def login_subscriber(subscriber_id: int) -> None:
    session.clear()
    session["sub"] = int(subscriber_id)
    session.permanent = True


def current_subscriber_id() -> int | None:
    value = session.get("sub")
    return int(value) if isinstance(value, int) else None


def logout_subscriber() -> None:
    session.pop("sub", None)
