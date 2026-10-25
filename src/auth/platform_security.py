"""Security of the platform console: the console host, the console session token (own audience and key), the cookie,
and the dependencies every ``/api/v1/platform`` route is built on.

* **Host**: the whole ``/api/v1/platform`` router answers only when ``Host`` (and ``X-Forwarded-Host``, when present) is one of
  ``CONSOLE_HOSTS``; any other host gets the same ``404 Not Found`` as an unknown path. nginx passes ``Host $host``.
* **Token**: a JWT with ``aud=algo-console``, ``type=console`` and its OWN signing key (derived from the secret key) - a cabinet token
  does not even verify here, and a console token does not verify in the cabinets. The JWT only names a row of ``platform_sessions``
  (``sid``); the row decides (revoked / expired / staff disabled close the session at once).
* **Cookie** ``algo_console_session``: host-only, ``HttpOnly``, ``SameSite=Strict``, ``Secure`` on staging/production, 12 h.
* **Origin**: an unsafe request that carries ``Origin`` must come from a console host.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, FrozenSet, Optional

from fastapi import Depends, Request
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from src.core.config import get_settings
from src.core.database import get_db
from src.models.auth import User
from src.models.platform import PlatformRole, PlatformSession, PlatformStaff

from . import platform_permissions as perms
from .cookies import cookies_are_secure
from .platform_common import utcnow
from .platform_errors import PlatformError

COOKIE_NAME = "algo_console_session"
TOKEN_AUDIENCE = "algo-console"
TOKEN_TYPE = "console"
SESSION_HOURS = 12
DEFAULT_CONSOLE_HOSTS = "console.algolab.me,console.stage.algolab.me"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ── host ──────────────────────────────────────────────────────────────────────────────────────


def console_hosts() -> FrozenSet[str]:
    raw = os.getenv("CONSOLE_HOSTS", DEFAULT_CONSOLE_HOSTS)
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _hostname(value: Optional[str]) -> str:
    """``Console.AlgoLab.me:443`` -> ``console.algolab.me`` (an IPv6 literal keeps its brackets)."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.split(":")[0]


def is_console_host(request: Request) -> bool:
    hosts = console_hosts()
    if _hostname(request.headers.get("host")) not in hosts:
        return False
    forwarded = request.headers.get("x-forwarded-host")
    if forwarded is not None:
        # a proxy chain lists hosts left to right; the first is the one the browser asked for
        if _hostname(forwarded.split(",")[0]) not in hosts:
            return False
    return True


def console_guard(request: Request) -> None:
    """Router-level dependency: wrong host -> 404 (the section does not exist there); a foreign Origin on a write -> 403."""
    if not is_console_host(request):
        raise PlatformError(404, "Not Found", "not_found")
    if request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None:
            from urllib.parse import urlsplit

            host = urlsplit(origin).hostname if origin != "null" else None
            if host is None or host.lower() not in console_hosts():
                raise PlatformError(403, "Запрос отклонён", "bad_origin")


# ── token and cookie ──────────────────────────────────────────────────────────────────────────


def _signing_key() -> str:
    secret = get_settings().secret_key
    return hashlib.sha256(f"{secret}|algo-console-session".encode("utf-8")).hexdigest()


def issue_token(user_id, session_id, expires_at: datetime) -> str:
    exp = expires_at.replace(tzinfo=timezone.utc)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "aud": TOKEN_AUDIENCE,
        "type": TOKEN_TYPE,
        "iat": datetime.now(timezone.utc),
        "exp": exp,
    }
    return jwt.encode(payload, _signing_key(), algorithm=get_settings().algorithm)


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, _signing_key(), algorithms=[get_settings().algorithm], audience=TOKEN_AUDIENCE)
    except JWTError:
        return None
    if payload.get("type") != TOKEN_TYPE or not payload.get("sub") or not payload.get("sid"):
        return None
    return payload


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_HOURS * 3600,
        path="/",
        httponly=True,
        secure=cookies_are_secure(),
        samesite="strict",
        # no ``domain``: host-only, the cookie never reaches the cabinets' host
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=cookies_are_secure(), samesite="strict")


# ── the principal ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Principal:
    user: User
    staff: PlatformStaff
    role: PlatformRole
    session: PlatformSession
    permissions: FrozenSet[str]

    @property
    def user_id(self):
        return self.user.id

    @property
    def name(self) -> str:
        return f"{self.user.first_name} {self.user.last_name}".strip()

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def load_principal(db: Session, request: Request, *, allow_must_change: bool = False) -> Principal:
    from uuid import UUID

    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise PlatformError(401, "Войдите в консоль", "not_authenticated")
    payload = decode_token(token)
    expired = PlatformError(401, "Сессия истекла. Войдите снова.", "session_expired")
    if payload is None:
        raise expired
    try:
        user_id, session_id = UUID(payload["sub"]), UUID(payload["sid"])
    except (ValueError, TypeError):
        raise expired from None
    session = db.get(PlatformSession, session_id)
    if session is None or session.user_id != user_id or session.revoked_at is not None or session.expires_at <= utcnow():
        raise expired
    user = db.get(User, user_id)
    staff = db.query(PlatformStaff).filter(PlatformStaff.user_id == user_id).first()
    if user is None or staff is None or not user.is_active or not staff.is_active:
        raise PlatformError(403, "Доступ к консоли отключён", "staff_inactive")
    role = db.get(PlatformRole, staff.role_id)
    if staff.must_change_password and not allow_must_change:
        raise PlatformError(403, "Сначала смените пароль", "must_change_password")
    return Principal(user, staff, role, session, perms.effective_permissions(staff.is_owner, role.permissions if role else ()))


def require_platform(permission: Optional[str] = None, *, allow_must_change: bool = False) -> Callable:
    """A dependency: a signed-in console user (who has ``permission``, when one is named)."""

    def dependency(request: Request, db: Session = Depends(get_db)) -> Principal:
        principal = load_principal(db, request, allow_must_change=allow_must_change)
        if permission is not None and not principal.can(permission):
            raise PlatformError(403, "Недостаточно прав", "forbidden")
        return principal

    return dependency


def session_expiry() -> datetime:
    return utcnow() + timedelta(hours=SESSION_HOURS)
