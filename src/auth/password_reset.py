"""«Забыли пароль» and the reset link from the leadership of a centre (K2-2, B and C).

* ``forgot``: the public form. The answer is ALWAYS the same (code, body, headers); the look-up of the address, the link and the letter all happen in the
  background. Only an employee of a cabinet (owner / admin / teacher) with an active place in an active, unblocked centre gets a letter; a console account, a
  blocked or removed person, an unknown address get none (and no difference in the answer).
* ``send_to_member``: «Сотрудники → ⋯ → Отправить ссылку для смены пароля». Allowed exactly: owner -> admin / teacher of his centre; admin -> teacher of his
  centre. Everything else (himself, a peer, the owner, another centre, a former employee, a person who is in another centre too) is the same 404.
  The letter names who sent it; the password does not change until the employee sets a new one; the journal of the centre gets who -> whom -> when (no token).
* The letters go in the background (G2); a limit of 150 «Забыли пароль» letters per day for the whole platform keeps half of the Brevo budget for the
  invitations and the welcome letters.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.api.rate_limit import hit_window
from src.models.auth import MemberAudit, Organization, OrganizationMember, User
from src.models.platform import PlatformStaff

from . import business_clock as bc
from . import password_links as links
from .background import background_session
from .platform_errors import PlatformError

logger = logging.getLogger(__name__)

NEUTRAL_TEXT = "Если такая почта зарегистрирована, мы отправили на неё ссылку. Проверьте «Спам» и что адрес такой же, как при входе."

PER_MAILBOX = (3, 3600)       # letters asked for ONE address, per hour
PER_ADDRESS = (10, 3600)      # ... from ONE client address (IPv6: a /64), per hour
PER_DOMAIN = (30, 3600)       # ... to ONE mail domain, per hour
PER_PLATFORM_DAY = 150        # ... over the whole platform, per day (half of the free Brevo budget of 300)
STAFF_LINKS_PER_HOUR = 5      # links sent to ONE employee by his leadership, per hour

NOT_FOUND = PlatformError(404, "Сотрудник не найден", "member_not_found")

# who may send to whom: actor org_role -> the org_roles of the target
HIERARCHY = {"owner": ("admin", "teacher"), "admin": ("teacher",)}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]  # no address in a counter key


def normalize(email: object) -> str:
    return email.strip().lower() if isinstance(email, str) else ""


# ---------------------------------------------------------------------------------------------------------- who is a cabinet account
def is_console_account(db: Session, user_id) -> bool:
    return db.query(PlatformStaff.id).filter(PlatformStaff.user_id == user_id).first() is not None


def reachable_by_forgot(db: Session, user: Optional[User]) -> bool:
    """An employee of a cabinet with an active place in an active, unblocked centre (the only one who gets a «Забыли пароль» letter)."""
    if user is None or not user.is_active or user.role not in ("owner", "admin", "teacher") or is_console_account(db, user.id):
        return False
    place = (
        db.query(OrganizationMember.id)
        .join(Organization, Organization.id == OrganizationMember.org_id)
        .filter(
            OrganizationMember.user_id == user.id, OrganizationMember.is_active.is_(True), Organization.status == "active",
            Organization.blocked_at.is_(None),
        )
        .first()
    )
    return place is not None


# ---------------------------------------------------------------------------------------------------------- «Забыли пароль»
def count_request(email: str, client: str) -> bool:
    """Count one request in every window (always all four steps: the cost does not depend on the address). True = within the limits."""
    domain = email.rpartition("@")[2]
    ok = True
    for key, (limit, window) in (
        (f"rl:forgot_mail:{_digest(email)}", PER_MAILBOX),
        (f"rl:forgot_ip:{client}", PER_ADDRESS),
        (f"rl:forgot_domain:{_digest(domain)}", PER_DOMAIN),
    ):
        count, _ = hit_window(key, window)
        if count > limit:
            ok = False
    return ok


def platform_has_room() -> bool:
    """One slot of the day's budget of «Забыли пароль» letters for the platform; the warning goes to the log once a day."""
    count, _ = hit_window("rl:forgot_platform_day", 86400)
    if count <= PER_PLATFORM_DAY:
        return True
    warned, _ = hit_window("rl:forgot_platform_warned", 86400)
    if warned == 1:
        logger.warning("forgot-password: the daily budget of %s letters for the platform is spent - no more letters today", PER_PLATFORM_DAY)
    return False


def greeting_of(user: User) -> str:
    return " ".join(part for part in (user.first_name, user.patronymic) if part) or (user.first_name or "")


