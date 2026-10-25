"""Mail settings, read from the environment at CALL time (a test or a script may change them)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MailConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    from_address: str
    from_name: str
    reply_to: str
    admin_email: str
    console_url: str
    frontend_url: str
    site_url: str

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)


def _url(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip().rstrip("/")


def localhost_urls(cfg: "MailConfig | None" = None) -> list:
    """Names of the settings whose links would point at a developer's machine (a letter with such a link is useless to the person)."""
    cfg = cfg or load()
    bad = []
    for name, value in (("FRONTEND_URL", cfg.frontend_url), ("CONSOLE_URL", cfg.console_url)):
        host = (value.split("://", 1)[-1].split("/", 1)[0].split(":")[0]).lower()
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost") or host.endswith(".local"):
            bad.append(name)
    return bad


def load() -> MailConfig:
    return MailConfig(
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587") or 587),
        smtp_user=os.getenv("SMTP_USER", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        from_address=(os.getenv("SMTP_FROM") or "noreply@algolab.me").strip(),
        from_name=(os.getenv("SMTP_FROM_NAME") or "ALGO").strip(),
        reply_to=os.getenv("MAIL_REPLY_TO", "").strip(),
        admin_email=os.getenv("ADMIN_NOTIFICATION_EMAIL", "").strip(),
        console_url=_url("CONSOLE_URL", "https://console.algolab.me"),
        frontend_url=_url("FRONTEND_URL", "http://localhost:3000"),
        site_url=_url("SITE_URL", "https://algolab.me"),
    )
