"""/api/v1/platform/auth/*: login, logout, logout-all, me, change-password."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.api.rate_limit import client_bucket, hit_window
from src.core.database import get_db

from . import platform_audit as audit
from . import platform_auth
from .platform_common import utcnow
from .platform_errors import PlatformError
from .platform_security import (
    cookie_name,
    Principal,
    clear_session_cookie,
    decode_token,
    require_platform,
    set_session_cookie,
)
from .platform_validation import Form

router = APIRouter(prefix="/auth")


LOGIN_PER_MINUTE = 30


def login_rate_limit(request: Request) -> None:
    """30 sign-in requests a minute per client address, counted before anything touches the database (a flood is dropped here)."""
    count, left = hit_window(f"rl:console_login_ip:{client_bucket(request)}", 60)
    if count > LOGIN_PER_MINUTE:
        raise PlatformError(429, "Слишком много запросов. Повторите позже.", "rate_limited", retry_after=left)


@router.post("/login", dependencies=[Depends(login_rate_limit)])
def console_login(request: Request, body: dict = Body(default=None), db: Session = Depends(get_db)):
    form = Form(body)
    email = form.text("email", "Почта", max_len=255)
    password = form.data.get("password")
    if not isinstance(password, str) or not password:
        form.fail("password", "Укажите пароль")
    if isinstance(password, str) and len(password) > 1000:
        form.fail("password", "Пароль слишком длинный")
    form.done()
    principal, token = platform_auth.login(db, request, email, password)
    response = JSONResponse(platform_auth.me_payload(principal))
    set_session_cookie(response, token)
    return response


@router.post("/logout")
def console_logout(request: Request, db: Session = Depends(get_db)):
    """Always 200: revokes the session named by the cookie (if it is a valid one) and clears the cookie."""
    token = request.cookies.get(cookie_name())
    payload = decode_token(token) if token else None
    if payload:
        from uuid import UUID

        from src.models.platform import PlatformSession

        try:
            session = db.get(PlatformSession, UUID(payload["sid"]))
        except (ValueError, TypeError):
            session = None
        if session is not None and session.revoked_at is None and str(session.user_id) == payload["sub"]:
            session.revoked_at = utcnow()
            session.revoke_reason = "logout"
            audit.record(db, actor_id=session.user_id, action="console.logout", target_type="console", target_id=session.user_id, request=request)
            db.commit()
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@router.post("/logout-all")
def console_logout_all(request: Request, p: Principal = Depends(require_platform(allow_must_change=True)), db: Session = Depends(get_db)):
    count = platform_auth.revoke_sessions(db, p.user.id, "logout_all")
    audit.record(db, actor_id=p.user.id, action="console.logout_all", target_type="console", target_id=p.user.id, details={"revoked": count}, request=request)
    db.commit()
    response = JSONResponse({"ok": True, "revoked": count})
    clear_session_cookie(response)
    return response


@router.get("/me")
def console_me(p: Principal = Depends(require_platform(allow_must_change=True))):
    return platform_auth.me_payload(p)


@router.post("/change-password")
def console_change_password(
    request: Request,
    body: dict = Body(default=None),
    p: Principal = Depends(require_platform(allow_must_change=True)),
    db: Session = Depends(get_db),
):
    form = Form(body)
    current = form.data.get("current_password")
    new = form.data.get("new_password")
    if not isinstance(current, str) or not current:
        form.fail("current_password", "Укажите текущий пароль")
    if not isinstance(new, str) or not new:
        form.fail("new_password", "Укажите новый пароль")
    form.done()
    platform_auth.change_password(db, request, p, current, new)
    return {"ok": True}
