"""通知适配器与消息渲染。

当前只启用邮件一个通道。这是刻意的选择，不是半成品：

国内短信签名报备要求企业或个体工商户主体，个人主体拿不到发送权限
（腾讯云自 2025-09-18 起不再受理个人自用资质；阿里云明确「个人认证自用
资质无法通过签名实名制报备」）。既然发不出去，就不该在代码里留一条
永远走不通的路径——那只会让人以为「配一下就能用」。
等真的具备主体资质，再加回来。

关于邮件送达率
--------------
除了内容本身，收件方还会看发件域名的 SPF / DKIM / DMARC 以及是否有
``List-Unsubscribe`` 头。这里两者都做了，但 SPF/DKIM 属于 DNS 配置，
需要部署者自己在域名侧完成，README 里有说明。
"""

from __future__ import annotations

import html as html_mod
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import settings

log = logging.getLogger("ruankao.notify")

EMAIL_DIR = Path(__file__).resolve().parent / "emails"

_env = Environment(
    loader=FileSystemLoader(str(EMAIL_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


class NotifyError(RuntimeError):
    """发送失败。调用方负责记录并决定是否重试。"""


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def render(template: str, **ctx: Any) -> str:
    return _env.get_template(template).render(**ctx)


def _base_context(subject: str, **extra: Any) -> dict[str, Any]:
    return {
        "subject": subject,
        "site_name": settings.site_name,
        "operator": settings.site_operator,
        "contact_email": settings.site_contact_email,
        "footer_note": "本邮件由软考报名提醒服务自动发出，请勿直接回复。",
        "disclaimer": (
            "提醒数据抓取自官方报名平台，仅供参考，可能存在延迟或误差。"
            "报名与缴费请务必以官方页面为准。"
        ),
        **extra,
    }


def _html_to_text(markup: str) -> str:
    """给纯文本客户端用的降级版本，不求好看，只求能读懂。"""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", markup)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------

def _mail_ready() -> bool:
    return settings.mail.ready


def send_mail(
    to_email: str,
    subject: str,
    html: str,
    *,
    unsubscribe_url: str | None = None,
) -> None:
    cfg = settings.mail

    if settings.dry_run:
        log.info("[DRY_RUN] 本应发往 <%s> 的邮件：%s", to_email, subject)
        return

    if not cfg.ready:
        raise NotifyError(
            "邮件通道未就绪。请检查 .env 中的 MAIL_ENABLED / MAIL_HOST / MAIL_USER / MAIL_PASS / MAIL_FROM。"
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = to_email
    msg["Date"] = formatdate(localtime=True)

    domain = ""
    if "@" in cfg.sender:
        domain = cfg.sender.rsplit("@", 1)[1].strip(">").strip()
    msg["Message-ID"] = make_msgid(domain=domain or None)
    msg["Auto-Submitted"] = "auto-generated"

    if unsubscribe_url:
        # 收件方会据此判断这是「可退订的通知邮件」而不是垃圾邮件
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    msg.set_content(_html_to_text(html))
    msg.add_alternative(html, subtype="html")

    try:
        context = ssl.create_default_context()
        if cfg.secure:
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=25, context=context) as smtp:
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=25) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise NotifyError(
            "SMTP 认证失败。多数邮箱（含 QQ / 腾讯企业邮）要求使用「授权码」"
            "而不是登录密码，请确认 MAIL_PASS 填的是授权码。"
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise NotifyError(f"SMTP 发送失败：{exc}") from exc


def send_magic_email(
    to_email: str,
    *,
    magic_url: str,
    purpose: str,
    ttl_hours: int,
    region_names: str | None = None,
    batch: str | None = None,
    unsubscribe_url: str | None = None,
) -> None:
    """免密码验证邮件。

    订阅确认和登录共用一套机制，只有措辞不同——都是「点一下这个链接」，
    没必要写两套逻辑、两套过期规则、两套踩过的坑。
    """
    if purpose == "login":
        subject = f"进入你的订阅管理页 · {settings.site_name}"
        button_label = "进入管理页"
    else:
        subject = f"确认订阅：{settings.site_name}"
        button_label = "确认订阅"

    body = render(
        "magic.html",
        **_base_context(
            subject,
            magic_url=magic_url,
            purpose=purpose,
            button_label=button_label,
            ttl_hours=ttl_hours,
            region_names=region_names,
            batch=batch,
            unsubscribe_url=unsubscribe_url,
        ),
    )
    send_mail(to_email, subject, body, unsubscribe_url=unsubscribe_url)


_KIND_SUBJECT = {
    "published": "{region}软考报名时间已公布",
    "open": "{region}软考报名今天开始",
    "closing": "{region}软考报名还剩 {days} 天截止",
    "pay_closing": "{region}软考缴费还剩 {days} 天截止",
}


def kind_subject(kind: str, region_name: str, days_left: int | None = None) -> str:
    template = _KIND_SUBJECT.get(kind, "{region}软考报名提醒")
    return template.format(region=region_name, days=days_left if days_left is not None else "")


def send_notice_email(
    to_email: str,
    *,
    kind: str,
    region_name: str,
    batch: str,
    sign_start: str | None,
    sign_end: str | None,
    pay_start: str | None,
    pay_end: str | None,
    days_left: int | None,
    official_home: str,
    entry_url: str | None,
    registered_url: str,
    paid_url: str,
    skip_url: str,
    unsubscribe_url: str,
    manage_url: str,
) -> None:
    subject = kind_subject(kind, region_name, days_left)
    body = render(
        "notify.html",
        **_base_context(
            subject,
            kind=kind,
            region_name=region_name,
            batch=batch,
            sign_start=sign_start,
            sign_end=sign_end,
            pay_start=pay_start,
            pay_end=pay_end,
            days_left=days_left,
            official_home=official_home,
            entry_url=entry_url,
            registered_url=registered_url,
            paid_url=paid_url,
            skip_url=skip_url,
            show_feedback=kind in {"closing", "pay_closing"},
            unsubscribe_url=unsubscribe_url,
            manage_url=manage_url,
        ),
    )
    send_mail(to_email, subject, body, unsubscribe_url=unsubscribe_url)


def send_admin_alert(subject: str, lines: list[str]) -> None:
    """给运维自己发告警。抓取失败时用，避免系统静默失效。"""
    if not settings.alert_email:
        log.warning("未配置 ALERT_EMAIL，告警只写日志：%s / %s", subject, "; ".join(lines))
        return

    text = "\n".join(lines)
    body = render(
        "alert.html",
        **_base_context(subject, alert_subject=subject, alert_lines=lines, alert_text=text),
    ) if (EMAIL_DIR / "alert.html").exists() else (
        f"<p>{html_mod.escape(subject)}</p><pre>{html_mod.escape(text)}</pre>"
    )

    try:
        send_mail(settings.alert_email, f"[软考提醒] {subject}", body)
    except NotifyError as exc:
        log.error("管理员告警也发不出去：%s", exc)


# ---------------------------------------------------------------------------
# 通道状态（后台展示用）
# ---------------------------------------------------------------------------

def channel_status() -> list[dict[str, str]]:
    mail = settings.mail
    return [
        {
            "channel": "email",
            "label": "邮件",
            "state": "ready" if mail.ready else ("disabled" if not mail.enabled else "incomplete"),
            "detail": mail.describe(),
        },
    ]
