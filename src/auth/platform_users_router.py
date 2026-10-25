"""/api/v1/platform/users*: the people of the centres - search, card, new invitation, reset link, block / unblock.

Console staff are not listed here (they live in /platform/staff). Passwords and their hashes are never part of any answer."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.mail import letters
from src.models.auth import Organization, OrganizationMember, User, UserSession
from src.models.platform import PlatformStaff

from . import password_links as links
from . import platform_audit as audit
from . import user_sessions
from .platform_common import contains, iso, not_found, page_params, page_response, query_form, utcnow
from .platform_errors import PlatformError
from .platform_security import Principal, require_platform
from .platform_validation import Form, parse_uuid_or_404

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users")

STATUSES = ("active", "never_logged_in", "blocked", "inactive")
ROLES = ("owner", "admin", "teacher")


def _status(u: User) -> str:
    if u.platform_blocked_at is not None:
        return "blocked"
    if not u.is_active:
        return "inactive"
    if u.last_login_at is None:
        return "never_logged_in"
    return "active"


def _row(u: User, org: Optional[Organization], org_role: Optional[str]) -> dict:
    return {
        "id": str(u.id),
        "email": u.email,
        "name": f"{u.last_name} {u.first_name}".strip(),
        "role": u.role,
        "org": (
            {"id": str(org.id), "name": org.name, "org_role": org_role, "blocked": org.blocked_at is not None} if org is not None else None
        ),
        "is_active": bool(u.is_active),
        "blocked": u.platform_blocked_at is not None,
        "status": _status(u),
        "last_login_at": iso(u.last_login_at),
        "created_at": iso(u.created_at),
    }


def _base(db: Session):
    staff = db.query(PlatformStaff.user_id)
    return (
        db.query(User, Organization, OrganizationMember.org_role)
        .outerjoin(Organization, Organization.id == User.default_org_id)
        .outerjoin(OrganizationMember, (OrganizationMember.org_id == User.default_org_id) & (OrganizationMember.user_id == User.id))
        .filter(~User.id.in_(staff))
    )


def _load(db: Session, user_id) -> tuple:
    found = _base(db).filter(User.id == user_id).first()
    if found is None:
        raise not_found("Пользователь")
    return found


def _get(db: Session, user_id_text: str, *, lock: bool = False) -> User:
    uid = parse_uuid_or_404(user_id_text, "Пользователь")
    q = db.query(User).filter(User.id == uid, ~User.id.in_(db.query(PlatformStaff.user_id)))
    if lock:
        q = q.with_for_update()
    user = q.first()
    if user is None:
        raise not_found("Пользователь")
    return user


@router.get("")
def list_users(request: Request, p: Principal = Depends(require_platform("users.view")), db: Session = Depends(get_db)):
    form = query_form(request)
    page = page_params(request, ("name", "email", "last_login_at", "created_at"), "created_at")
    q = form.text("q", "Поиск", required=False, max_len=100)
    org_id = form.uuid("org_id", "Центр", required=False)
    role = form.choice("role", "Роль", ROLES, required=False)
    status = form.choice("status", "Статус", STATUSES, required=False)
    form.done()

    query = _base(db)
    if q:
        like = contains(q)
        query = query.filter(
            or_(
                User.email.ilike(like, escape="\\"), User.first_name.ilike(like, escape="\\"), User.last_name.ilike(like, escape="\\"),
                func.concat(User.last_name, " ", User.first_name).ilike(like, escape="\\"),
                func.concat(User.first_name, " ", User.last_name).ilike(like, escape="\\"),
                Organization.name.ilike(like, escape="\\"), Organization.phone.ilike(like, escape="\\"),
            )
        )
    if org_id:
        query = query.filter(User.id.in_(db.query(OrganizationMember.user_id).filter(OrganizationMember.org_id == org_id)))
    if role:
        query = query.filter(User.role == role)
    if status == "blocked":
        query = query.filter(User.platform_blocked_at.isnot(None))
    elif status == "inactive":
        query = query.filter(User.is_active.is_(False), User.platform_blocked_at.is_(None))
    elif status == "never_logged_in":
        query = query.filter(User.is_active.is_(True), User.last_login_at.is_(None))
    elif status == "active":
        query = query.filter(User.is_active.is_(True), User.last_login_at.isnot(None))
    total = query.count()
    key = {
        "name": func.lower(func.concat(User.last_name, " ", User.first_name)), "email": func.lower(User.email),
        "last_login_at": User.last_login_at, "created_at": User.created_at,
    }[page.sort]
    ordered = key.asc() if page.order == "asc" else key.desc()
    rows = query.order_by(ordered.nulls_last(), User.id).offset(page.offset).limit(page.size).all()
    return page_response([_row(u, o, r) for u, o, r in rows], total, page)


@router.get("/{user_id}")
def get_user(user_id: str, p: Principal = Depends(require_platform("users.view")), db: Session = Depends(get_db)):
    user = _get(db, user_id)
    u, org, org_role = _load(db, user.id)
    out = _row(u, org, org_role)
    memberships = (
        db.query(OrganizationMember, Organization)
        .join(Organization, Organization.id == OrganizationMember.org_id)
        .filter(OrganizationMember.user_id == user.id)
        .order_by(Organization.name)
        .all()
    )
    now = utcnow()
    active_sessions = (
        db.query(func.count(UserSession.id))
        .filter(UserSession.user_id == user.id, UserSession.revoked_at.is_(None), UserSession.expires_at > now)
        .scalar() or 0
    )
    link = links.live_link(db, user.id)
    out.update(
        {
            "memberships": [
                {"org_id": str(o.id), "org_name": o.name, "org_role": m.org_role, "is_active": bool(m.is_active), "org_blocked": o.blocked_at is not None}
                for m, o in memberships
            ],
            "login_count": user.login_count or 0,
            "active_sessions": active_sessions,
            "pending_link": {"purpose": link.purpose, "expires_at": iso(link.expires_at)} if link else None,
            "block": (
                {"blocked_at": iso(user.platform_blocked_at), "reason": user.platform_block_reason}
                if user.platform_blocked_at is not None else None
            ),
            "history": audit.history(db, "user", user.id),
        }
    )
    return out


def _first_org(db: Session, user: User) -> Optional[Organization]:
    return db.get(Organization, user.default_org_id) if user.default_org_id else None


@router.post("/{user_id}/resend-invite")
def resend_invite(
    user_id: str, request: Request, p: Principal = Depends(require_platform("users.manage")), db: Session = Depends(get_db)
):
    user = _get(db, user_id, lock=True)
    if user.platform_blocked_at is not None or not user.is_active:
        raise PlatformError(409, "Пользователь заблокирован", "user_blocked")
    is_owner = db.query(OrganizationMember.id).filter(OrganizationMember.user_id == user.id, OrganizationMember.org_role == "owner").first()
    if is_owner is None:
        raise PlatformError(409, "Приглашение «Задать пароль» отправляется только владельцам центров", "not_owner")
    if user.last_login_at is not None:
        raise PlatformError(409, "Пользователь уже входил — отправьте ссылку сброса пароля", "already_active")
    org = _first_org(db, user)
    token, link = links.issue(db, user.id, "centre_invite", created_by=p.user.id)
    entry = audit.record(
        db, actor_id=p.user.id, action="user.invite_resent", target_type="user", target_id=user.id,
        details={"email": user.email, "link_expires_at": iso(link.expires_at)}, request=request,
    )
    db.commit()
    sent = letters.send_welcome(
        user.email, greeting=" ".join(x for x in (user.first_name, user.patronymic) if x), centre_name=org.name if org else "ALGO",
        token=token, expires_at=link.expires_at, email=user.email,
    )
    entry.details = {**(entry.details or {}), "email_sent": bool(sent)}
    db.commit()
    return {"expires_at": iso(link.expires_at), "email_sent": bool(sent)}


@router.post("/{user_id}/send-reset")
def send_reset(
    user_id: str, request: Request, p: Principal = Depends(require_platform("users.manage")), db: Session = Depends(get_db)
):
    user = _get(db, user_id, lock=True)
    if user.platform_blocked_at is not None or not user.is_active:
        raise PlatformError(409, "Пользователь заблокирован", "user_blocked")
    token, link = links.issue(db, user.id, "reset", created_by=p.user.id)
    entry = audit.record(
        db, actor_id=p.user.id, action="user.reset_sent", target_type="user", target_id=user.id,
        details={"email": user.email, "link_expires_at": iso(link.expires_at)}, request=request,
    )
    db.commit()
    sent = letters.send_password_reset(user.email, greeting=" ".join(x for x in (user.first_name, user.patronymic) if x), token=token, email=user.email)
    entry.details = {**(entry.details or {}), "email_sent": bool(sent)}
    db.commit()
    return {"expires_at": iso(link.expires_at), "email_sent": bool(sent)}


@router.post("/{user_id}/block")
def block_user(
    user_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform("users.block")), db: Session = Depends(get_db),
):
    form = Form(body)
    reason = form.text("reason", "Причина", min_len=3, max_len=500, multiline=True)
    form.done()
    user = _get(db, user_id, lock=True)
    if user.platform_blocked_at is not None:
        raise PlatformError(409, "Пользователь уже заблокирован", "already_blocked")
    user.is_active = False
    user.platform_blocked_at = utcnow()
    user.platform_block_reason = reason
    revoked = user_sessions.revoke_all(db, user.id, user_sessions.REVOKE_USER_BLOCKED)
    audit.record(
        db, actor_id=p.user.id, action="user.blocked", target_type="user", target_id=user.id, reason=reason,
        details={"sessions_revoked": revoked}, request=request,
    )
    db.commit()
    return _row(*_load(db, user.id))


@router.post("/{user_id}/unblock")
def unblock_user(
    user_id: str, request: Request, body: dict = Body(default=None),
    p: Principal = Depends(require_platform("users.block")), db: Session = Depends(get_db),
):
    form = Form(body)
    reason = form.text("reason", "Причина", required=False, max_len=500, multiline=True)
    form.done()
    user = _get(db, user_id, lock=True)
    if user.platform_blocked_at is None:
        raise PlatformError(409, "Пользователь не заблокирован консолью", "not_blocked")
    user.is_active = True
    user.platform_blocked_at = None
    user.platform_block_reason = None
    audit.record(db, actor_id=p.user.id, action="user.unblocked", target_type="user", target_id=user.id, reason=reason, request=request)
    db.commit()
    return _row(*_load(db, user.id))
