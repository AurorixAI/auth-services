"""Creating the first platform owner (used by ``scripts/create_platform_owner.py``; the logic is here so that it is tested)."""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.security import hash_password
from src.models.auth import User
from src.models.platform import PlatformRole, PlatformStaff

from . import platform_permissions as perms
from .platform_auth import validate_new_password
from .platform_validation import EMAIL_RE


class OwnerError(Exception):
    """A refusal with a Russian text meant for the person at the terminal."""


def create_owner(db: Session, email: str, password: str, first_name: str = "Владелец", last_name: str = "ALGO") -> User:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email) or ".." in email:
        raise OwnerError("Укажите корректную почту")
    reason = validate_new_password(password, email)
    if reason:
        raise OwnerError(reason)
    if db.query(User.id).filter(func.lower(User.email) == email).first() is not None:
        raise OwnerError("Эта почта уже используется в ALGO")
    perms.ensure_template_roles(db)
    role = db.query(PlatformRole).filter(PlatformRole.is_system.is_(True)).first()
    user = User(
        email=email, password_hash=hash_password(password), first_name=(first_name or "Владелец").strip()[:100],
        last_name=(last_name or "ALGO").strip()[:100], role="teacher", default_org_id=None, language="ru", is_active=True,
        is_verified=True, must_change_password=False,
    )
    db.add(user)
    db.flush()
    db.add(PlatformStaff(user_id=user.id, role_id=role.id, is_owner=True, is_active=True, must_change_password=False))
    db.commit()
    return user


def reset_owner(db: Session, email: str, password: str) -> User:
    """A new password for an existing platform owner (a forgotten password, or a lock that must be lifted by hand): the password is replaced,
    every console session is ended, the failure counts and locks of the address are cleared, the account is active again."""
    from . import platform_audit as audit
    from .platform_auth import revoke_sessions, unlock_email

    email = (email or "").strip().lower()
    reason = validate_new_password(password, email)
    if reason:
        raise OwnerError(reason)
    user = db.query(User).filter(func.lower(User.email) == email).first()
    staff = db.query(PlatformStaff).filter(PlatformStaff.user_id == user.id).first() if user else None
    if staff is None or not staff.is_owner:
        raise OwnerError("Владелец консоли с такой почтой не найден")
    user.password_hash = hash_password(password)
    user.is_active = True
    staff.is_active = True
    staff.must_change_password = False
    staff.failed_logins, staff.failed_at, staff.locked_until = 0, None, None
    revoke_sessions(db, user.id, "password_reset")
    audit.record(
        db, actor_id=None, action="staff.password_reset", target_type="staff", target_id=user.id,
        details={"email": email, "via": "scripts/reset_platform_owner.py"},
    )
    db.commit()
    unlock_email(email)
    return user
