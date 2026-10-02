"""面向访客的页面：首页、订阅、确认、时间表、退订、回执、政策页。"""

from __future__ import annotations

import logging

from flask import Blueprint, abort, redirect, render_template, request, url_for

from .. import scraper, services
from ..db import get_db, query_one
from ..security import (
    client_ip,
    current_subscriber_id,
    login_subscriber,
    logout_subscriber,
    rate_limited,
)
from ..util import email_valid, mask_email, normalize_email

log = logging.getLogger("ruankao.views.public")

bp = Blueprint("public", __name__)


@bp.get("/")
def index():
    conn = get_db()
    summary = services.current_batch_summary(conn)

    # 首页只挑几条正当时的展示，全量在 /schedule
    highlights: list[dict] = []
    for state in ("open", "upcoming"):
        for window in summary["windows"]:
            if len(highlights) >= 4:
                break
            if services.window_state(window) == state:
                window["state_label"] = services.STATE_LABEL[state]
                highlights.append(window)

    return render_template("index.html", summary=summary, highlights=highlights)


@bp.post("/subscribe")
def subscribe():
    if rate_limited("subscribe", 12, 3600):
        abort(429)

    email = request.form.get("email", "")
    codes = request.form.getlist("region")

    try:
        info = services.subscribe(email, codes, client_ip())
    except services.ServiceError as exc:
        return (
            render_template(
                "message.html",
                title="没能提交成功",
                body=exc.message,
                back_to_form=True,
                tone="warn",
            ),
            exc.status,
        )

    body = (
        f"我们已向 {info['masked']} 发送了一封确认邮件。"
        "请点开里面的确认按钮，订阅才会真正生效——这一步是为了防止有人用你的邮箱乱订阅。"
    )
    return render_template(
        "message.html",
        title="就差最后一步：去邮箱确认",
        body=body,
        hint=f"本次选择的地区：{info['regions']}",
        tone="ok",
    )


@bp.get("/auth/<token>")
def auth(token: str):
    """一次性链接的统一入口。

    确认订阅、登录管理，两件事共用这一个入口——都是「证明你是这个邮箱的主人」，
    没必要写成两套流程，否则过期规则、单次使用的判断迟早会在两边跑偏。
    """
    try:
        info = services.consume_login_token(token)
    except services.ServiceError as exc:
        return (
            render_template(
                "message.html",
                title="链接没能用上",
                body=exc.message,
                hint="本站没有密码，重新申请一封登录邮件即可，入口在页面右上角的「管理订阅」。",
                tone="warn",
            ),
            exc.status,
        )

    login_subscriber(info["subscriber_id"])
    return redirect(url_for("manage.page", welcome=1))


@bp.get("/verify/<token>")
def verify_legacy(token: str):
    """老邮件里可能存在的 /verify 链接。

    邮件一旦发出去就收不回来了，所以这个入口不能删——哪怕它只是转个弯。
    """
    return redirect(url_for("public.auth", token=token))


@bp.get("/login")
def login():
    # 已经登录过就直接进管理页，别让用户再走一遍邮件流程
    if current_subscriber_id() is not None:
        return redirect(url_for("manage.page"))
    return render_template("login.html")


@bp.post("/login")
def login_submit():
    """申请登录链接。

    无论这个邮箱是否订阅过，页面文案完全一致。否则这个接口就成了
    「查询某人有没有在本站订阅过」的枚举工具。
    """
    if rate_limited("login-page", 30, 3600):
        abort(429)

    email = normalize_email(request.form.get("email", ""))
    if not email_valid(email):
        return render_template("login.html", error="邮箱地址格式不对，请检查一下。", email=email), 400

    try:
        services.request_login_link(email, client_ip())
    except services.ServiceError as exc:
        return render_template("login.html", error=exc.message, email=email), exc.status

    return render_template("login.html", sent=True, masked=mask_email(email))


