"""Building and delivering a letter: multipart/alternative (text + HTML), From «ALGO <noreply@algolab.me>», Reply-To.

``deliver`` is the only function that touches the network; a test replaces it. A failure never raises: the caller gets False and
the reason goes to the log as the class of the error (without the body: it carries one-time links; without the address: only a masked one).
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


def mask_address(address: str) -> str:
    """``john.doe@gmail.com`` -> ``j***@gmail.com``: the only form of an address that may reach the log (it is the personal data of an applicant)."""
    text = (address or "").strip()
    if not text:
        return "***"
    local, _, domain = text.partition("@")
    return f"{local[:1]}***@{domain}" if domain else f"{local[:1]}***"


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
    msg.letter_kind = rendered.letter  # for the log only (a Python attribute, not a header of the letter)
    return msg


def deliver(msg: EmailMessage, cfg: mail_config.MailConfig | None = None) -> bool:
    cfg = cfg or mail_config.load()
    to = str(msg["To"])
    kind, who = getattr(msg, "letter_kind", "letter"), mask_address(to)  # the log keeps the kind and a masked address: no Subject (it carries the applicant's words)
    if not cfg.smtp_configured:
        logger.info("SMTP not configured - letter %s to %s NOT sent", kind, who)
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
        logger.info("Letter %s sent to %s", kind, who)
        return True
    except Exception as exc:  # noqa: BLE001 - a letter that did not go must never break the request
        logger.error("Failed to send a letter %s to %s: %s", kind, who, type(exc).__name__)  # the class only: the text and the trace of smtplib carry the address
        return False


def send(to: str, rendered: Rendered) -> bool:
    try:
        return deliver(build_message(to, rendered))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send a letter %s to %s: %s", rendered.letter, mask_address(to), type(exc).__name__)
        return False
