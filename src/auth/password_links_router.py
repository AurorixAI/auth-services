"""``GET|POST /api/v1/auth/password-links/{token}``: the page behind the «Задать пароль» / «Сброс пароля» button."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.api.rate_limit import client_bucket, client_ip, rate_limit
from src.core.database import get_db
from src.core.exceptions import InvalidCredentialsError
from src.core.security import hash_password
from src.models.auth import Organization, User

from . import password_links as links
from . import password_rules
from . import user_sessions
from .cookies import set_auth_cookies
from .platform_audit import record as audit_record
from .platform_blocks import ORG_BLOCKED_TEXT, ensure_centre_usable, ensure_login_allowed
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

NOT_FOUND = PlatformError(404, "Ссылка недействительна. Запросите новую.", "link_not_found")


EXPIRED_RESET_TEXT = "Срок действия ссылки истёк. Запросите новую на странице входа."
EXPIRED_INVITE_TEXT = "Срок действия ссылки истёк. Напишите нам — пришлём новую."
USER_CLOSED_BY_CENTRE = "Вашу учётную запись отключил центр. Обратитесь к руководству центра."
USER_CLOSED_BY_ALGO = "Доступ приостановлен. Свяжитесь с ALGO"


def _problem(status: str, link=None) -> PlatformError:
    """The refusal of a dead link. ``purpose`` tells the page what to offer next: «Запросить новую ссылку» for a ``reset``, «напишите нам» for a welcome link."""
    extra = {"purpose": link.purpose} if link is not None else None
    if status == links.USED:
        return PlatformError(410, "Эта ссылка уже использована. Если нужно, запросите новую.", "link_used", extra=extra)
    if status == links.REVOKED:
        return PlatformError(410, "Выдана новая ссылка — используйте её из последнего письма.", "link_revoked", extra=extra)
    if status == links.EXPIRED:
        text = EXPIRED_RESET_TEXT if link is not None and link.purpose == "reset" else EXPIRED_INVITE_TEXT
        return PlatformError(410, text, "link_expired", extra=extra)
    return PlatformError(404, "Ссылка недействительна. Запросите новую.", "link_not_found")


def _first_name_greeting(user: User) -> str:
    parts = [user.first_name, user.patronymic]
    return " ".join(p for p in parts if p) or user.first_name


@router.get("/{token}", dependencies=[Depends(rate_limit(LINK_LIMIT, endpoint=LINK_RATE_ENDPOINT))])
def link_info(token: str, db: Session = Depends(get_db)):
    status, link = links.lookup(db, token)
    if status != links.OK:
        raise _problem(status, link)
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
def set_password(
    token: str, request: Request, background: BackgroundTasks, body: dict = Body(default=None), db: Session = Depends(get_db)
):
    form = Form(body)
    password = form.data.get("password")
    confirm = form.data.get("password_confirm")
    problem = password_rules.problem(password, min_length=MIN_PASSWORD)
    if problem:
        form.fail("password", problem)
    if "password" not in form.errors and confirm != password:
        form.fail("password_confirm", "Пароли не совпадают")
    owner = links.owner_of(db, token)
    if owner is None:
        raise _problem(links.MISSING)
    user_sessions.lock_user(db, owner)  # lock order: the per-user lock -> the link row -> the user row (an issue, a sign-in, a confirmation follow it too)
    status, link = links.lookup(db, token, for_update=True, grace=True)  # a ``reset`` link has 5 more minutes for the POST than the page shows
    if status != links.OK:
        raise _problem(status, link)
    form.done()
    user = db.get(User, link.user_id)
    if user is None:
        raise _problem(links.MISSING)
    again = password_rules.problem(password, email=user.email, min_length=MIN_PASSWORD)  # the one rule, now with the e-mail of the account
    if again:
        raise validation_error({"password": again})
    if not user.is_active:
        raise PlatformError(403, USER_CLOSED_BY_ALGO if user.platform_blocked_at is not None else USER_CLOSED_BY_CENTRE, "user_blocked")
    try:
        ensure_login_allowed(db, user)
        ensure_centre_usable(db, user)
    except HTTPException as exc:  # the centre is blocked / not active: the password stays unset, the link stays valid
        raise PlatformError(403, str(exc.detail) if exc.detail else ORG_BLOCKED_TEXT, "org_blocked") from None
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
    audit_record(db, actor_id=None, action="user.password_set_by_link", target_type="user", target_id=user.id,
                 details={"purpose": link.purpose}, request=request)
    db.commit()

    mail = dict(to=user.email, greeting=_first_name_greeting(user), changed_at=now) if link.purpose == "reset" else None
    result = _svc(db, request).login(LoginRequest(email=user.email, password=password))
    response = _auth_response_with_cookies(result)
    if mail is not None:  # «Пароль изменён» goes to the mailbox in the background (L9)
        background.add_task(_send_changed_letter, mail)
    response.background = background  # a Response returned by hand carries its own background tasks
    return response


def _send_changed_letter(job: dict) -> None:
    from src.mail import letters

    to = job.pop("to")
    try:
        letters.send_password_changed(to, **job)
    except Exception as exc:  # noqa: BLE001
        logger.error("password changed: the letter failed %s", type(exc).__name__)
