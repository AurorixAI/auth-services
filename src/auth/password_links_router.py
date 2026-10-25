"""``GET|POST /api/v1/auth/password-links/{token}``: the page behind the «Задать пароль» / «Сброс пароля» button."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.api.rate_limit import client_bucket, client_ip, rate_limit
from src.core.database import get_db
from src.core.exceptions import InvalidCredentialsError
from src.core.security import hash_password
from src.models.auth import Organization, User

from . import password_links as links
from . import user_sessions
from .cookies import set_auth_cookies
from .platform_blocks import ORG_BLOCKED_TEXT, ensure_login_allowed
from .platform_common import iso, utcnow
from .platform_errors import PlatformError, validation_error
from .platform_validation import Form
from .router import _auth_response_with_cookies, _svc
from .schemas import LoginRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/password-links", tags=["Authentication"])

LINK_LIMIT = "60/minute"
LINK_RATE_ENDPOINT = "auth-password-link"
MIN_PASSWORD = 8
MAX_PASSWORD = 128

NOT_FOUND = PlatformError(404, "Ссылка недействительна. Запросите новую.", "link_not_found")


def _problem(status: str) -> PlatformError:
    if status == links.USED:
        return PlatformError(410, "Эта ссылка уже использована. Если нужно, запросите новую.", "link_used")
    if status == links.REVOKED:
        return PlatformError(410, "Выдана новая ссылка — используйте её из последнего письма.", "link_revoked")
    if status == links.EXPIRED:
        return PlatformError(410, "Срок действия ссылки истёк. Запросите новую — ответьте на письмо с приглашением.", "link_expired")
    return PlatformError(404, "Ссылка недействительна. Запросите новую.", "link_not_found")


def _first_name_greeting(user: User) -> str:
    parts = [user.first_name, user.patronymic]
    return " ".join(p for p in parts if p) or user.first_name


@router.get("/{token}", dependencies=[Depends(rate_limit(LINK_LIMIT, endpoint=LINK_RATE_ENDPOINT))])
def link_info(token: str, db: Session = Depends(get_db)):
    status, link = links.lookup(db, token)
    if status != links.OK:
        raise _problem(status)
    user = db.get(User, link.user_id)
    if user is None:
        raise _problem(links.MISSING)
    org = db.get(Organization, user.default_org_id) if user.default_org_id else None
    return {
        "valid": True,
        "purpose": link.purpose,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "greeting": _first_name_greeting(user),
        "email": user.email,
        "org_name": org.name if org else None,
        "expires_at": iso(link.expires_at),
    }


@router.post("/{token}", dependencies=[Depends(rate_limit(LINK_LIMIT, endpoint=LINK_RATE_ENDPOINT))])
def set_password(token: str, request: Request, body: dict = Body(default=None), db: Session = Depends(get_db)):
    form = Form(body)
    password = form.data.get("password")
    confirm = form.data.get("password_confirm")
    if not isinstance(password, str) or not password:
        form.fail("password", "Укажите пароль")
    elif "\x00" in password:
        form.fail("password", "Пароль содержит недопустимые символы")
    elif len(password) < MIN_PASSWORD:
        form.fail("password", f"Пароль не короче {MIN_PASSWORD} символов")
    elif len(password) > MAX_PASSWORD:
        form.fail("password", f"Пароль не длиннее {MAX_PASSWORD} символов")
    if "password" not in form.errors and confirm != password:
        form.fail("password_confirm", "Пароли не совпадают")
    status, link = links.lookup(db, token, for_update=True)
    if status != links.OK:
        raise _problem(status)
    form.done()
    user = db.get(User, link.user_id)
    if user is None:
        raise _problem(links.MISSING)
    if not user.is_active:
        raise PlatformError(403, "Доступ приостановлен. Свяжитесь с ALGO", "user_blocked")
    try:
        ensure_login_allowed(db, user)
    except HTTPException:  # the centre is blocked: the password stays unset, the link stays valid
        raise PlatformError(403, ORG_BLOCKED_TEXT, "org_blocked") from None
    except InvalidCredentialsError:  # a console account has no cabinet
        raise _problem(links.MISSING) from None

    now = utcnow()
    user.password_hash = hash_password(password)
    user.must_change_password = False
    user.is_verified = True
    link.used_at = now
    db.query(links.PasswordLink).filter(
        links.PasswordLink.user_id == user.id, links.PasswordLink.id != link.id, links.PasswordLink.used_at.is_(None),
        links.PasswordLink.revoked_at.is_(None),
    ).update({"revoked_at": now}, synchronize_session=False)
    user_sessions.revoke_all(db, user.id, user_sessions.REVOKE_PASSWORD_CHANGED)  # every earlier sign-in ends with the old password
    db.commit()

    result = _svc(db, request).login(LoginRequest(email=user.email, password=password))
    return _auth_response_with_cookies(result)
