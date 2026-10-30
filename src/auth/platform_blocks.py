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
USER_BLOCKED_TEXT = "Аккаунт заблокирован. Свяжитесь с руководством вашего центра или с ALGO"
USER_REMOVED_TEXT = "Доступ закрыт центром. Обратитесь к руководству центра"


def org_blocked_error() -> HTTPException:
    return HTTPException(403, ORG_BLOCKED_TEXT)


NO_CENTRE_TEXT = "Ваш аккаунт не привязан к центру. Обратитесь к руководству центра или в ALGO"
CENTRE_NOT_ACTIVE_TEXT = "Центр не активен. Обратитесь в ALGO"


def ensure_login_allowed(db, user: User) -> None:
    """Called by the cabinet sign-in (and by a refresh) AFTER the password / the session was verified (so it tells nothing to someone who does not know it)."""
    if is_console_account(db, user):
        raise InvalidCredentialsError()
    if user.default_org_id is not None:
        blocked = db.query(Organization.blocked_at).filter(Organization.id == user.default_org_id).scalar()
        if blocked is not None:
            raise org_blocked_error()


def ensure_centre_usable(db, user: User) -> None:
    """K2-2 (F): at the SIGN-IN (after the right password) a person without a centre, or of a centre that is not active, is told so (403 with the reason)
    instead of being let in to meet a loop of 403s at every page."""
    if user.default_org_id is None:
        raise HTTPException(403, NO_CENTRE_TEXT)
    status = db.query(Organization.status).filter(Organization.id == user.default_org_id).scalar()
    if status is None:
        raise HTTPException(403, NO_CENTRE_TEXT)
    if status != "active":
        raise HTTPException(403, CENTRE_NOT_ACTIVE_TEXT)


def is_console_account(db, user: User) -> bool:
    user_id = getattr(user, "id", None)
    return user_id is not None and db.query(PlatformStaff.id).filter(PlatformStaff.user_id == user_id).first() is not None


def account_closed_error(user: User) -> HTTPException:
    """K2-2 (F): what a person is told when the PASSWORD WAS RIGHT but the account is switched off: blocked by the platform team, or closed by his centre."""
    return HTTPException(403, USER_BLOCKED_TEXT if getattr(user, "platform_blocked_at", None) is not None else USER_REMOVED_TEXT)
