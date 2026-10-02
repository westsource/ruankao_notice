"""管理页。

身份由一次性邮件链接建立会话，之后所有操作都在会话内完成。
这样页面上不再出现任何长期有效的凭据，用户也可以放心把它加进书签。

刻意不做账号体系——没有注册、没有密码、也没有找回密码。少一整套
凭据管理，就少一整套泄露风险。
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for

from .. import services
from ..db import get_db, query_one
from ..security import csrf_ok, csrf_token, current_subscriber_id, login_subscriber
from ..util import mask_email

bp = Blueprint("manage", __name__, url_prefix="/m")


def _current():
    subscriber_id = current_subscriber_id()
    if subscriber_id is None:
        return None
    return query_one(get_db(), "SELECT * FROM subscribers WHERE id = ?", (subscriber_id,))


def _csrf(subscriber) -> str:
    return csrf_token(f"manage:{subscriber['id']}")


@bp.get("/")
def page():
    subscriber = _current()
    if subscriber is None:
        return redirect(url_for("public.login"))

    return render_template(
        "manage.html",
        subscriber=subscriber,
        selected=services.regions_of(subscriber["id"]),
        csrf=_csrf(subscriber),
        welcome=request.args.get("welcome"),
        saved=request.args.get("saved"),
        masked_email=mask_email(subscriber["email"] or ""),
    )


@bp.get("/<token>")
def page_by_token(token: str):
    """凭 access_token 直接进入。

    邮件底部的管理链接就是这个格式：点一下直接进管理页，不用再走一遍
    「填邮箱 → 收信 → 点链接」。进来之后会顺手建立会话，后续操作都在会话里完成。
    """
    subscriber = services.get_subscriber_by_access(token)
    if subscriber is None:
        return (
            render_template(
                "message.html",
                title="链接无效",
                body="这个管理链接不认识。它可能来自一封很旧的邮件，或者地址被截断了。",
            ),
            404,
        )
    login_subscriber(subscriber["id"])
    return redirect(url_for("manage.page"))


def _guarded():
    subscriber = _current()
    if subscriber is None:
        return None, redirect(url_for("public.login"))
    if not csrf_ok(f"manage:{subscriber['id']}", request.form.get("csrf")):
        return None, (
            render_template(
                "message.html", title="请求已过期", body="请回到管理页重新操作一次。", tone="warn"
            ),
            403,
        )
    return subscriber, None


@bp.post("/regions")
def update_regions():
    subscriber, error = _guarded()
    if error:
        return error
    try:
        services.update_regions(subscriber["id"], request.form.getlist("region"))
        if subscriber["status"] == "unsubscribed":
            services.set_status(subscriber["id"], "active")
    except services.ServiceError as exc:
        return render_template("message.html", title="没能保存", body=exc.message, tone="warn")
    return redirect(url_for("manage.page", saved=1))


@bp.post("/status")
def update_status():
    subscriber, error = _guarded()
    if error:
        return error
    target = "paused" if subscriber["status"] == "active" else "active"
    services.set_status(subscriber["id"], target)
    return redirect(url_for("manage.page", saved=1))


@bp.post("/unsubscribe")
def unsubscribe():
    """管理页里取消订阅：只停发提醒，记录保留，日后可重新订阅。"""
    subscriber, error = _guarded()
    if error:
        return error
    services.unsubscribe(subscriber["id"])
    return redirect(url_for("manage.page"))
