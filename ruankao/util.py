"""零散工具：时间、token、脱敏。

关于时区：软考是中国大陆考试，全年只用 UTC+8 且无夏令时，
所以这里用固定偏移而不是 zoneinfo——顺带免掉了 Windows 上
必须有 tzdata 包才能用 zoneinfo 的坑。
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")


def now() -> datetime:
    return datetime.now(CN_TZ)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return now().strftime("%Y-%m-%d")


def parse_dt(text: str | None) -> datetime | None:
    """解析官方页面上的「2026-09-11 00:00」这类时间串。"""
    if not text:
        return None
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    return None


def new_token(nbytes: int = 16) -> str:
    """URL 安全的随机 token。默认 16 字节 -> 22 字符。"""
    return secrets.token_urlsafe(nbytes)


def email_valid(email: str) -> bool:
    email = (email or "").strip()
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def mask_email(email: str) -> str:
    """a***b@example.com，用于页面回显而非完整暴露。"""
    email = email or ""
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        shown = local[:1] + "*"
    else:
        shown = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{shown}@{domain}"


def hash_ip(ip: str) -> str:
    """只存散列，不存原始 IP——收集个人信息够用即可，不越界。"""
    if not ip:
        return ""
    return hashlib.sha256(("ruankao-notice:" + ip).encode()).hexdigest()[:32]


def human_range(start: str | None, end: str | None) -> str:
    """把起止时间渲染成「09-11 ~ 09-17」这种人看的短格式。"""
    def short(value: str | None) -> str:
        dt = parse_dt(value)
        if not dt:
            return ""
        return dt.strftime("%m-%d")

    a, b = short(start), short(end)
    if a and b:
        if a == b:
            return a
        return f"{a} ~ {b}"
    return a or b or "—"