@bp.post("/logout")
def logout():
    logout_subscriber()
    return redirect(url_for("public.index"))


@bp.get("/schedule")
def schedule():
    conn = get_db()
    windows, batch = services.latest_windows(conn)

    groups: dict[str, list[dict]] = {}
    for window in windows:
        window["state"] = services.window_state(window)
        window["state_label"] = services.STATE_LABEL[window["state"]]
        groups.setdefault(window["region_group"], []).append(window)

    last_run = query_one(conn, "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1")
    counts = {
        state: sum(1 for w in windows if w["state"] == state)
        for state in ("open", "upcoming", "closed", "unknown")
    }

    return render_template(
        "schedule.html",
        groups=groups,
        batch=batch,
        last_run=dict(last_run) if last_run else None,
        counts=counts,
        total=len(windows),
    )


@bp.get("/u/<token>")
def unsubscribe_page(token: str):
    """先给一个确认页，点击才真正退订。

    为什么不直接在 GET 里退订：邮件客户端和网关会预取链接，
    预取就等于替你退订了，这是很多提醒服务翻过车的地方。
    """
    subscriber = services.get_subscriber_by_unsubscribe(token)
    if subscriber is None:
        return (
            render_template("message.html", title="链接无效", body="这个退订链接不认识，可能已经用过了。"),
            404,
        )
    if subscriber["status"] == "unsubscribed":
        return render_template(
            "message.html", title="你已经退订了", body="这个邮箱目前不会再收到任何提醒。", tone="ok"
        )
    return render_template(
        "unsubscribe.html",
        token=token,
        email=mask_email(subscriber["email"] or ""),
        regions=services.region_names_of(subscriber["id"]),
    )


@bp.post("/u/<token>")
def unsubscribe_submit(token: str):
    subscriber = services.get_subscriber_by_unsubscribe(token)
    if subscriber is None:
        return (
            render_template("message.html", title="链接无效", body="这个退订链接不认识，可能已经用过了。"),
            404,
        )
    services.unsubscribe(subscriber["id"])
    return render_template(
        "message.html",
        title="已退订",
        body="不会再收到任何提醒了。想重新订阅，回首页再填一次邮箱就行。",
        hint="如需彻底删除邮箱与订阅记录，请邮件联系站点管理员。",
        tone="ok",
    )


@bp.get("/f/<token>/<int:window_id>/<action>")
def feedback(token: str, window_id: int, action: str):
    """邮件里的回执按钮。命中后就不再重复催同一件事。"""
    subscriber = services.get_subscriber_by_access(token)
    if subscriber is None:
        return (
            render_template("message.html", title="链接无效", body="这个链接不认识，可能来自很久以前的邮件。"),
            404,
        )
    try:
        services.record_feedback(subscriber["id"], window_id, action)
    except services.ServiceError as exc:
        return render_template("message.html", title="没能记录", body=exc.message, tone="warn")

    text = {
        "registered": ("记下了", "既然已经报上名，这次就不再提醒你报名了。别忘了按时缴费——缴费成功才算报名成功。"),
        "paid": ("记下了", "缴费完成，这次考试就等你上岸了。"),
        "skip": ("好的，不打扰了", "这个批次不再向你这个邮箱发送提醒。以后想重新订阅，回首页填一次就行。"),
    }[action]
    return render_template("message.html", title=text[0], body=text[1], tone="ok")


@bp.get("/privacy")
def privacy():
    return render_template("privacy.html")


@bp.get("/terms")
def terms():
    return render_template("terms.html")


@bp.get("/healthz")
def healthz():
    conn = get_db()
    batch = scraper.latest_batch(conn)
    last_run = query_one(conn, "SELECT ok, finished_at FROM scrape_runs ORDER BY id DESC LIMIT 1")
    return {
        "ok": True,
        "batch": batch,
        "last_scrape_ok": bool(last_run["ok"]) if last_run else None,
        "last_scrape_at": last_run["finished_at"] if last_run else None,
    }
