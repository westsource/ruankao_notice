"""启动入口。

一个进程同时干两件事：对外提供 HTTP 服务，以及跑每日定时抓取。

为什么不拆成「Web 容器 + 任务容器」：这个站点一天只需要执行两次任务，
每次几秒钟。为它单独维护一个调度进程、一套部署编排、一份日志收集，
运维成本远大于收益。真要扩容时，把 SCHEDULER_ENABLED 设为 false，
再多起几个纯 Web 实例即可——那时候再拆也不迟。
"""

from __future__ import annotations

import logging
import os

from ruankao import create_app
from ruankao.config import settings
from ruankao.scheduler import DailyScheduler

app = create_app()
log = logging.getLogger("ruankao.run")


def _serve(flask_app, host: str, port: int) -> None:
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        log.warning(
            "未安装 waitress，回退到 Flask 内置服务器。仅供本机调试，请勿用于生产。"
        )
        flask_app.run(host=host, port=port, debug=settings.debug, use_reloader=False)
        return

    log.info("HTTP 服务已启动：http://%s:%s", host, port)
    waitress_serve(flask_app, host=host, port=port, threads=6, ident="ruankao-notice")


def main() -> None:
    scheduler = None
    if settings.scheduler_enabled:
        scheduler = DailyScheduler(app, settings.scrape_times)
        scheduler.start()
    else:
        log.info("SCHEDULER_ENABLED=false，本进程不执行定时任务")

    log.info("站点地址：%s", settings.site_host)
    log.info("数据库：%s", settings.db_path)

    try:
        _serve(app, os.getenv("HOST", "0.0.0.0"), settings.port)
    finally:
        if scheduler is not None:
            scheduler.stop()


if __name__ == "__main__":
    main()
