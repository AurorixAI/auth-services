"""Changing the e-mail of an employee (K2-2, E). Only the OWNER of the centre, for an admin / a teacher of his centre.

The new address must prove that the letters reach it: a link goes to the NEW address (24 h); the OLD address gets a notice. Until the link is confirmed the old
address stays the login. Confirming (a public page: no session needed, it works from a phone or another browser) replaces the address of the SAME ``user.id``
(groups, lessons and payroll stay), ends every session and every live link, and sends «Почта изменена» to the new address.
A GET of the link only shows the page (a mail scanner opens links); the POST confirms.
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.rate_limit import hit_window
from src.models.auth import MemberAudit, Organization, OrganizationMember, User
from src.models.platform import EmailChange, PasswordLink

from . import password_links as links
from . import user_sessions
from .password_reset import _digest, eligible_target, greeting_of, is_console_account
from .platform_common import iso, utcnow
from .platform_errors import PlatformError, validation_error
from .platform_validation import EMAIL_RE

logger = logging.getLogger(__name__)

TTL = timedelta(hours=24)
PER_TARGET_HOUR = 5
PER_NEW_ADDRESS_DAY = 5
TAKEN = PlatformError(409, "Этот адрес уже используется", "email_taken")

OK, USED, REVOKED, EXPIRED, MISSING = links.OK, links.USED, links.REVOKED, links.EXPIRED, links.MISSING


def mask(address: str) -> str:
    from src.mail.sender import mask_address

    return mask_address(address)


def _taken(db: Session, email: str) -> bool:
    return db.query(User.id).filter(func.lower(User.email) == email).first() is not None


def _clean_email(raw: object) -> str:
    email = raw.strip().lower() if isinstance(raw, str) else ""
    if not email or len(email) > 255 or not EMAIL_RE.match(email) or ".." in email or len(email.split("@")[0]) > 64:
        raise validation_error({"email": "Укажите корректную почту"}, message="Укажите корректную почту")
    return email


# ---------------------------------------------------------------------------------------------------------- the owner asks
def start(db: Session, *, org_id, actor: dict, member_id, new_email_raw: object, background=None) -> dict:
    member, user = eligible_target(db, org_id, actor.get("org_role"), actor.get("user_id"), member_id)
    if actor.get("org_role") != "owner":  # the hierarchy lets an admin reach a teacher; the e-mail is the owner's alone
        from .password_reset import NOT_FOUND

        raise NOT_FOUND
    new_email = _clean_email(new_email_raw)
    if new_email == (user.email or "").strip().lower():
        raise validation_error({"email": "Это уже текущая почта сотрудника"}, message="Это уже текущая почта сотрудника")
    if _taken(db, new_email):
        raise TAKEN
    count, left = hit_window(f"rl:email_change_target:{user.id}", 3600)
    if count > PER_TARGET_HOUR:
        raise PlatformError(429, "Слишком много попыток для этого сотрудника. Повторите через час.", "rate_limited", retry_after=left)
    count, left = hit_window(f"rl:email_change_address:{_digest(new_email)}", 86400)
    if count > PER_NEW_ADDRESS_DAY:
        raise PlatformError(429, "На этот адрес уже отправлено много писем. Повторите завтра.", "rate_limited", retry_after=left)

    now = utcnow()
    # one pending change per user: a new request ends the earlier one (its link answers «replaced»)
    db.query(EmailChange).filter(
        EmailChange.user_id == user.id, EmailChange.used_at.is_(None), EmailChange.revoked_at.is_(None)
    ).update({"revoked_at": now}, synchronize_session=False)
    token = secrets.token_urlsafe(32)
    row = EmailChange(
        user_id=user.id, org_id=org_id, new_email=new_email, token_hash=links.hash_token(token), requested_by=actor.get("user_id"),
        expires_at=now + TTL, created_at=now,
    )
    db.add(row)
    db.add(
        MemberAudit(
            org_id=org_id, member_id=member.id, user_id=user.id, actor_id=actor.get("user_id"), action="email_change_started",
            snapshot={"new_email_masked": mask(new_email)}, created_at=now,
        )
    )
    org = db.get(Organization, org_id)
    job = dict(
        old_email=user.email, new_email=new_email, token=token, expires_at=row.expires_at, greeting=greeting_of(user),
        centre_name=org.name if org else "",
    )
    try:
        db.commit()
    except IntegrityError:  # two requests at once: the other one won the single live row
        db.rollback()
        raise PlatformError(409, "Смена почты уже запрошена. Повторите через минуту.", "change_in_progress") from None
    if background is not None:
        background.add_task(send_start_letters, job)
    else:
        send_start_letters(job)
    return {"email": user.email, "new_email": new_email, "expires_at": iso(row.expires_at), "message": "Письмо с подтверждением отправлено на новый адрес"}


def send_start_letters(job: dict) -> None:
    from src.mail import letters

    for send, to, kwargs in (
        (letters.send_email_change_confirm, job["new_email"], dict(greeting=job["greeting"], centre_name=job["centre_name"], token=job["token"], expires_at=job["expires_at"])),
        (letters.send_email_change_notice, job["old_email"], dict(greeting=job["greeting"], centre_name=job["centre_name"], new_email_masked=mask(job["new_email"]))),
    ):
        try:
            send(to, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error("email change: a letter failed %s", type(exc).__name__)


# ---------------------------------------------------------------------------------------------------------- the link
def classify(row: Optional[EmailChange]) -> str:
    if row is None:
        return MISSING
    if row.used_at is not None:
        return USED
    if row.revoked_at is not None:
        return REVOKED
    if row.expires_at <= utcnow():
        return EXPIRED
    return OK


def lookup(db: Session, raw: str, *, for_update: bool = False):
    if not raw or len(raw) > 200:
        return MISSING, None
    q = db.query(EmailChange).filter(EmailChange.token_hash == links.hash_token(raw))
    if for_update:
        q = q.with_for_update()
    row = q.first()
    return classify(row), row


def problem(status: str) -> PlatformError:
    if status == USED:
        return PlatformError(410, "Этот адрес уже подтверждён. Войдите с новой почтой.", "link_used")
    if status == REVOKED:
        return PlatformError(410, "Запрошена новая смена почты — используйте ссылку из последнего письма.", "link_revoked")
    if status == EXPIRED:
        return PlatformError(410, "Срок действия ссылки истёк. Попросите владельца центра отправить её ещё раз.", "link_expired")
    return PlatformError(404, "Ссылка недействительна. Попросите владельца центра отправить её ещё раз.", "link_not_found")


def info(db: Session, token: str) -> dict:
    """What the confirmation page shows (a GET spends nothing)."""
    status, row = lookup(db, token)
    if status != OK:
        raise problem(status)
    user = db.get(User, row.user_id)
    org = db.get(Organization, row.org_id)
    if user is None:
        raise problem(MISSING)
    return {
        "valid": True, "first_name": user.first_name, "org_name": org.name if org else None,
        "new_email": row.new_email, "old_email_masked": mask(user.email or ""), "expires_at": iso(row.expires_at),
    }


def confirm(db: Session, token: str, background=None) -> dict:
    status, row = lookup(db, token, for_update=True)
    if status != OK:
        raise problem(status)
    now = utcnow()
    # lock order: the per-user session lock, then the rows of the links, then the user row (the same as a sign-in and a link that sets a password)
    user_sessions.lock_user(db, row.user_id)
    db.query(PasswordLink).filter(
        PasswordLink.user_id == row.user_id, PasswordLink.used_at.is_(None), PasswordLink.revoked_at.is_(None)
    ).update({"revoked_at": now}, synchronize_session=False)  # the live links went to the OLD address
    user = db.query(User).filter(User.id == row.user_id).with_for_update().populate_existing().first()
    member = (
        db.query(OrganizationMember)
        .filter(OrganizationMember.user_id == row.user_id, OrganizationMember.org_id == row.org_id, OrganizationMember.is_active.is_(True))
        .first()
    )
    if user is None or not user.is_active or member is None or is_console_account(db, user.id):
        raise PlatformError(403, "Доступ закрыт центром. Обратитесь к руководству центра.", "access_closed")
    if _taken(db, row.new_email):
        raise TAKEN
    old = user.email
    user.email = row.new_email
    row.used_at = now
    db.query(EmailChange).filter(
        EmailChange.user_id == user.id, EmailChange.id != row.id, EmailChange.used_at.is_(None), EmailChange.revoked_at.is_(None)
    ).update({"revoked_at": now}, synchronize_session=False)
    user_sessions.revoke_all(db, user.id, user_sessions.REVOKE_EMAIL_CHANGED)
    db.add(
        MemberAudit(
            org_id=row.org_id, member_id=member.id, user_id=user.id, actor_id=row.requested_by, action="email_changed",
            snapshot={"new_email_masked": mask(row.new_email), "old_email_masked": mask(old or "")}, created_at=now,
        )
    )
    job = dict(to=row.new_email, greeting=greeting_of(user), new_email=row.new_email)
    try:
        db.commit()
    except IntegrityError:  # the address was taken between the check and the commit (the unique index of users.email)
        db.rollback()
        raise TAKEN from None
    if background is not None:
        background.add_task(send_changed_letter, job)
    else:
        send_changed_letter(job)
    return {"message": "Адрес подтверждён. Войдите с новой почтой и прежним паролем."}


def send_changed_letter(job: dict) -> None:
    from src.mail import letters

    to = job.pop("to")
    try:
        letters.send_email_changed(to, **job)
    except Exception as exc:  # noqa: BLE001
        logger.error("email change: the final letter failed %s", type(exc).__name__)
