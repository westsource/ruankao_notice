"""软考报名提醒 — 应用工厂。"""

from __future__ import annotations

import logging
import secrets

from flask import Flask, render_template

from . import db
from .config import BASE_DIR, settings
from .regions import REGIONS, grouped
from .util import human_range, parse_dt

__version__ = "0.1.0"

log = logging.getLogger("ruankao")


def create_app() -> Flask:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "ruankao" / "templates"),
        static_folder=str(BASE_DIR / "ruankao" / "static"),
        static_url_path="/static",
    )

    app.config.update(
        SECRET_KEY=settings.secret_key or secrets.token_urlsafe(32),
        DB_PATH=settings.db_path,
        ADMIN_TOKEN=settings.admin_token,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.site_url.startswith("https://"),
        # 改一行模板刷新就能看到效果，不用重启。这个站点的模板渲染开销可以忽略，
        # 而「改了没生效」是最容易浪费时间的坑。
        TEMPLATES_AUTO_RELOAD=True,
        MAX_CONTENT_LENGTH=256 * 1024,
    )

    db.init_app(app)

    with app.app_context():
        conn = db.get_db()
        db.init_schema(conn)
        db.sync_regions(conn, REGIONS)

    _register_jinja(app)
    _register_blueprints(app)
    _register_errorhandlers(app)

    for warning in settings.warnings():
        log.warning("配置提醒：%s", warning)

    return app


def _register_jinja(app: Flask) -> None:
    @app.template_filter("shortrange")
    def _shortrange(value: str | None) -> str:
        if not value:
            return "—"
        start, _, end = value.partition("~")
        return human_range(start.strip(), end.strip())

    @app.template_filter("dt")
    def _dt(value: str | None) -> str:
        moment = parse_dt(value)
        if not moment:
            return "—"
        return f"{moment.year}-{moment.month:02d}-{moment.day:02d}"

    @app.template_filter("dttime")
    def _dttime(value: str | None) -> str:
        moment = parse_dt(value)
        if not moment:
            return "—"
        return f"{moment.month}月{moment.day}日 {moment.hour:02d}:{moment.minute:02d}"

    @app.context_processor
    def _inject_globals() -> dict:
        return {
            "settings": settings,
            "region_groups": grouped(),
            "all_regions": REGIONS,
            "version": __version__,
            "site_last_update": _last_update(),
        }

    @app.route("/robots.txt")
    def robots():
        # 订阅页与时间表页希望被搜索引擎收录，管理页绝对不能
        body = (
            "User-agent: *\n"
            "Allow: /$\n"
            "Allow: /schedule\n"
            "Disallow: /m/\n"
            "Disallow: /admin\n"
            "Disallow: /verify/\n"
            "Disallow: /u/\n"
            "Disallow: /f/\n"
        )
        return app.response_class(body, mimetype="text/plain")


def _register_blueprints(app: Flask) -> None:
    from .views import admin, manage, public

    app.register_blueprint(public.bp)
    app.register_blueprint(manage.bp)
    app.register_blueprint(admin.bp)


def _last_update() -> str:
    """页脚展示「数据最近核对于 …」，让用户自己判断信息的新鲜度。

    任何异常都必须吞掉——这只是个装饰性信息，不能因为它让整页挂掉。
    """
    try:
        from .db import get_db, query_one

        row = query_one(
            get_db(),
            "SELECT finished_at FROM scrape_runs WHERE ok = 1 ORDER BY id DESC LIMIT 1",
        )
        if not row or not row["finished_at"]:
            return ""
        moment = parse_dt(row["finished_at"])
        if not moment:
            return ""
        return f"{moment.month} 月 {moment.day} 日 {moment.hour:02d}:{moment.minute:02d}"
    except Exception:
        return ""


def _register_errorhandlers(app: Flask) -> None:
    @app.errorhandler(404)
    def _not_found(_exc):
        return render_template("message.html", title="页面不存在", body="这个地址没有对应的内容。"), 404

    @app.errorhandler(429)
    def _too_many(_exc):
        return render_template(
            "message.html", title="操作太频繁", body="请稍等几分钟再试。"
        ), 429

    @app.errorhandler(500)
    def _server_error(exc):
        log.exception("未处理的异常：%s", exc)
        return render_template(
            "message.html", title="服务器出了点问题", body="已经记录下来了，请稍后再试。"
        ), 500
