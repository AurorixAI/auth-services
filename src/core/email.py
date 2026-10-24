"""
ALGO V1 — Email Utility

Sends transactional emails via SMTP.
Falls back to logging the email body when SMTP is not configured (dev mode).

Env vars:
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
    ADMIN_NOTIFICATION_EMAIL, FRONTEND_URL
"""

from __future__ import annotations

import html
import logging
import os
import re
import smtplib
from urllib.parse import quote
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

SMTP_HOST: str = os.getenv("SMTP_HOST", "")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str = os.getenv("SMTP_USER", "")
SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM: str = os.getenv("SMTP_FROM", "noreply@algo.kz")
ADMIN_NOTIFICATION_EMAIL: str = os.getenv("ADMIN_NOTIFICATION_EMAIL", "")
FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")


def _smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


def _esc(value: object) -> str:
    """HTML-escape a value that came from a user (org name, owner name, e-mail) before it goes into a mail body (LD-19)."""
    return html.escape(str(value), quote=True)


def _header(value: str) -> str:
    """A header value on one line: CR/LF from a user-supplied name cannot start a new header."""
    return re.sub(r"[\r\n]+", " ", value).strip()


def _dev_env() -> bool:
    return (os.getenv("APP_ENV") or "").strip().lower() in ("", "development", "testing", "dev", "test")


def _send_raw(to: str, subject: str, html_body: str) -> bool:
    """Send an email via SMTP.  Returns True on success."""
    to, subject = _header(to), _header(subject)
    if not _smtp_configured():
        # The body carries activation links: it is logged only in development / tests, never in a deployed environment.
        logger.info(
            "SMTP not configured — email NOT sent.\n  To: %s\n  Subject: %s%s",
            to,
            subject,
            f"\n  Body:\n{html_body}" if _dev_env() else "",
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [to], msg.as_string())
        logger.info("Email sent to %s — %s", to, subject)
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to)
        return False


# ── Public helpers ───────────────────────────────────────────────────────────


def send_admin_new_application(
    org_name: str,
    org_id: str,
    owner_email: str,
    owner_name: str,
) -> bool:
    """Notify platform admin that a new organization has registered."""
    to = ADMIN_NOTIFICATION_EMAIL
    if not to:
        logger.warning(
            "ADMIN_NOTIFICATION_EMAIL not set — skipping admin notification "
            "for org '%s'",
            org_name,
        )
        return False

    subject = f"[ALGO] Новая заявка: {org_name}"
    html_body = f"""\
<html><body style="font-family:sans-serif;color:#1e293b">
<h2 style="color:#3b82f6">Новая заявка на регистрацию</h2>
<table style="border-collapse:collapse">
  <tr><td style="padding:4px 12px;font-weight:600">Организация:</td><td>{_esc(org_name)}</td></tr>
  <tr><td style="padding:4px 12px;font-weight:600">ID:</td><td><code>{_esc(org_id)}</code></td></tr>
  <tr><td style="padding:4px 12px;font-weight:600">Владелец:</td><td>{_esc(owner_name)} ({_esc(owner_email)})</td></tr>
</table>
<p>Для подтверждения используйте панель администратора или API:</p>
<pre>POST /api/v1/auth/admin/approve/{_esc(org_id)}</pre>
</body></html>"""
    return _send_raw(to, subject, html_body)


def send_activation_email(
    to: str,
    owner_name: str,
    org_name: str,
    activation_token: str,
) -> bool:
    """Send the activation link to the organization owner after admin approval."""
    activation_url = f"{FRONTEND_URL}/activate?token={quote(str(activation_token), safe='')}"
    subject = f"[ALGO] Ваша организация «{org_name}» одобрена!"
    html_body = f"""\
<html><body style="font-family:sans-serif;color:#1e293b">
<h2 style="color:#16a34a">Заявка одобрена ✓</h2>
<p>Здравствуйте, {_esc(owner_name)}!</p>
<p>Ваша организация <strong>«{_esc(org_name)}»</strong> была одобрена администратором ALGO.</p>
<p>Нажмите кнопку ниже, чтобы активировать ваш аккаунт:</p>
<p style="text-align:center;margin:24px 0">
  <a href="{_esc(activation_url)}"
     style="display:inline-block;padding:12px 32px;background:#3b82f6;
            color:#fff;text-decoration:none;border-radius:8px;font-weight:600">
    Активировать аккаунт
  </a>
</p>
<p style="font-size:13px;color:#64748b">
  Или перейдите по ссылке:
  <a href="{_esc(activation_url)}">{_esc(activation_url)}</a>
</p>
<p style="font-size:13px;color:#94a3b8">Ссылка действительна 7 дней.</p>
</body></html>"""
    return _send_raw(to, subject, html_body)
