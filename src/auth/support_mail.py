"""The letters of «Поддержка» (K4a), sent AFTER the response (as the invitations of K2-2, G2): a session of their own, the letter, the recorded outcome. Never raises.

* a new ticket / a reply of the author -> the mailbox of the support (``MAIL_REPLY_TO`` / support@algolab.me): the number, the centre, the role, the first 300 characters, the link to the card;
* an answer of the team -> the author: «есть ответ - откройте «Мои обращения»», WITHOUT the text of the answer and without a Reply-To (the answer is given in the cabinet).
* TWO daily budgets, counted when a letter is really about to go (LESSONS 72): the letters to the team (30 a day for the platform; one an hour about one ticket) and the letters
  «есть ответ» to the authors (50 a day; one in 30 minutes about one ticket).  A noisy author can spend only the first; over a budget the letter is ``skipped``, inside the
  per-ticket window ``throttled`` - the console shows the mark, the answer is in the cabinet anyway.  Both are separate from the «Забыли пароль» budget (150).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from sqlalchemy import or_

from src.api.rate_limit import hit_window
from src.mail import config as mail_config
from src.mail import letters
from src.models.auth import Organization, OrganizationMember, User
from src.models.support import SupportMessage, SupportTicket

from . import background
from . import platform_common as pc
from . import support_rules as rules

logger = logging.getLogger(__name__)


def has_room(key: str, per_day: int) -> bool:
    """One slot of the day's budget ``key``; the warning goes to the log once a day."""
    count, _ = hit_window(key, 86400)
    if count <= per_day:
        return True
    warned, _ = hit_window(key + ":warned", 86400)
    if warned == 1:
        logger.warning("support: the daily budget of %s letters (%s) is spent - no more letters today (the answers stay in the cabinet)", per_day, key)
    return False


def _claim(db, ticket_id, column: str, seconds: int) -> bool:
    """The one letter of this kind about this ticket per ``seconds``: an atomic UPDATE, so two replies a moment apart cannot both pass.  Costs the platform budget nothing."""
    col = getattr(SupportTicket, column)
    moment = pc.utcnow()
    taken = (
        db.query(SupportTicket)
        .filter(SupportTicket.id == ticket_id, or_(col.is_(None), col <= moment - timedelta(seconds=seconds)))
        .update({column: moment}, synchronize_session=False)
    )
    db.commit()
    return taken == 1


def _outcome(db, ticket_id, column: str, seconds: int, key: str, per_day: int, send) -> str:
    """throttled: this ticket had such a letter recently; skipped: the platform's day is spent (counted when a letter really is about to go: LESSONS 72); else sent / failed."""
    if not _claim(db, ticket_id, column, seconds):
        return "throttled"
    if not has_room(key, per_day):
        return "skipped"
    try:
        return "sent" if send() else "failed"
    except Exception as exc:  # noqa: BLE001 - the ticket is safe; the console shows «failed»
        logger.error("support: a letter failed: %s", type(exc).__name__)  # the class only: no address, no text
        return "failed"


def _centre_name(db, org_id) -> str:
    return db.query(Organization.name).filter(Organization.id == org_id).scalar() or ""


def deliver_new_ticket(ticket_id: str) -> None:
    db = background.background_session()
    try:
        ticket = db.get(SupportTicket, UUID(str(ticket_id)))
        if ticket is None:
            return
        status = _outcome(
            db, ticket.id, "team_mail_at", rules.TEAM_MAIL_PER_TICKET_SECONDS, rules.TEAM_MAIL_KEY, rules.TEAM_MAIL_PER_DAY,
            lambda: letters.send_support_new(
                mail_config.load().support_email, ticket_id=str(ticket.id), number=ticket.number, centre_name=_centre_name(db, ticket.org_id), author_role=ticket.author_role, text=ticket.text,
            )
        )
        ticket.team_mail_status = status
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("support: the notice of a new ticket failed: %s", type(exc).__name__)
        db.rollback()
    finally:
        db.close()


def deliver_author_reply(message_id: str) -> None:
    db = background.background_session()
    try:
        message = db.get(SupportMessage, UUID(str(message_id)))
        ticket = db.get(SupportTicket, message.ticket_id) if message is not None else None
        if message is None or ticket is None:
            return
        status = _outcome(
            db, ticket.id, "team_mail_at", rules.TEAM_MAIL_PER_TICKET_SECONDS, rules.TEAM_MAIL_KEY, rules.TEAM_MAIL_PER_DAY,
            lambda: letters.send_support_author_reply(
                mail_config.load().support_email, ticket_id=str(ticket.id), number=ticket.number, centre_name=_centre_name(db, ticket.org_id), author_role=ticket.author_role, text=message.text,
            )
        )
        message.mail_status, message.mail_status_at = status, pc.utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("support: the notice of a reply failed: %s", type(exc).__name__)
        db.rollback()
    finally:
        db.close()


def deliver_staff_reply(message_id: str) -> None:
    """To the author: only «there is an answer». The text of the answer never goes into the letter."""
    db = background.background_session()
    try:
        message = db.get(SupportMessage, UUID(str(message_id)))
        ticket = db.get(SupportTicket, message.ticket_id) if message is not None else None
        author = db.get(User, ticket.author_user_id) if ticket is not None and ticket.author_user_id else None
        if message is None or ticket is None:
            return
        if author is None or not author.email:
            status = "skipped"
        else:
            role = (
                db.query(OrganizationMember.org_role)
                .filter(OrganizationMember.user_id == author.id, OrganizationMember.org_id == ticket.org_id)
                .scalar()
            ) or ticket.author_role
            greeting = " ".join(part for part in (author.first_name, author.patronymic) if part) or (author.first_name or "")
            status = _outcome(
                db, ticket.id, "author_mail_at", rules.AUTHOR_MAIL_PER_TICKET_SECONDS, rules.AUTHOR_MAIL_KEY, rules.AUTHOR_MAIL_PER_DAY,
                lambda: letters.send_support_reply(author.email, greeting=greeting, number=ticket.number, org_role=role),
            )
        message.mail_status, message.mail_status_at = status, pc.utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("support: the notice of an answer failed: %s", type(exc).__name__)
        db.rollback()
    finally:
        db.close()
