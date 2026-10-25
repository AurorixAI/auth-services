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
