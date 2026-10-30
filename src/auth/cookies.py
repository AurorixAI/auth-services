"""Session cookie policy: names, lifetimes and flags, in ONE place.

A leaf module (it imports no router, no dependency and no model) so that the sign-in routes, the dev
quick-login and the request dependencies can all share it without importing each other.

**Names and flags (K2-1, A3).** On every real stack (stage and prod, behind https) both cookies are ``__Host-algo_auth_token`` and
``__Host-algo_refresh_token``: ``Secure``, ``Path=/``, NO ``Domain``, ``HttpOnly``, ``SameSite=Lax``. The ``__Host-`` prefix makes the browser
itself refuse a cookie of that name that a sibling subdomain (``stage.``, ``tg.``, ``landing.`` ...) tries to plant with a ``Domain`` attribute, so
no other subdomain can fix a session onto a victim. The console does the same (``platform_security.COOKIE_SECURE``). On plain-http development the
prefix cannot be used (the browser drops a ``__Host-`` cookie without ``Secure``): the names are ``algo_auth_token`` / ``algo_refresh_token`` there.
The refresh cookie has ``Path=/`` too (the prefix requires it); the server reads it only on ``/auth/*`` routes.

**Lifetimes (D).** The mode is chosen at sign-in and kept on the session row (``user_sessions.remember``):

* «Запомнить меня»: BOTH cookies carry ``Max-Age`` = ``JWT_REFRESH_TTL_DAYS`` (14 days), renewed at every refresh;
* without it: BOTH cookies are session cookies - no ``Max-Age``, no ``Expires`` - so they go when the browser closes; the server session ends 12 h
  after the sign-in anyway (``user_sessions.SHORT_SESSION_HOURS``).

The ACCESS cookie lives as long as the refresh cookie: the JWT inside keeps its own short ``exp`` (``JWT_ACCESS_TTL_MINUTES``, 60) and the server
rejects it with 401 once it is stale, which makes the client call /auth/refresh and retry. If the cookie expired together with the JWT, the browser
would send no credential at all on the next page navigation and the Next.js middleware would send the user to the login page before any client code
could refresh the session.
"""
import os
from typing import Mapping, Optional, TypeVar

from starlette.responses import Response

from . import service as _tokens

# Plain names (development over http) and the ``__Host-`` names of every secure stack.
PLAIN_AUTH_COOKIE = "algo_auth_token"
PLAIN_REFRESH_COOKIE = "algo_refresh_token"
HOST_PREFIX = "__Host-"

# Every cookie of the cabinets is host-only and at the root: the ``__Host-`` rule.
AUTH_COOKIE_PATH = "/"
REFRESH_COOKIE_PATH = "/"

# The cookies of the cabinet before K2-1 (the names without the prefix; the refresh cookie lived on the auth path only). On a secure stack a sign-in and a
# sign-out delete them, so a browser does not carry a dead 30-day cookie that the front end could mistake for a sign-in.
LEGACY_REFRESH_PATH = "/api/v1/auth"


def cookies_are_secure(env: Mapping[str, str] = os.environ) -> bool:
    """Whether the auth cookies carry ``Secure``: on every real stack, i.e. behind https.

    Both real stacks (stage and prod) run ``APP_ENV=staging`` (the compose files of the infrastructure repo
    set that and nothing called ``ENVIRONMENT``), and a real production build sets ``APP_ENV=production``;
    the legacy ``ENVIRONMENT=production`` still counts. ``development`` / ``testing`` (plain http on a
    laptop or in the test client) stay without ``Secure``, otherwise the browser would drop the cookie.
    """
    return env.get("APP_ENV", "") in ("production", "staging") or env.get("ENVIRONMENT", "") == "production"


def cookie_names(secure: bool) -> tuple:
    """(access cookie name, refresh cookie name) for a secure or a plain stack."""
    if secure:
        return HOST_PREFIX + PLAIN_AUTH_COOKIE, HOST_PREFIX + PLAIN_REFRESH_COOKIE
    return PLAIN_AUTH_COOKIE, PLAIN_REFRESH_COOKIE


_IS_PROD = cookies_are_secure()
AUTH_COOKIE_NAME, REFRESH_COOKIE_NAME = cookie_names(_IS_PROD)

R = TypeVar("R", bound=Response)


def refresh_cookie_max_age() -> int:
    """Seconds a REMEMBERED cookie lives: the configured lifetime of a remembered session (``JWT_REFRESH_TTL_DAYS``)."""
    return max(1, int(_tokens.REFRESH_TOKEN_EXPIRE_DAYS)) * 86400


def access_cookie_max_age() -> int:
    """Seconds the access COOKIE lives when remembered: as long as the refresh cookie (the JWT inside is shorter-lived)."""
    return refresh_cookie_max_age()


def set_auth_cookies(
    response: R,
    access_token: str,
    refresh_token: str,
    remember: bool = False,
    *,
    secure: Optional[bool] = None,
) -> R:
    """Attach the httpOnly JWT cookies to *response*. Without *remember* they are session cookies (no ``Max-Age``/``Expires``)."""
    secure = _IS_PROD if secure is None else secure
    auth_name, refresh_name = cookie_names(secure)
    response.set_cookie(
        key=auth_name,
        value=access_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=access_cookie_max_age() if remember else None,
        path=AUTH_COOKIE_PATH,
    )
    response.set_cookie(
        key=refresh_name,
        value=refresh_token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=refresh_cookie_max_age() if remember else None,
        path=REFRESH_COOKIE_PATH,
    )
    return response


def clear_legacy_cookies(response: R, *, secure: Optional[bool] = None) -> R:
    """Delete the cookies under the OLD names (``algo_auth_token`` on ``/``, ``algo_refresh_token`` on ``/api/v1/auth``). Only where the names differ
    from the current ones, i.e. on a secure stack; on plain-http development the old names ARE the current ones."""
    secure = _IS_PROD if secure is None else secure
    if secure:
        response.delete_cookie(PLAIN_AUTH_COOKIE, path=AUTH_COOKIE_PATH, secure=True, httponly=True, samesite="lax")
        response.delete_cookie(PLAIN_REFRESH_COOKIE, path=LEGACY_REFRESH_PATH, secure=True, httponly=True, samesite="lax")
    return response


def clear_auth_cookies(response: R, *, secure: Optional[bool] = None) -> R:
    """Delete both browser cookies (same path they were set with, no domain), and the old-named ones of before K2-1."""
    secure = _IS_PROD if secure is None else secure
    clear_legacy_cookies(response, secure=secure)
    # The deleting Set-Cookie carries the same flags as the one that set the cookie (Secure included).
    auth_name, refresh_name = cookie_names(secure)
    response.delete_cookie(auth_name, path=AUTH_COOKIE_PATH, secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(refresh_name, path=REFRESH_COOKIE_PATH, secure=secure, httponly=True, samesite="lax")
    return response
