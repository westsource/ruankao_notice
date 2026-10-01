"""订阅业务逻辑。视图只负责取参数和渲染，规则全部集中在这里。

一个刻意的设计：**任何订阅变更都必须重新点一次邮件里的链接**。

原因很直接——如果已生效的用户拿着自己的邮箱再次提交就把地区改掉了，
那任何人都能用别人的邮箱地址随意改动别人的订阅。所以流程是：

1. 提交 → 把地区写进 ``pending_regions``，生成一次性 token，发确认邮件
2. 点链接 → 才把 ``pending_regions`` 落到 ``subscriptions``，状态置为 active

这样即使别人用你的邮箱乱提交，你也只会在邮箱里看到一封「确认变更」的信，
不点它，什么都不会发生。

同一套 token 机制顺带承担了「免密码登录」：用户忘了链接也没关系，
填邮箱再收一封就行。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from . import scraper
from .db import get_db, query, query_one
from .notify import NotifyError, send_magic_email
from .regions import BY_CODE, REGIONS
from .security import rate_limited
from .util import (
    email_valid,
    hash_ip,
    iso,
    mask_email,
    new_token,
    normalize_email,
    now,
    parse_dt,
)

log = logging.getLogger("ruankao.services")

VERIFY_TTL_HOURS = 48        # 订阅确认链接：用户可能隔一两天才看邮件，给宽一点
LOGIN_TTL_HOURS = 2          # 登录链接：纯粹的即时操作，短一点更安全

MAX_REGIONS = len(REGIONS)

# 同一邮箱每小时最多触发几次验证邮件，防止有人拿别人邮箱刷信
VERIFY_EMAIL_LIMIT = 3
VERIFY_EMAIL_WINDOW = 3600


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def get_subscriber_by_access(token: str):
    if not token:
        return None
    return query_one(get_db(), "SELECT * FROM subscribers WHERE access_token = ?", (token,))


def get_subscriber_by_unsubscribe(token: str):
    if not token:
        return None
    return query_one(get_db(), "SELECT * FROM subscribers WHERE unsubscribe_token = ?", (token,))


def regions_of(subscriber_id: int) -> list[str]:
    rows = query(
        get_db(),
        "SELECT region_code FROM subscriptions WHERE subscriber_id = ? ORDER BY region_code",
        (subscriber_id,),
    )
    return [r["region_code"] for r in rows]


def region_names_of(subscriber_id: int) -> list[str]:
    rows = query(
        get_db(),
        """SELECT r.name FROM subscriptions s
           JOIN regions r ON r.code = s.region_code
           WHERE s.subscriber_id = ?
           ORDER BY r.sort_order""",
        (subscriber_id,),
    )
    return [r["name"] for r in rows]


def parse_pending(row) -> list[str]:
    try:
        data = json.loads(row["pending_regions"] or "[]")
    except (TypeError, ValueError):
        return []
    return [c for c in data if c in BY_CODE]


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def _clean_regions(codes: list[str]) -> list[str]:
    seen: list[str] = []
    for code in codes:
        code = (code or "").strip()
        if code in BY_CODE and code not in seen:
            seen.append(code)
    if not seen:
        raise ServiceError("请至少选择一个报考地区。")
    if len(seen) > MAX_REGIONS:
        raise ServiceError("选择的地区过多。")
    return seen


def subscribe(email: str, region_codes: list[str], ip: str) -> dict:
    """发起订阅或变更订阅。返回给视图用的提示信息。"""
    email = normalize_email(email)
    if not email_valid(email):
        raise ServiceError("邮箱地址格式不对，请检查一下。")

    codes = _clean_regions(region_codes)

    if rate_limited(f"verify-mail:{email}", VERIFY_EMAIL_LIMIT, VERIFY_EMAIL_WINDOW):
        raise ServiceError("这个邮箱刚刚已经发过确认信了，请到收件箱里找找，或稍后再试。", 429)

    conn = get_db()
    row = query_one(conn, "SELECT * FROM subscribers WHERE email = ?", (email,))

    token = new_token()
    expires = iso(now() + timedelta(hours=VERIFY_TTL_HOURS))
    payload = json.dumps(codes)

    if row is None:
        cur = conn.execute(
            """INSERT INTO subscribers
               (email, channel, plan, status, login_token, login_token_expires_at,
                pending_regions, access_token, unsubscribe_token, created_at, ip_hash)
               VALUES (?, 'email', 'free', 'pending', ?, ?, ?, ?, ?, ?, ?)""",
            (
                email, token, expires, payload,
                new_token(20), new_token(20), iso(), hash_ip(ip),
            ),
        )
        subscriber_id = cur.lastrowid
        is_new = True
    else:
        subscriber_id = row["id"]
        is_new = False
        # 状态不动：已生效的用户不会因为这次提交而中断现有的提醒。
        # 变更先落到 pending_regions，等他点了邮件里的链接才真正生效。
        conn.execute(
            """UPDATE subscribers
               SET login_token = ?, login_token_expires_at = ?, pending_regions = ?
               WHERE id = ?""",
            (token, expires, payload, subscriber_id),
        )

    conn.commit()

    unsub_token = query_one(
        conn, "SELECT unsubscribe_token AS t FROM subscribers WHERE id = ?", (subscriber_id,)
    )["t"]
    base = settings_url()
    magic_url = f"{base}/auth/{token}"
    unsubscribe_url = f"{base}/u/{unsub_token}"

    names = "、".join(BY_CODE[c].name for c in codes)
    batch = scraper.latest_batch(conn) or "尚未公布"

    try:
        send_magic_email(
            email,
            magic_url=magic_url,
            purpose="confirm",
            ttl_hours=VERIFY_TTL_HOURS,
            region_names=names,
            batch=batch,
            unsubscribe_url=unsubscribe_url,
        )
    except NotifyError as exc:
        log.error("确认邮件发送失败：%s", exc)
        raise ServiceError(
            "确认邮件发送失败了，可能是发信服务暂时不可用。请稍后再试，或联系站点管理员。", 503
        ) from exc

    return {"email": email, "masked": mask_email(email), "is_new": is_new, "regions": names}


# ---------------------------------------------------------------------------
# 免密码邮箱验证
#
# 本站没有密码，也没有注册。所有身份确认都靠「邮箱里的一次性链接」：
# 订阅时用它确认，日后想改地区或退订也用它登录。这样用户永远不需要
# 记住任何东西，也就没有找回密码这类麻烦事。
# ---------------------------------------------------------------------------

def request_login_link(email: str, ip: str) -> dict:
    """给邮箱发一个登录链接。

    对外表现必须与邮箱是否存在无关——否则这个接口就成了「查询某人是否
    订阅过」的枚举工具。所以无论内部走了哪条分支，返回结构都一样。
    """
    email = normalize_email(email)
    result = {"masked": mask_email(email) if email_valid(email) else "", "sent": False}

    if not email_valid(email):
        return result

    if rate_limited(f"login-mail:{email}", VERIFY_EMAIL_LIMIT, VERIFY_EMAIL_WINDOW):
        return result

    conn = get_db()
    row = query_one(conn, "SELECT * FROM subscribers WHERE email = ?", (email,))
    if row is None:
        # 没订阅过就不发信，但对外不露声色
        return result

    token = new_token()
    conn.execute(
        """UPDATE subscribers
           SET login_token = ?, login_token_expires_at = ?
           WHERE id = ?""",
        (token, iso(now() + timedelta(hours=LOGIN_TTL_HOURS)), row["id"]),
    )
    conn.commit()

    base = settings_url()
    try:
        send_magic_email(
            email,
            magic_url=f"{base}/auth/{token}",
            purpose="login",
            ttl_hours=LOGIN_TTL_HOURS,
            unsubscribe_url=f"{base}/u/{row['unsubscribe_token']}",
        )
    except NotifyError as exc:
        log.error("登录邮件发送失败：%s", exc)
        raise ServiceError(
            "邮件没能发出去，可能是发信服务暂时不可用，请稍后再试。", 503
        ) from exc

    result["sent"] = True
    return result


def consume_login_token(token: str) -> dict:
    """消费一次性链接，并把待确认的地区真正落库。

    过期规则、单次使用、以及「确认订阅」和「登录」共用同一个入口，
    都收在这里，免得两条路径各自实现、各自出岔子。
    """
    conn = get_db()
    row = query_one(conn, "SELECT * FROM subscribers WHERE login_token = ?", (token,))
    if row is None:
        raise ServiceError("这个链接无效，可能已经用过了。", 404)

    expires = parse_dt(row["login_token_expires_at"])
    if expires and now() > expires:
        raise ServiceError(
            "这个链接已经过期了。回到首页重新填一次邮箱，或者重新申请一封登录邮件就行。", 410
        )

    codes = parse_pending(row)
    applied: list[str] = []

    with conn:
        if codes:
            _replace_regions(conn, row["id"], codes)
            applied = codes
        conn.execute(
            """UPDATE subscribers
               SET status = CASE WHEN status = 'pending' THEN 'active' ELSE status END,
                   verified_at = COALESCE(verified_at, ?),
                   login_token = NULL, login_token_expires_at = NULL,
                   pending_regions = NULL
               WHERE id = ?""",
            (iso(), row["id"]),
        )

    return {
        "subscriber_id": row["id"],
        "email": row["email"],
        "status": row["status"],
        "applied_regions": applied,
        "region_names": region_names_of(row["id"]),
        "just_activated": row["status"] == "pending",
    }


def settings_url() -> str:
    from .config import settings

    return settings.site_host


def update_regions(subscriber_id: int, codes: list[str]) -> None:
    """管理页里直接改地区。能走到这里说明用户已经持有 access_token。"""
    clean = _clean_regions(codes)
    conn = get_db()
    with conn:
        _replace_regions(conn, subscriber_id, clean)


def set_status(subscriber_id: int, status: str) -> None:
    if status not in {"active", "paused"}:
        raise ServiceError("无效的状态。")
    conn = get_db()
    conn.execute("UPDATE subscribers SET status = ? WHERE id = ?", (status, subscriber_id))
    conn.commit()


def unsubscribe(subscriber_id: int) -> None:
    """一键退订。保留记录（避免重复入库），但不再发送任何通知。"""
    conn = get_db()
    conn.execute(
        "UPDATE subscribers SET status = 'unsubscribed', login_token = NULL, pending_regions = NULL WHERE id = ?",
        (subscriber_id,),
    )
    conn.commit()


def delete_subscriber(subscriber_id: int) -> None:
    """彻底删除。《个人信息保护法》要求提供删除途径，这个是硬的，不能省。"""
    conn = get_db()
    conn.execute("DELETE FROM subscribers WHERE id = ?", (subscriber_id,))
    conn.commit()


def record_feedback(subscriber_id: int, window_id: int, action: str) -> None:
    if action not in {"registered", "paid", "skip"}:
        raise ServiceError("无效的操作。")
    conn = get_db()
    conn.execute(
        """INSERT INTO feedback (subscriber_id, window_id, action, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(subscriber_id, window_id) DO UPDATE SET
               action = excluded.action, created_at = excluded.created_at""",
        (subscriber_id, window_id, action, iso()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 共享的写入辅助
# ---------------------------------------------------------------------------

def _replace_regions(conn, subscriber_id: int, codes: list[str]) -> None:
    """把这批地区设成该用户的全部订阅：缺的补上，没勾的删掉。"""
    for code in codes:
        conn.execute(
            """INSERT OR IGNORE INTO subscriptions (subscriber_id, region_code, created_at)
               VALUES (?, ?, ?)""",
            (subscriber_id, code, iso()),
        )
    placeholders = ",".join("?" for _ in codes)
    conn.execute(
        f"""DELETE FROM subscriptions
            WHERE subscriber_id = ? AND region_code NOT IN ({placeholders})""",
        (subscriber_id, *codes),
    )



# ---------------------------------------------------------------------------
# 展示用查询
# ---------------------------------------------------------------------------

def latest_windows(conn, batch: str | None = None) -> tuple[list[dict], str | None]:
    """当前批次的全部窗口，按地区顺序排好。"""
    batch = batch or scraper.latest_batch(conn)
    if not batch:
        return [], None
    rows = query(
        conn,
        """SELECT w.*, r.name AS region_name, r.grp AS region_group, r.sort_order
           FROM exam_windows w
           JOIN regions r ON r.code = w.region_code
           WHERE w.exam_type = ? AND w.batch = ?
           ORDER BY r.sort_order""",
        (scraper.EXAM_TYPE, batch),
    )
    return [dict(r) for r in rows], batch


def window_state(window: dict, moment=None) -> str:
    """给页面用的状态标记：open / closed / upcoming / unknown。"""
    moment = moment or now()
    start = parse_dt(window.get("sign_start"))
    end = parse_dt(window.get("sign_end"))
    if start is None and end is None:
        return "unknown"
    if start and moment < start:
        return "upcoming"
    if end and moment > end:
        return "closed"
    return "open"


STATE_LABEL = {
    "open": "报名进行中",
    "closed": "报名已结束",
    "upcoming": "尚未开始",
    "unknown": "时间未公布",
}


def current_batch_summary(conn) -> dict:
    """首页右侧状态卡用的一小撮数字。"""
    windows, batch = latest_windows(conn)
    states = [window_state(w) for w in windows] if windows else []
    last_run = query_one(
        conn, "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 1"
    )
    return {
        "batch": batch,
        "total": len(windows),
        "open": states.count("open"),
        "upcoming": states.count("upcoming"),
        "closed": states.count("closed"),
        "unknown": states.count("unknown"),
        "last_run": dict(last_run) if last_run else None,
        "subscribers": query_one(
            conn, "SELECT COUNT(*) AS c FROM subscribers WHERE status = 'active'"
        )["c"],
        "windows": windows,
    }
