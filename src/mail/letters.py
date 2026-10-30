"""The letters of the platform, as functions the application calls. Each returns True when the letter was handed to SMTP.

``render_*`` builds the letter without sending it (tests, previews); ``send_*`` sends it. All links are built here from the
configuration: ``FRONTEND_URL`` (cabinets) and ``CONSOLE_URL`` (console).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from . import config as mail_config
from . import sender
from .render import Rendered, render

logger = logging.getLogger(__name__)


def set_password_url(token: str, cfg: mail_config.MailConfig | None = None) -> str:
    cfg = cfg or mail_config.load()
    return f"{cfg.frontend_url}/set-password/{quote(token, safe='')}"


def invite_url(code: str, cfg: mail_config.MailConfig | None = None) -> str:
    """The page where an invited employee sets the password: ``/invite/{code}`` of the cabinet. The code in it is the proof of the mailbox."""
    cfg = cfg or mail_config.load()
    return f"{cfg.frontend_url}/invite/{quote(code, safe='')}"


def console_request_url(request_id: str, cfg: mail_config.MailConfig | None = None) -> str:
    cfg = cfg or mail_config.load()
    return f"{cfg.console_url}/requests?id={quote(str(request_id), safe='')}"


def render_application_received(*, contact_name: str, centre_name: str) -> Rendered:
    return render("application_received", {"greeting": contact_name, "centre_name": centre_name})


def render_welcome(*, greeting: str, centre_name: str, token: str, expires_at: datetime, email: str = "") -> Rendered:
    return render(
        "welcome",
        {"greeting": greeting, "centre_name": centre_name, "cta_url": set_password_url(token), "expires_at": expires_at, "email": email},
    )


def render_application_declined(*, greeting: str, centre_name: str, reason: Optional[str] = None) -> Rendered:
    return render("application_declined", {"greeting": greeting, "centre_name": centre_name, "reason": reason or ""})


def render_new_application(request: dict) -> Rendered:
    return render("new_application", {"request": request, "console_link": console_request_url(request["id"])})


def render_password_reset(
    *, greeting: str, token: str, email: str = "", kind: str = "console", expires_at: datetime | None = None, requested_at: datetime | None = None,
    centre_name: str = "", initiator_name: str = "", initiator_role: str = "",
) -> Rendered:
    """``kind``: ``console`` (the team sent it), ``self`` (the person asked on the sign-in page, ``requested_at``), ``staff`` (the owner / the administrator of
    the centre sent it: they are named, so that the letter does not read like a phishing one - K2-2, C)."""
    return render(
        "password_reset",
        {
            "greeting": greeting, "cta_url": set_password_url(token), "email": email, "kind": kind, "expires_at": expires_at, "requested_at": requested_at,
            "centre_name": centre_name, "initiator_name": initiator_name, "initiator_role": initiator_role,
        },
    )


def render_password_changed(*, greeting: str, changed_at: datetime, cfg: mail_config.MailConfig | None = None) -> Rendered:
    cfg = cfg or mail_config.load()
    return render("password_changed", {"greeting": greeting, "changed_at": changed_at, "support_email": cfg.support_email})


def confirm_email_url(token: str, cfg: mail_config.MailConfig | None = None) -> str:
    cfg = cfg or mail_config.load()
    return f"{cfg.frontend_url}/confirm-email/{quote(token, safe='')}"


def render_email_change_confirm(*, greeting: str, centre_name: str, token: str, expires_at: datetime, cfg: mail_config.MailConfig | None = None) -> Rendered:
    cfg = cfg or mail_config.load()
    return render(
        "email_change_confirm",
        {"greeting": greeting, "centre_name": centre_name, "cta_url": confirm_email_url(token, cfg), "expires_at": expires_at, "support_email": cfg.support_email},
    )


def render_email_change_notice(*, greeting: str, centre_name: str, new_email_masked: str, cfg: mail_config.MailConfig | None = None) -> Rendered:
    cfg = cfg or mail_config.load()
    return render(
        "email_change_notice",
        {"greeting": greeting, "centre_name": centre_name, "new_email_masked": new_email_masked, "support_email": cfg.support_email},
    )


def render_email_changed(*, greeting: str, new_email: str, cfg: mail_config.MailConfig | None = None) -> Rendered:
    cfg = cfg or mail_config.load()
    return render("email_changed", {"greeting": greeting, "new_email": new_email, "support_email": cfg.support_email})


def render_staff_invite(*, centre_name: str, org_role: str, code: str, expires_at: datetime) -> Rendered:
    return render(
        "staff_invite", {"centre_name": centre_name, "org_role": org_role, "cta_url": invite_url(code), "expires_at": expires_at}
    )


def send_application_received(to: str, **kw) -> bool:
    return sender.send(to, render_application_received(**kw))


def send_welcome(to: str, **kw) -> bool:
    return sender.send(to, render_welcome(**kw))


def send_application_declined(to: str, **kw) -> bool:
    return sender.send(to, render_application_declined(**kw))


def send_new_application(request: dict) -> bool:
    to = mail_config.load().admin_email
    if not to:
        logger.warning("ADMIN_NOTIFICATION_EMAIL is not set - the owner is not told about the new request")
        return False
    return sender.send(to, render_new_application(request))


def send_staff_invite(to: str, **kw) -> bool:
    return sender.send(to, render_staff_invite(**kw))


def send_password_reset(to: str, **kw) -> bool:
    return sender.send(to, render_password_reset(**kw))


def send_password_changed(to: str, **kw) -> bool:
    return sender.send(to, render_password_changed(**kw))


def send_email_change_confirm(to: str, **kw) -> bool:
    return sender.send(to, render_email_change_confirm(**kw))


def send_email_change_notice(to: str, **kw) -> bool:
    return sender.send(to, render_email_change_notice(**kw))


def send_email_changed(to: str, **kw) -> bool:
    return sender.send(to, render_email_changed(**kw))
