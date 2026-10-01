"""配置。所有可调项来自环境变量 / .env，代码里不硬编码任何凭证。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _times(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """解析 "08:10,20:10" 形式的每日抓取时刻。"""
    raw = _str(name)
    if not raw:
        return default
    parsed: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if len(part) == 5 and part[2] == ":" and part.replace(":", "").isdigit():
            parsed.append(part)
    return tuple(parsed) or default


@dataclass(frozen=True)
class MailSettings:
    enabled: bool = False
    host: str = ""
    port: int = 465
    secure: bool = True
    user: str = ""
    password: str = ""
    sender: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.host and self.user and self.sender)

    def describe(self) -> str:
        if not self.enabled:
            return "未启用"
        if not self.ready:
            return "已启用但配置不完整"
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class Settings:
    site_name: str = "软考报名提醒"
    site_url: str = "http://localhost:3000"
    site_operator: str = ""
    site_contact_email: str = ""
    port: int = 3000
    debug: bool = False
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "ruankao.db")
    secret_key: str = ""
    admin_token: str = "change-me-please"
    alert_email: str = ""
    scrape_times: tuple[str, ...] = ("08:10", "20:10")
    scheduler_enabled: bool = True
    dry_run: bool = False
    mail: MailSettings = field(default_factory=MailSettings)

    @property
    def site_host(self) -> str:
        return self.site_url.rstrip("/")

    @property
    def admin_ready(self) -> bool:
        return self.admin_token not in {"", "change-me-please"}

    def url_for(self, path: str) -> str:
        return f"{self.site_host}/{path.lstrip('/')}"

    def warnings(self) -> list[str]:
        """启动时打印的配置提醒。不阻断运行，但要把风险说清楚。"""
        out: list[str] = []
        if not self.admin_ready:
            out.append("ADMIN_TOKEN 仍是默认值，后台处于未设防状态，请立刻修改")
        if not self.mail.ready:
            out.append("邮件通道不可用，抓取结果将只写日志、无法送达用户")
        if not self.secret_key:
            out.append("未设置 SECRET_KEY，已临时随机生成，重启后登录态会失效")
        return out


def load_settings() -> Settings:
    db_raw = _str("DB_PATH", "data/ruankao.db")
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    return Settings(
        site_name=_str("SITE_NAME", "软考报名提醒"),
        site_url=_str("SITE_URL", "http://localhost:3000"),
        site_operator=_str("SITE_OPERATOR"),
        site_contact_email=_str("SITE_CONTACT_EMAIL"),
        port=int(_str("PORT", "3000") or 3000),
        debug=_bool("FLASK_DEBUG"),
        db_path=db_path,
        secret_key=_str("SECRET_KEY"),
        admin_token=_str("ADMIN_TOKEN", "change-me-please"),
        alert_email=_str("ALERT_EMAIL"),
        scrape_times=_times("SCRAPE_TIMES", ("08:10", "20:10")),
        scheduler_enabled=_bool("SCHEDULER_ENABLED", True),
        dry_run=_bool("DRY_RUN"),
        mail=MailSettings(
            enabled=_bool("MAIL_ENABLED"),
            host=_str("MAIL_HOST"),
            port=int(_str("MAIL_PORT", "465") or 465),
            secure=_bool("MAIL_SECURE", True),
            user=_str("MAIL_USER"),
            password=_str("MAIL_PASS"),
            sender=_str("MAIL_FROM"),
        ),
    )


settings = load_settings()
