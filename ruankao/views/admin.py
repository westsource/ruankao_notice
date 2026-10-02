"""后台：看状态、手动抓取、看订阅与发送记录。

入口是 /admin，口令来自 .env 的 ADMIN_TOKEN。仅靠一个口令保护，所以：
- 部署时务必改掉默认值（启动日志会主动提醒）
- 考虑在 Nginx 层再加一层 IP 白名单或 Basic Auth
"""

from __future__ import annotations

import logging

from flask import Blueprint, redirect, render_template, request, session, url_for

from .. import services
from ..db import get_db, query, query_one
from ..notify import channel_status
from ..regions import BY_CODE
from ..scheduler import run_all, run_notify_job, run_scrape_job
from ..security import csrf_ok, csrf_token, login_admin, logout_admin, require_admin

log = logging.getLogger("ruankao.views.admin")

bp = Blueprint("admin", __name__, url_prefix="/admin")

SUBSCRIBER_STATUSES = ("pending", "active", "paused", "unsubscribed")
SUBSCRIBERS_PAGE_SIZE = 50

FEEDBACK_LABELS = {"registered": "已报名", "paid": "已缴费", "skip": "不打扰"}


def _safe_next(target: str | None) -> str:
    """只允许跳回站内相对路径，避免开放重定向。"""
    if not target or not target.startswith("/") or target.startswith("//"):
        return url_for("admin.dashboard")
    return target


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if login_admin(request.form.get("token", "")):
            return redirect(_safe_next(request.args.get("next")))
        error = "口令不对。"
        log.warning("后台登录失败，来源 %s", request.remote_addr)
    return render_template("admin_login.html", error=error, csrf=csrf_token("admin-login"))


@bp.post("/logout")
def logout():
    logout_admin()
    return redirect(url_for("admin.login"))


@bp.get("/")
@require_admin
def dashboard():
    conn = get_db()
    summary = services.current_batch_summary(conn)

    runs = query(conn, "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 8")
    notifications = query(
        conn,
        """SELECT n.*, s.email, r.name AS region_name
           FROM notify_log n
           JOIN subscribers s ON s.id = n.subscriber_id
           JOIN exam_windows w ON w.id = n.window_id
           JOIN regions r ON r.code = w.region_code
           ORDER BY n.id DESC LIMIT 15""",
        (),
    )
    subscriber_stats = query_one(
        conn,
        """SELECT
             COUNT(*)                                              AS total,
             SUM(CASE WHEN status = 'active'       THEN 1 ELSE 0 END) AS active,
             SUM(CASE WHEN status = 'pending'      THEN 1 ELSE 0 END) AS pending,
             SUM(CASE WHEN status = 'paused'       THEN 1 ELSE 0 END) AS paused,
             SUM(CASE WHEN status = 'unsubscribed' THEN 1 ELSE 0 END) AS unsubscribed
           FROM subscribers"""
    )

    top_regions = query(
        conn,
        """SELECT r.name, COUNT(*) AS c
           FROM subscriptions sub JOIN regions r ON r.code = sub.region_code
           GROUP BY sub.region_code ORDER BY c DESC, r.sort_order LIMIT 10""",
    )

    return render_template(
        "admin.html",
        summary=summary,
        channels=channel_status(),
        runs=[dict(r) for r in runs],
        notifications=[dict(n) for n in notifications],
        stats=dict(subscriber_stats) if subscriber_stats else {},
        top_regions=[dict(r) for r in top_regions],
        csrf=csrf_token("admin"),
        flash=request.args.get("done"),
    )


@bp.post("/scrape")
@require_admin
def manual_scrape():
    if not csrf_ok("admin", request.form.get("csrf")):
        return redirect(url_for("admin.dashboard"))
    result = run_scrape_job()
    log.info("后台手动抓取：%s", result)
    return redirect(url_for("admin.dashboard", done=f"抓取完成：{result}"))


