"""
ALGO V1 — Email Utility

Sends transactional emails via SMTP.
Falls back to logging the email body when SMTP is not configured (dev mode).

Env vars:
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM,
    ADMIN_NOTIFICATION_EMAIL, FRONTEND_URL
"""

from __future__ import annotations

import logging
import os
import smtplib
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


def _send_raw(to: str, subject: str, html_body: str) -> bool:
    """Send an email via SMTP.  Returns True on success."""
    if not _smtp_configured():
        logger.info(
            "SMTP not configured — email NOT sent.\n"
            "  To: %s\n  Subject: %s\n  Body:\n%s",
            to,
            subject,
            html_body,
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
    html = f"""\
<html><body style="font-family:sans-serif;color:#1e293b">
<h2 style="color:#3b82f6">Новая заявка на регистрацию</h2>
<table style="border-collapse:collapse">
  <tr><td style="padding:4px 12px;font-weight:600">Организация:</td><td>{org_name}</td></tr>
  <tr><td style="padding:4px 12px;font-weight:600">ID:</td><td><code>{org_id}</code></td></tr>
  <tr><td style="padding:4px 12px;font-weight:600">Владелец:</td><td>{owner_name} ({owner_email})</td></tr>
</table>
<p>Для подтверждения используйте панель администратора или API:</p>
<pre>POST /api/v1/auth/admin/approve/{org_id}</pre>
</body></html>"""
    return _send_raw(to, subject, html)


def send_activation_email(
    to: str,
    owner_name: str,
    org_name: str,
    activation_token: str,
) -> bool:
    """Send the activation link to the organization owner after admin approval."""
    activation_url = f"{FRONTEND_URL}/activate?token={activation_token}"
    subject = f"[ALGO] Ваша организация «{org_name}» одобрена!"
    html = f"""\
<html><body style="font-family:sans-serif;color:#1e293b">
<h2 style="color:#16a34a">Заявка одобрена ✓</h2>
<p>Здравствуйте, {owner_name}!</p>
<p>Ваша организация <strong>«{org_name}»</strong> была одобрена администратором ALGO.</p>
<p>Нажмите кнопку ниже, чтобы активировать ваш аккаунт:</p>
<p style="text-align:center;margin:24px 0">
  <a href="{activation_url}"
     style="display:inline-block;padding:12px 32px;background:#3b82f6;
            color:#fff;text-decoration:none;border-radius:8px;font-weight:600">
    Активировать аккаунт
  </a>
</p>
<p style="font-size:13px;color:#64748b">
  Или перейдите по ссылке:
  <a href="{activation_url}">{activation_url}</a>
</p>
<p style="font-size:13px;color:#94a3b8">Ссылка действительна 7 дней.</p>
</body></html>"""
    return _send_raw(to, subject, html)
