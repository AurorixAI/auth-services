"""Building and delivering a letter: multipart/alternative (text + HTML), From «ALGO <noreply@algolab.me>», Reply-To.

``deliver`` is the only function that touches the network; a test replaces it. A failure never raises: the caller gets False and
the reason goes to the log (without the body: it carries one-time links).
"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from . import config as mail_config
from .render import Rendered

logger = logging.getLogger(__name__)


def _line(value: str) -> str:
    """A header value on one line: CR/LF in user-supplied text cannot start a new header."""
    return re.sub(r"[\r\n]+", " ", value or "").strip()


def build_message(to: str, rendered: Rendered, cfg: mail_config.MailConfig | None = None) -> EmailMessage:
    cfg = cfg or mail_config.load()
    msg = EmailMessage()
    msg["Subject"] = _line(rendered.subject)
    msg["From"] = formataddr((_line(cfg.from_name), _line(cfg.from_address)))
    msg["To"] = _line(to)
    if cfg.reply_to:
        msg["Reply-To"] = _line(cfg.reply_to)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=_line(cfg.from_address).rsplit("@", 1)[-1] or "algolab.me")
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Auto-Response-Suppress"] = "OOF, AutoReply"
    msg.set_content(rendered.text)  # text/plain first ...
    msg.add_alternative(rendered.html, subtype="html")  # ... then text/html: multipart/alternative, the richest part last
    return msg


def deliver(msg: EmailMessage, cfg: mail_config.MailConfig | None = None) -> bool:
    cfg = cfg or mail_config.load()
    to = msg["To"]
    if not cfg.smtp_configured:
        logger.info("SMTP not configured - letter NOT sent. To: %s Subject: %s", to, msg["Subject"])
        return False
    try:
        if cfg.smtp_port == 465:
            with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15, context=ssl.create_default_context()) as server:
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.send_message(msg, from_addr=cfg.from_address, to_addrs=[to])
        else:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.send_message(msg, from_addr=cfg.from_address, to_addrs=[to])
        logger.info("Letter sent to %s - %s", to, msg["Subject"])
        return True
    except Exception:  # noqa: BLE001 - a letter that did not go must never break the request
        logger.exception("Failed to send a letter to %s (%s)", to, msg["Subject"])
        return False


def send(to: str, rendered: Rendered) -> bool:
    try:
        return deliver(build_message(to, rendered))
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send a letter to %s", to)
        return False
