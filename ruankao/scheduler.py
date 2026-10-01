"""调度器：定时抓取 → 变化检测 → 按订阅关系发出提醒。

提醒有四个节点，不是只发一封：

===============  ==========================================  ==========================
节点 kind         触发条件                                     收件人筛选
===============  ==========================================  ==========================
``published``    报名时间公布，且距开放不足 14 天              所有订阅该地区的人
``open``         已到报名起始时间、尚未截止                    所有订阅该地区的人
``closing``      距报名截止不足 48 小时                        未回执「已报名」的人
``pay_closing``  距缴费截止不足 24 小时                        未回执「已缴费」的人
===============  ==========================================  ==========================

两个关键约束
------------
1. **只处理当前批次**。历史批次的窗口留在库里供人查阅，但绝不能拿来发通知，
   否则新订阅的用户一进来就会收到「2026 下半年报名已开始」这种过期消息。
2. **一个订阅者对同一窗口，每轮只发一封**。同一天可能同时命中 ``closing``
   和 ``pay_closing``，按紧急度取一封，避免连发两封把人烦到退订。
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import scraper
from .config import settings
from .db import get_db, query, query_one
from .notify import NotifyError, send_admin_alert, send_notice_email
from .util import CN_TZ, iso, now, parse_dt, today

log = logging.getLogger("ruankao.scheduler")

# 紧急度从高到低。同一轮命中多个时取第一个。
KIND_PRIORITY: tuple[str, ...] = ("pay_closing", "closing", "open", "published")

PUBLISHED_AHEAD = timedelta(days=14)
CLOSING_AHEAD = timedelta(hours=48)
PAY_CLOSING_AHEAD = timedelta(hours=24)

# 用户回执能屏蔽掉哪些节点。这里是「动作 -> 被屏蔽的节点」，不是反过来：
# 用户说「我已报名」之后，就不该再催他报名了，但**缴费提醒必须保留**——
# 报完名不缴费等于没报，这恰恰是最后一道保险。
_FEEDBACK_BLOCK: dict[str, set[str]] = {
    "registered": {"published", "open", "closing"},
    "paid": {"published", "open", "closing", "pay_closing"},
    "skip": {"published", "open", "closing", "pay_closing"},
}


@dataclass
class Pending:
    subscriber_id: int
    email: str | None
    channel: str
    access_token: str
    unsubscribe_token: str
    window_id: int
    region_code: str
    region_name: str
    batch: str
    kind: str
    days_left: int | None
    window: dict


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------

def run_scrape_job() -> dict:
    """完整跑一轮抓取。返回一份可供后台与日志查看的摘要。"""
    conn = get_db()
    started = iso()
    log.info("开始抓取官方报名平台 …")

    try:
        result = scraper.scrape()
    except scraper.ScrapeError as exc:
        scraper.record_run(conn, started_at=started, ok=False, error=str(exc))
        log.error("抓取失败：%s", exc)
        send_admin_alert(
            "抓取失败",
            [
                f"时间：{started}",
                f"数据源：{scraper.SOURCE_URL}",
                f"错误：{exc}",
                "",
                "在修复之前，所有用户都不会收到新的提醒。请尽快人工核对官方页面。",
            ],
        )
        return {"ok": False, "error": str(exc), "rows": 0, "changed": 0}

    changes = scraper.persist(conn, result)
    scraper.record_run(
        conn, started_at=started, ok=True, batch=result.batch,
        rows=len(result.windows), changed=len(changes),
    )

    log.info("抓取完成：批次 %s，%d 个机构，%d 处变化", result.batch, len(result.windows), len(changes))

    # 解析出现异常信号时告警——宁可多提醒运维，也不要静默地错下去
    if result.unknown_names or result.warnings:
        send_admin_alert(
            "抓取结果异常，请人工核对",
            [
                f"时间：{started}",
                f"批次：{result.batch}",
                f"解析到 {len(result.windows)} 个机构",
                f"未识别的机构：{result.unknown_names or '无'}",
                "警告：",
                *[f"  - {w}" for w in result.warnings],
                "",
                "官方页面可能已改版，请检查后更新解析规则。",
            ],
        )

    return {
        "ok": True,
        "batch": result.batch,
        "rows": len(result.windows),
        "changed": len(changes),
        "warnings": result.warnings,
        "unknown": result.unknown_names,
    }


# ---------------------------------------------------------------------------
# 提醒判定
# ---------------------------------------------------------------------------

def _candidate_kinds(window: dict, moment: datetime) -> list[str]:
    sign_start = parse_dt(window["sign_start"])
    sign_end = parse_dt(window["sign_end"])
    pay_end = parse_dt(window["pay_end"])

    # 官方还没公布时间，不发任何通知
    if sign_start is None and sign_end is None:
        return []

    kinds: list[str] = []

    # 缴费截止（最紧急，很多人的失手点）
    if pay_end and pay_end - PAY_CLOSING_AHEAD <= moment < pay_end:
        kinds.append("pay_closing")

    # 报名截止
    if sign_end and sign_end - CLOSING_AHEAD <= moment < sign_end:
        kinds.append("closing")

    # 报名进行中
    if sign_start and moment >= sign_start:
        upper = sign_end or (sign_start + timedelta(days=20))
        if moment < upper:
            kinds.append("open")

    # 时间已公布、尚未开放
    if sign_start and moment < sign_start and sign_start - moment <= PUBLISHED_AHEAD:
        kinds.append("published")

    return [k for k in KIND_PRIORITY if k in kinds]


def _days_left(end_text: str | None, moment: datetime) -> int | None:
    end = parse_dt(end_text)
    if not end:
        return None
    seconds = (end - moment).total_seconds()
    return max(0, math.ceil(seconds / 86400))


def collect_pending(conn, moment: datetime | None = None) -> list[Pending]:
    """算出这一轮该发哪些提醒。不发送，只判定。"""
    moment = moment or now()
    batch = scraper.latest_batch(conn)
    if not batch:
        return []

    rows = query(
        conn,
        """SELECT w.id            AS window_id,
                  w.region_code   AS region_code,
                  r.name          AS region_name,
                  w.batch         AS batch,
                  w.sign_start, w.sign_end, w.pay_start, w.pay_end,
                  w.entry_url,
                  s.id                AS subscriber_id,
                  s.email, s.channel,
                  s.access_token, s.unsubscribe_token,
                  f.action            AS feedback_action
           FROM exam_windows w
           JOIN subscriptions sub ON sub.region_code = w.region_code
           JOIN subscribers   s   ON s.id = sub.subscriber_id
           JOIN regions       r   ON r.code = w.region_code
           LEFT JOIN feedback f   ON f.subscriber_id = s.id AND f.window_id = w.id
           WHERE w.exam_type = ? AND w.batch = ? AND s.status = 'active'""",
        (scraper.EXAM_TYPE, batch),
    )

    sent_rows = query(conn, "SELECT subscriber_id, window_id, kind, channel FROM notify_log")
    sent = {(r["subscriber_id"], r["window_id"], r["kind"], r["channel"]) for r in sent_rows}

    out: list[Pending] = []
    for row in rows:
        window = dict(row)
        channel = row["channel"] or "email"
        if not row["email"]:
            continue

        kinds = _candidate_kinds(window, moment)
        if not kinds:
            continue

        action = row["feedback_action"]
        blocked = _FEEDBACK_BLOCK.get(action, set())
        usable = []
        for kind in kinds:
            if kind in blocked:
                continue
            if (row["subscriber_id"], row["window_id"], kind, channel) in sent:
                continue
            usable.append(kind)

        if not usable:
            continue

        kind = usable[0]  # 已按紧急度排序
        end_text = window["pay_end"] if kind == "pay_closing" else window["sign_end"]
        out.append(
            Pending(
                subscriber_id=row["subscriber_id"],
                email=row["email"],
                channel=channel,
                access_token=row["access_token"],
                unsubscribe_token=row["unsubscribe_token"],
                window_id=row["window_id"],
                region_code=row["region_code"],
                region_name=row["region_name"],
                batch=row["batch"],
                kind=kind,
                days_left=_days_left(end_text, moment),
                window=window,
            )
        )

    return out


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------

def _fmt(value: str | None) -> str | None:
    dt = parse_dt(value)
    if not dt:
        return None
    # 注意不要用 %-m / %-d：那是 glibc 的扩展，Windows 上会抛 ValueError
    date = f"{dt.year}年{dt.month}月{dt.day}日"
    if dt.hour == 0 and dt.minute == 0:
        return date
    return f"{date} {dt.hour:02d}:{dt.minute:02d}"


def dispatch_pending(conn, pending: list[Pending]) -> dict[str, int]:
    stats = {"sent": 0, "failed": 0, "skipped": 0}
    base = settings.site_host

    for item in pending:
        try:
            send_notice_email(
                item.email or "",
                kind=item.kind,
                region_name=item.region_name,
                batch=item.batch,
                sign_start=_fmt(item.window.get("sign_start")),
                sign_end=_fmt(item.window.get("sign_end")),
                pay_start=_fmt(item.window.get("pay_start")),
                pay_end=_fmt(item.window.get("pay_end")),
                days_left=item.days_left,
                official_home=scraper.OFFICIAL_HOME,
                entry_url=item.window.get("entry_url"),
                registered_url=f"{base}/f/{item.access_token}/{item.window_id}/registered",
                paid_url=f"{base}/f/{item.access_token}/{item.window_id}/paid",
                skip_url=f"{base}/f/{item.access_token}/{item.window_id}/skip",
                unsubscribe_url=f"{base}/u/{item.unsubscribe_token}",
                manage_url=f"{base}/m/{item.access_token}",
            )

            conn.execute(
                """INSERT OR IGNORE INTO notify_log
                   (subscriber_id, window_id, kind, channel, sent_at, status)
                   VALUES (?, ?, ?, ?, ?, 'sent')""",
                (item.subscriber_id, item.window_id, item.kind, item.channel, iso()),
            )
            stats["sent"] += 1
        except NotifyError as exc:
            log.error("发送失败 subscriber=%s kind=%s：%s", item.subscriber_id, item.kind, exc)
            conn.execute(
                """INSERT OR IGNORE INTO notify_log
                   (subscriber_id, window_id, kind, channel, sent_at, status, error)
                   VALUES (?, ?, ?, ?, ?, 'failed', ?)""",
                (item.subscriber_id, item.window_id, item.kind, item.channel, iso(), str(exc)),
            )
            stats["failed"] += 1

    conn.commit()
    return stats


def run_notify_job(moment: datetime | None = None) -> dict[str, int]:
    conn = get_db()
    pending = collect_pending(conn, moment)
    if not pending:
        log.info("本轮没有需要发送的提醒")
        return {"sent": 0, "failed": 0, "pending": 0}
    log.info("本轮待发 %d 条提醒", len(pending))
    stats = dispatch_pending(conn, pending)
    stats["pending"] = len(pending)
    return stats


def run_all() -> dict:
    scrape_result = run_scrape_job()
    notify_stats = run_notify_job()
    return {"scrape": scrape_result, "notify": notify_stats}


# ---------------------------------------------------------------------------
# 后台线程
# ---------------------------------------------------------------------------

class DailyScheduler(threading.Thread):
    """极简的定时器。

    刻意不引 APScheduler：这个项目一天只需要跑两次，用 60 行的线程
    比多一个依赖更好维护，自部署者也更容易看懂它在干什么。
    """

    daemon = True

    def __init__(self, app, times: tuple[str, ...]):
        super().__init__(name="ruankao-scheduler")
        self.app = app
        self.times = set(times)
        self._stop_event = threading.Event()
        self._last_day: str | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("调度器已启动，每日 %s 执行（容器时区需为 Asia/Shanghai）", "、".join(sorted(self.times)))
        while not self._stop_event.wait(30):
            try:
                stamp = now()
                if stamp.strftime("%H:%M") in self.times and self._last_day != today():
                    self._last_day = today()
                    with self.app.app_context():
                        result = run_all()
                        log.info("定时任务完成：%s", result)
            except Exception:  # 任何异常都不能让线程死掉
                log.exception("定时任务执行出错，将在下个周期重试")

    def trigger_now(self) -> None:
        """后台手动触发（在请求线程里同步执行）。"""
        run_all()