@bp.post("/notify")
@require_admin
def manual_notify():
    if not csrf_ok("admin", request.form.get("csrf")):
        return redirect(url_for("admin.dashboard"))
    stats = run_notify_job()
    log.info("后台手动发送：%s", stats)
    return redirect(url_for("admin.dashboard", done=f"提醒任务完成：{stats}"))


@bp.post("/run-all")
@require_admin
def manual_run_all():
    if not csrf_ok("admin", request.form.get("csrf")):
        return redirect(url_for("admin.dashboard"))
    result = run_all()
    return redirect(url_for("admin.dashboard", done=f"全流程完成：{result}"))


@bp.get("/subscribers")
@require_admin
def subscribers():
    conn = get_db()

    status = request.args.get("status") or ""
    region = request.args.get("region") or ""
    keyword = (request.args.get("q") or "").strip()

    where: list[str] = []
    params: list = []
    if status in SUBSCRIBER_STATUSES:
        where.append("s.status = ?")
        params.append(status)
    if region in BY_CODE:
        where.append(
            "EXISTS (SELECT 1 FROM subscriptions sub"
            " WHERE sub.subscriber_id = s.id AND sub.region_code = ?)"
        )
        params.append(region)
    if keyword:
        where.append("s.email LIKE ?")
        params.append(f"%{keyword.lower()}%")
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    matched = query_one(conn, f"SELECT COUNT(*) AS c FROM subscribers s {clause}", params)["c"]

    try:
        page = int(request.args.get("page") or 1)
    except ValueError:
        page = 1
    pages = max(1, -(-matched // SUBSCRIBERS_PAGE_SIZE))
    page = min(max(1, page), pages)
    offset = (page - 1) * SUBSCRIBERS_PAGE_SIZE

    rows = query(
        conn,
        f"""SELECT s.*,
                   (SELECT COUNT(*) FROM subscriptions sub
                     WHERE sub.subscriber_id = s.id)                      AS region_count,
                   (SELECT GROUP_CONCAT(name, '、') FROM (
                        SELECT r.name AS name
                        FROM subscriptions sub JOIN regions r ON r.code = sub.region_code
                        WHERE sub.subscriber_id = s.id
                        ORDER BY r.sort_order))                           AS region_names,
                   (SELECT COUNT(*) FROM notify_log n
                     WHERE n.subscriber_id = s.id AND n.status = 'sent')   AS sent_count,
                   (SELECT COUNT(*) FROM notify_log n
                     WHERE n.subscriber_id = s.id AND n.status = 'failed') AS failed_count,
                   (SELECT MAX(n.sent_at) FROM notify_log n
                     WHERE n.subscriber_id = s.id)                        AS last_sent_at,
                   (SELECT GROUP_CONCAT(action) FROM feedback f
                     WHERE f.subscriber_id = s.id)                        AS feedback_actions
              FROM subscribers s
              {clause}
             ORDER BY s.id DESC
             LIMIT ? OFFSET ?""",
        [*params, SUBSCRIBERS_PAGE_SIZE, offset],
    )

    items: list[dict] = []
    for row in rows:
        item = dict(row)
        item["pending_names"] = "、".join(
            BY_CODE[c].name for c in services.parse_pending(row)
        )
        counts: dict[str, int] = {}
        for action in (row["feedback_actions"] or "").split(","):
            if action:
                counts[action] = counts.get(action, 0) + 1
        item["feedback_text"] = "、".join(
            f"{FEEDBACK_LABELS.get(a, a)}×{c}" for a, c in counts.items()
        )
        items.append(item)

    totals = query_one(
        conn,
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active
             FROM subscribers""",
    )

    return render_template(
        "admin_subscribers.html",
        rows=items,
        matched=matched,
        page=page,
        pages=pages,
        page_size=SUBSCRIBERS_PAGE_SIZE,
        total=totals["total"] if totals else 0,
        active=totals["active"] if totals else 0,
        status=status,
        region=region,
        q=keyword,
        filters={k: v for k, v in (("status", status), ("region", region), ("q", keyword)) if v},
        csrf=csrf_token("admin"),
    )
