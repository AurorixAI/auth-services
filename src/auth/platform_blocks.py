"""Blocks set from the console, as the cabinets see them.

* A centre blocked by the platform team (``organizations.blocked_at``): its staff cannot sign in and every request they make is
  answered ``403 «Доступ центра приостановлен. Свяжитесь с ALGO»``. The request check is in ``dependencies.get_current_active_user_payload``
  (the organisation row is read there anyway: no cache, so an unblock works at once); the sign-in check is ``ensure_login_allowed``.
* A user blocked by the platform team: ``users.is_active`` is off (the cabinets already refuse such an account: sign-in 401, requests
  401) and ``platform_blocked_at`` says it was the console.
* A console account (a ``platform_staff`` row) is not a cabinet account: the cabinets answer its sign-in with the ordinary «Неверная
  почта или пароль».
"""
from __future__ import annotations

from fastapi import HTTPException

from src.core.exceptions import InvalidCredentialsError
from src.models.auth import Organization, User
from src.models.platform import PlatformStaff

ORG_BLOCKED_TEXT = "Доступ центра приостановлен. Свяжитесь с ALGO"


def org_blocked_error() -> HTTPException:
    return HTTPException(403, ORG_BLOCKED_TEXT)


def ensure_login_allowed(db, user: User) -> None:
    """Called by the cabinet sign-in AFTER the password was verified (so it tells nothing to someone who does not know it)."""
    if db.query(PlatformStaff.id).filter(PlatformStaff.user_id == user.id).first() is not None:
        raise InvalidCredentialsError()
    if user.default_org_id is not None:
        blocked = db.query(Organization.blocked_at).filter(Organization.id == user.default_org_id).scalar()
        if blocked is not None:
            raise org_blocked_error()