def process_forgot(db: Session, email: str, requested_at: datetime) -> bool:
    """The background half of «Забыли пароль»: look up, issue, send. Returns whether a letter was handed to SMTP. Never raises to the caller's caller."""
    from src.mail import letters  # late: the mail package imports src.auth modules

    user = db.query(User).filter(func.lower(User.email) == email).first()
    if not reachable_by_forgot(db, user):
        return False
    if not platform_has_room():
        return False
    token, link = links.issue(db, user.id, "reset", created_by=None)
    db.commit()
    return bool(
        letters.send_password_reset(
            user.email, greeting=greeting_of(user), token=token, email=user.email, kind="self", expires_at=link.expires_at, requested_at=requested_at,
        )
    )


def forgot_in_background(email: str) -> None:
    db = background_session()
    try:
        process_forgot(db, email, bc.utc_now())
    except Exception as exc:  # noqa: BLE001 - the page already answered; the person asks again
        logger.error("forgot-password: failed %s", type(exc).__name__)
        db.rollback()
    finally:
        db.close()


# ---------------------------------------------------------------------------------------------------------- from the leadership
def eligible_target(db: Session, org_id, actor_org_role: Optional[str], actor_user_id, member_id):
    """(member, user) the actor may act on, else the single 404. The user row is NOT locked here: the lock order of the access code is
    link / e-mail-change rows -> the per-user session lock -> the user row (a sign-in, a link, a confirmation all follow it: no deadlock)."""
    from uuid import UUID

    allowed = HIERARCHY.get(actor_org_role or "")
    if not allowed:
        raise NOT_FOUND
    try:
        mid = UUID(str(member_id))
    except (TypeError, ValueError):
        raise NOT_FOUND from None
    member = (
        db.query(OrganizationMember)
        .filter(OrganizationMember.id == mid, OrganizationMember.org_id == org_id, OrganizationMember.is_active.is_(True))
        .first()
    )
    if member is None or member.org_role not in allowed or str(member.user_id) == str(actor_user_id):
        raise NOT_FOUND
    user = db.query(User).filter(User.id == member.user_id).first()
    if user is None or not user.is_active or is_console_account(db, user.id):
        raise NOT_FOUND
    elsewhere = (
        db.query(OrganizationMember.id)
        .filter(OrganizationMember.user_id == user.id, OrganizationMember.org_id != org_id, OrganizationMember.is_active.is_(True))
        .first()
    )
    if elsewhere is not None:  # one account in two centres: a reset from ONE centre would open the other too
        raise NOT_FOUND
    return member, user


def send_to_member(db: Session, *, org_id, actor: dict, member_id, background=None) -> dict:
    """«Отправить ссылку для смены пароля»: the letter goes to the EMPLOYEE (the leadership never sees the link)."""
    member, user = eligible_target(db, org_id, actor.get("org_role"), actor.get("user_id"), member_id)
    count, left = hit_window(f"rl:staff_reset:{user.id}", 3600)
    if count > STAFF_LINKS_PER_HOUR:
        raise PlatformError(429, "Слишком много ссылок для этого сотрудника. Повторите через час.", "rate_limited", retry_after=left)
    token, link = links.issue(db, user.id, "reset", created_by=actor.get("user_id"))
    now = bc.utc_now()
    db.add(
        MemberAudit(
            org_id=org_id, member_id=member.id, user_id=user.id, actor_id=actor.get("user_id"), action="password_link_sent",
            snapshot={"initiator_role": actor.get("org_role")}, created_at=now,
        )
    )
    actor_user = db.get(User, actor.get("user_id"))
    org = db.get(Organization, org_id)
    job = dict(
        to=user.email, greeting=greeting_of(user), token=token, email=user.email, kind="staff", expires_at=link.expires_at,
        centre_name=org.name if org else "", initiator_name=" ".join(x for x in (actor_user.first_name, actor_user.last_name) if x) if actor_user else "",
        initiator_role=actor.get("org_role") or "",
    )
    db.commit()
    if background is not None:
        background.add_task(send_staff_letter, job)
    else:
        send_staff_letter(job)
    return {"email": user.email, "message": "Ссылка отправляется сотруднику на почту"}


def send_staff_letter(job: dict) -> bool:
    from src.mail import letters

    to = job.pop("to")
    try:
        return bool(letters.send_password_reset(to, **job))
    except Exception as exc:  # noqa: BLE001
        logger.error("staff password link: the letter failed %s", type(exc).__name__)
        return False
