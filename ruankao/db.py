"""SQLite 访问层。

用标准库 sqlite3，不引 ORM：表结构简单、查询量小，裸 SQL 反而
更容易让自部署者看懂和改，也少一层依赖。

连接获取有两种方式：
- Web 请求内用 ``get_db()``，连接挂在 Flask 的 ``g`` 上，请求结束时自动关闭
- 命令行任务用 ``connect(path)``，自己负责关闭
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Sequence

from flask import Flask, current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS regions (
    code          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    official_name TEXT NOT NULL,
    grp           TEXT NOT NULL,
    sort_order    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS subscribers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT NOT NULL UNIQUE,
    channel           TEXT NOT NULL DEFAULT 'email',
    plan              TEXT NOT NULL DEFAULT 'free',
    paid_until        TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    login_token       TEXT,
    login_token_expires_at TEXT,
    pending_regions   TEXT,
    access_token      TEXT NOT NULL UNIQUE,
    unsubscribe_token TEXT NOT NULL UNIQUE,
    created_at        TEXT NOT NULL,
    verified_at       TEXT,
    ip_hash           TEXT
);
CREATE INDEX IF NOT EXISTS idx_subscribers_status ON subscribers(status);

CREATE TABLE IF NOT EXISTS subscriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    region_code   TEXT NOT NULL REFERENCES regions(code),
    created_at    TEXT NOT NULL,
    UNIQUE (subscriber_id, region_code)
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_region ON subscriptions(region_code);

CREATE TABLE IF NOT EXISTS exam_windows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_type     TEXT NOT NULL DEFAULT 'ruankao',
    batch         TEXT NOT NULL,
    region_code   TEXT NOT NULL REFERENCES regions(code),
    official_name TEXT NOT NULL,
    sign_start    TEXT,
    sign_end      TEXT,
    pay_start     TEXT,
    pay_end       TEXT,
    entry_url     TEXT,
    source_url    TEXT NOT NULL,
    scraped_at    TEXT NOT NULL,
    raw_hash      TEXT NOT NULL,
    UNIQUE (exam_type, batch, region_code)
);
CREATE INDEX IF NOT EXISTS idx_windows_batch ON exam_windows(exam_type, batch);

CREATE TABLE IF NOT EXISTS notify_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    window_id     INTEGER NOT NULL REFERENCES exam_windows(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    channel       TEXT NOT NULL,
    sent_at       TEXT NOT NULL,
    status        TEXT NOT NULL,
    error         TEXT,
    UNIQUE (subscriber_id, window_id, kind, channel)
);
CREATE INDEX IF NOT EXISTS idx_notify_window ON notify_log(window_id, kind);

CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    window_id     INTEGER NOT NULL REFERENCES exam_windows(id) ON DELETE CASCADE,
    action        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (subscriber_id, window_id)
);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    ok          INTEGER NOT NULL DEFAULT 0,
    batch       TEXT,
    rows_parsed INTEGER NOT NULL DEFAULT 0,
    changed     INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """建立连接。命令行任务用这个。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


# 新增列时在这里登记，老库升级时会自动补上。
# SQLite 的 CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
# 所以开源项目的升级路径必须靠这个显式迁移，否则老用户一升级就报错。
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "subscribers": {
        "pending_regions": "TEXT",
        "login_token": "TEXT",
        "login_token_expires_at": "TEXT",
    },
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.commit()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)


def sync_regions(conn: sqlite3.Connection, regions: Sequence[Any]) -> None:
    """把代码里的地区字典刷进数据库，供外键引用与后台展示。"""
    conn.executemany(
        """INSERT INTO regions (code, name, official_name, grp, sort_order)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(code) DO UPDATE SET
               name = excluded.name,
               official_name = excluded.official_name,
               grp = excluded.grp,
               sort_order = excluded.sort_order""",
        [(r.code, r.name, r.official, r.group, r.order) for r in regions],
    )
    conn.commit()


def get_db() -> sqlite3.Connection:
    """Web 请求内取连接（挂在 g 上，请求结束自动关闭）。"""
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def init_app(app: Flask) -> None:
    app.teardown_appcontext(close_db)
