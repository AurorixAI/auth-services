"""Session cookie policy: names, lifetimes and flags, in ONE place.

A leaf module (it imports no router, no dependency and no model) so that the sign-in routes, the dev
quick-login and the request dependencies can all share it without importing each other.

Lifetimes: the REFRESH cookie lives as long as the refresh JWT (``JWT_REFRESH_TTL_DAYS``, 30 by default).
The ACCESS cookie lives exactly as long: the JWT inside keeps its own short ``exp``
(``JWT_ACCESS_TTL_MINUTES``, 60) and the server rejects it with 401 once it is stale, which makes the
client call /auth/refresh and retry. If the cookie expired together with the JWT, the browser would send no
credential at all on the next page navigation and the Next.js middleware would send the user to the login
page before any client code could refresh the session.

«Запомнить меня» off: both cookies are BROWSER-SESSION cookies (no Max-Age / Expires): the browser drops
them when it closes. The mode lives in the refresh JWT (claim ``rem: false``), so every rotation of the
refresh token (``/auth/refresh``) keeps it without the client having to say it again.
"""
import os
from typing import Optional, TypeVar

from starlette.responses import Response

from . import service as _tokens

AUTH_COOKIE_NAME = "algo_auth_token"
REFRESH_COOKIE_NAME = "algo_refresh_token"

# The refresh cookie is only sent to the auth routes. The real mount point is /api/v1/auth/*
# (see main_v2.py) - a plain "/auth" would never match, and the browser would never send it back.
AUTH_COOKIE_PATH = "/"
REFRESH_COOKIE_PATH = "/api/v1/auth"

_IS_PROD = os.getenv("ENVIRONMENT", "development") == "production"
_COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", None)  # e.g. ".algo.kz"

R = TypeVar("R", bound=Response)


def refresh_cookie_max_age() -> int:
    """Seconds the refresh cookie lives: the configured lifetime of the refresh JWT."""
    return max(1, int(_tokens.REFRESH_TOKEN_EXPIRE_DAYS)) * 86400


def access_cookie_max_age() -> int:
    """Seconds the access COOKIE lives: as long as the refresh cookie (the JWT inside is shorter-lived)."""
    return refresh_cookie_max_age()


def remember_from_refresh_token(refresh_token: str) -> bool:
    """False only for a refresh JWT of a login WITHOUT «Запомнить меня» (claim ``rem`` is false).

    Everything else - tokens issued before the option existed, a token that cannot be read - is the
    long-lived default. The tokens passed here were signed by this service a moment ago.
    """
    payload = _tokens.decode_token(refresh_token)
    return not (payload and payload.get("rem") is False)


def set_auth_cookies(
    response: R, access_token: str, refresh_token: str, remember: Optional[bool] = None
) -> R:
    """Attach the httpOnly JWT cookies to *response*.

    *remember* False makes both of them browser-session cookies; None (the callers' default) reads the
    mode from the refresh token itself.
    """
    if remember is None:
        remember = remember_from_refresh_token(refresh_token)
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=access_token,
        httponly=True,
        secure=_IS_PROD,
        samesite="lax",
        max_age=access_cookie_max_age() if remember else None,
        path=AUTH_COOKIE_PATH,
        domain=_COOKIE_DOMAIN,
    )
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=_IS_PROD,
        samesite="lax",
        max_age=refresh_cookie_max_age() if remember else None,
        path=REFRESH_COOKIE_PATH,
        domain=_COOKIE_DOMAIN,
    )
    return response


def clear_auth_cookies(response: R) -> R:
    """Delete both browser cookies (same path/domain they were set with)."""
    response.delete_cookie(AUTH_COOKIE_NAME, path=AUTH_COOKIE_PATH, domain=_COOKIE_DOMAIN)
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH, domain=_COOKIE_DOMAIN)
    return response
