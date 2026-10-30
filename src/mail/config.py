"""Mail settings, read from the environment at CALL time (a test or a script may change them)."""
from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_FRONTEND_URL = "http://localhost:3000"
DEFAULT_SITE_URL = "https://algolab.me"


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
    # K2-1 (G): who may get a letter. ``allowlist`` = addresses ("a@b.uz") and domains ("b.uz" / "@b.uz"), lower-case; ``restrict`` = this stack
    # is a stage: with an EMPTY list it sends nothing (safe default). A production stack has no list = no restriction.
    allowlist: tuple = ()
    restrict: bool = False
    # K2-2: where a person writes when a letter is not about him («если это были не вы»); the same mailbox the replies go to
    support_email: str = "support@algolab.me"

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    def allows(self, address: str) -> bool:
        """May a letter go to *address*? A non-empty ``allowlist`` always applies; an empty one means «anybody» on production and «nobody» on a stage."""
        if not self.allowlist:
            return not self.restrict
        _, _, domain = (address or "").strip().lower().rpartition("@")
        wanted = (address or "").strip().lower()
        for entry in self.allowlist:
            if entry.startswith("@"):
                if domain and domain == entry[1:]:
                    return True
            elif "@" in entry:
                if wanted == entry:
                    return True
            elif domain and domain == entry:
                return True
        return False


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


# Both real stacks run APP_ENV=staging (stage and prod alike), so the environment name cannot say "this is the stage": the stage compose sets
# MAIL_STAGE_MODE=true (an APP_ENV of stage / preview counts too).
STAGE_ENVS = ("stage", "preview")


def _allowlist(raw: str) -> tuple:
    return tuple(sorted({item.strip().lower() for item in (raw or "").replace(";", ",").split(",") if item.strip()}))


def _is_stage() -> bool:
    flag = os.getenv("MAIL_STAGE_MODE", "").strip().lower() in ("1", "true", "yes", "on")
    return flag or os.getenv("APP_ENV", "").strip().lower() in STAGE_ENVS


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
        frontend_url=_url("FRONTEND_URL", DEFAULT_FRONTEND_URL),
        site_url=_url("SITE_URL", DEFAULT_SITE_URL),
        allowlist=_allowlist(os.getenv("MAIL_ALLOWLIST", "")),
        restrict=_is_stage(),
        support_email=(os.getenv("MAIL_REPLY_TO") or "support@algolab.me").strip(),
    )
