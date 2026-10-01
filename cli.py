"""命令行工具。

    python cli.py doctor          排查配置与连通性
    python cli.py scrape          只跑抓取
    python cli.py notify          只跑提醒判定与发送
    python cli.py run             抓取 + 提醒
    python cli.py windows         打印当前批次的地区时间表
    python cli.py demo            造一个已生效的订阅者（本地调试用）
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata

from ruankao import create_app
from ruankao.config import settings
from ruankao.db import get_db, query, query_one
from ruankao.notify import channel_status
from ruankao.regions import BY_CODE
from ruankao.util import iso, new_token, normalize_email


def _app_ctx():
    app = create_app()
    return app, app.app_context()


def _width(text: str) -> int:
    """终端显示宽度。中文是全角，占两列，直接 len() 会让表格错位。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - _width(text))


# ---------------------------------------------------------------------------

def cmd_doctor(_args) -> int:
    app, ctx = _app_ctx()
    with ctx:
        print("=" * 62)
        print(f"站点名称      {settings.site_name}")
        print(f"站点地址      {settings.site_host}")
        print(f"数据库        {settings.db_path}")
        print(f"定时抓取      {'、'.join(settings.scrape_times)}  （调度器 "
              f"{'开启' if settings.scheduler_enabled else '关闭'}）")
        print(f"干跑模式      {'是（不会真发邮件）' if settings.dry_run else '否'}")
        print("-" * 62)
        for ch in channel_status():
            print(f"通道 {ch['label']:<4}    {ch['detail']}")
        print("-" * 62)

        warnings = settings.warnings()
        if warnings:
            print("配置提醒：")
            for w in warnings:
                print(f"  ! {w}")
        else:
            print("配置提醒：无")

        print("-" * 62)
        print("官方页面连通性测试 …")
        from ruankao import scraper

        try:
            result = scraper.scrape()
            print(f"  连接成功：批次 {result.batch}，解析出 {len(result.windows)} 个机构")
            if result.unknown_names:
                print(f"  未识别机构：{result.unknown_names}")
            for w in result.warnings:
                print(f"  ! {w}")
        except Exception as exc:
            print(f"  失败：{exc}")
            print("  在解决之前，用户不会收到任何新提醒。")

        print("-" * 62)
        conn = get_db()
        row = query_one(conn, "SELECT COUNT(*) AS c FROM exam_windows")
        subs = query_one(conn, "SELECT COUNT(*) AS c FROM subscribers")
        runs = query_one(conn, "SELECT COUNT(*) AS c FROM scrape_runs")
        print(f"数据库        窗口 {row['c']} 条 / 订阅者 {subs['c']} 人 / 抓取记录 {runs['c']} 次")
        print("=" * 62)
        print(f"后台地址      {settings.site_host}/admin")
    return 0


def cmd_scrape(_args) -> int:
    app, ctx = _app_ctx()
    with ctx:
        from ruankao.scheduler import run_scrape_job

        print(json.dumps(run_scrape_job(), ensure_ascii=False, indent=2))
    return 0


def cmd_notify(_args) -> int:
    app, ctx = _app_ctx()
    with ctx:
        from ruankao.scheduler import run_notify_job

        print(json.dumps(run_notify_job(), ensure_ascii=False, indent=2))
    return 0


def cmd_run(_args) -> int:
    app, ctx = _app_ctx()
    with ctx:
        from ruankao.scheduler import run_all

        print(json.dumps(run_all(), ensure_ascii=False, indent=2))
    return 0


def cmd_windows(_args) -> int:
    app, ctx = _app_ctx()
    with ctx:
        from ruankao import services

        conn = get_db()
        windows, batch = services.latest_windows(conn)
        if not windows:
            print("数据库里还没有窗口数据，先跑一次 python cli.py scrape")
            return 1
        print(f"批次：{batch}    共 {len(windows)} 个机构")
        print("-" * 100)
        print(_pad("地区", 12) + _pad("报名", 38) + _pad("缴费", 38) + "状态")
        print("-" * 100)
        for w in windows:
            sign = f"{w['sign_start'] or '—'} ~ {w['sign_end'] or '—'}"
            pay = f"{w['pay_start'] or '—'} ~ {w['pay_end'] or '—'}"
            state = services.STATE_LABEL[services.window_state(w)]
            print(_pad(w["region_name"], 12) + _pad(sign, 38) + _pad(pay, 38) + state)
    return 0


def cmd_demo(args) -> int:
    """直接造一个已生效的订阅者，跳过邮件确认，方便本地看邮件长什么样。"""
    app, ctx = _app_ctx()
    with ctx:
        conn = get_db()
        email = normalize_email(args.email)
        codes = [c.strip() for c in (args.regions or "").split(",") if c.strip()]
        codes = [c for c in codes if c in BY_CODE]
        if not codes:
            print("请用 --regions 指定至少一个地区代码，例如 --regions guangdong,zhejiang")
            print("可用代码：", "、".join(sorted(BY_CODE)))
            return 1

        row = query_one(conn, "SELECT * FROM subscribers WHERE email = ?", (email,))
        if row is None:
            cur = conn.execute(
                """INSERT INTO subscribers
                   (email, channel, plan, status, access_token, unsubscribe_token, created_at, verified_at)
                   VALUES (?, 'email', 'free', 'active', ?, ?, ?, ?)""",
                (email, new_token(20), new_token(20), iso(), iso()),
            )
            subscriber_id = cur.lastrowid
            access = query_one(
                conn, "SELECT access_token FROM subscribers WHERE id = ?", (subscriber_id,)
            )["access_token"]
        else:
            subscriber_id = row["id"]
            access = row["access_token"]
            conn.execute("UPDATE subscribers SET status = 'active' WHERE id = ?", (subscriber_id,))

        for code in codes:
            conn.execute(
                """INSERT OR IGNORE INTO subscriptions (subscriber_id, region_code, created_at)
                   VALUES (?, ?, ?)""",
                (subscriber_id, code, iso()),
            )
        conn.commit()

        print(f"已创建订阅者：{email}")
        print(f"订阅地区：{'、'.join(BY_CODE[c].name for c in codes)}")
        print(f"管理页：  {settings.site_host}/m/{access}")
        print()
        print("提示：用 python cli.py notify 触发一轮提醒判定，")
        print("      配合 .env 里的 DRY_RUN=true 可以在不发真邮件的情况下看效果。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli", description="软考报名提醒 — 命令行工具")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="排查配置与连通性").set_defaults(func=cmd_doctor)
    sub.add_parser("scrape", help="只跑抓取").set_defaults(func=cmd_scrape)
    sub.add_parser("notify", help="只跑提醒判定与发送").set_defaults(func=cmd_notify)
    sub.add_parser("run", help="抓取 + 提醒").set_defaults(func=cmd_run)
    sub.add_parser("windows", help="打印当前批次的地区时间表").set_defaults(func=cmd_windows)

    demo = sub.add_parser("demo", help="造一个已生效的订阅者（本地调试用）")
    demo.add_argument("--email", default="demo@example.com")
    demo.add_argument("--regions", default="guangdong", help="逗号分隔的地区代码")
    demo.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
