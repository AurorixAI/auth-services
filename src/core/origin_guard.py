"""AD-2: a request that changes something must come from a page of ALGO (the source check of every mutating route).

The browser sends the session cookies on every request to our host, whoever's page started it. ``SameSite=Lax`` and the ``__Host-`` names
(K2-1) already keep a foreign page from riding on them; this check is the second wall, on the SERVER, for every ``POST/PUT/PATCH/DELETE``
of the application at once (one ASGI middleware: a new route is covered the day it is written, nothing to remember).

**The decision** (``request_is_foreign``), in this order:

1. ``Sec-Fetch-Site: cross-site`` - every current browser sends it and a page's script cannot set it: foreign.
2. ``Origin`` present (every browser sends it on a POST/PUT/PATCH/DELETE; ``null`` of a sandboxed frame included): it must be one of the
   allowed origins, else foreign. A value that is not an origin at all is foreign.
3. No ``Origin`` but a ``Referer`` (an old browser; a privacy extension that strips Origin): the origin of the Referer must be allowed.
4. Neither header (curl, a server-to-server call, the Node side of the front): NOT judged. Such a request is not made by a browser page, so
   no foreign page can have started it with the visitor's cookies; refusing it would break the services and our own tests for nothing. A
   browser that sends neither is not a browser a foreign page can drive (a cross-origin form POST always carries Origin).

**Allowed origins of the cabinets**: ``CORS_ORIGINS`` (the product origins; ``*`` = any, as in CORS) PLUS the origin of ``FRONTEND_URL``. The second one
is deliberate (LESSONS 68): on 30.10 the sign-out broke because prod's ``CORS_ORIGINS`` did not name ``app.algolab.me`` yet; the address the product is
served from must work even if the CORS list is one step behind. The landing (``algolab.me``) is never in either list (K3).
The console has its own wall (``platform_security.console_guard``: the console host's origins) with the same decision.

**Exempt routes** (``EXEMPT``) are listed ONE BY ONE (method + path), never by a prefix: they carry no ambient cookie and are protected otherwise; each has
its reason here. A new route under ``/api/v1/internal`` or ``/api/v1/public`` is judged like any other until someone adds it here on purpose, and the guard
test (``tests/test_ad2_origin_guard.py``) fails on it with that question, and on any exempt route whose dependencies read a cabinet cookie.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.core.config import get_settings

logger = logging.getLogger(__name__)

MUTATING_METHODS = frozenset(("POST", "PUT", "PATCH", "DELETE"))
BAD_ORIGIN_CODE = "bad_origin"
BAD_ORIGIN_TEXT = "Запрос должен приходить с сайта ALGO"

_INTERNAL = "service-to-service (diagnostic-service), not a browser: protected by INTERNAL_SERVICE_TOKEN (router-level dependency), no cookie is read"
_PUBLIC = (
    "the demo request of the landing (algolab.me, a foreign origin BY DESIGN: it is never in CORS_ORIGINS): anonymous, no cookie, protected by Turnstile + "
    "rate limits + the request id (idempotency) / the signed details token; it changes nothing of a visitor's own"
)
# (METHOD, path template) -> why a foreign Origin is not judged there. ``{name}`` matches one path segment.
EXEMPT = {
    ("POST", "/api/v1/internal/diagnostic/{code}/start"): _INTERNAL,
    ("POST", "/api/v1/internal/diagnostic/{code}/complete"): _INTERNAL,
    ("POST", "/api/v1/public/demo-requests"): _PUBLIC,
    ("PATCH", "/api/v1/public/demo-requests/{request_id}"): _PUBLIC,
}
# Prefixes whose mutating routes must ALL be listed above (the guard test asks); nothing else is exempt by location.
EXEMPT_AREAS = ("/api/v1/internal", "/api/v1/public")


def _template_regex(path: str) -> "re.Pattern":
    return re.compile("^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(path)) + "$")


_EXEMPT_PATTERNS = tuple((method, _template_regex(path)) for method, path in EXEMPT)
# The platform console answers only on the console host and checks its own origins in ``platform_security.console_guard`` (a wrong host is 404 FIRST, so
# the console does not reveal itself to a probe from a foreign page); the middleware leaves the section to that dependency.
PLATFORM_PREFIX = "/api/v1/platform"


def normalize_origin(value: Optional[str]) -> Optional[str]:
    """``https://App.AlgoLab.me:443/`` -> ``https://app.algolab.me``; None when the value is not a plain http(s) origin (a path, a query, userinfo, ``null``)."""
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host or parts.username is not None or parts.query or parts.fragment or parts.path not in ("", "/"):
        return None
    if ":" in host:
        host = f"[{host}]"
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port not in (None, default) else "")


def referer_origin(value: Optional[str]) -> Optional[str]:
    """The origin a Referer (a full address, path included) points at."""
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    return normalize_origin(f"{parts.scheme}://{parts.netloc}") if parts.scheme and parts.netloc else None


def frontend_origin() -> Optional[str]:
    """The origin of ``FRONTEND_URL`` (its path, if somebody wrote one, is dropped: ``https://app.algolab.me/login`` -> ``https://app.algolab.me``)."""
    return referer_origin(os.getenv("FRONTEND_URL"))


def cabinet_origins() -> Optional[frozenset]:
    """The origins of the cabinets (CORS_ORIGINS + FRONTEND_URL, normalised); None = anybody (``*`` in CORS_ORIGINS)."""
    listed = list(get_settings().cors_origins)
    if "*" in listed:
        return None
    front = frontend_origin()
    allowed = {normalize_origin(o) for o in listed}
    if front:
        allowed.add(front)
    allowed.discard(None)
    return frozenset(allowed)


def cabinet_origin_allowed(origin: str) -> bool:
    allowed = cabinet_origins()
    return allowed is None or normalize_origin(origin) in allowed


def request_is_foreign(headers: Mapping[str, str], origin_allowed: Callable[[str], bool]) -> bool:
    """True when the headers say that a page of another site started the request (the rules in the module text). ``headers`` has lower-case names."""
    if (headers.get("sec-fetch-site") or "").strip().lower() == "cross-site":
        return True
    origin = headers.get("origin")
    if origin is not None:
        return not origin_allowed(origin)
    referer = headers.get("referer")
    if referer:
        found = referer_origin(referer)
        return found is None or not origin_allowed(found)
    return False


def is_exempt(method: str, path: str) -> bool:
    return any(m == method and pattern.match(path) for m, pattern in _EXEMPT_PATTERNS)


def is_platform(path: str) -> bool:
    return path == PLATFORM_PREFIX or path.startswith(PLATFORM_PREFIX + "/")


class OriginGuardMiddleware:
    """403 ``{"detail": text, "code": "bad_origin"}`` for a POST/PUT/PATCH/DELETE that another site started, before anything else runs (no cookie is read, no body is
    read, no route is matched). Everything else - GET, preflight, the exempt prefixes, the console section - passes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") in MUTATING_METHODS:
            path = scope.get("path", "")
            if not is_exempt(scope["method"], path) and not is_platform(path):
                headers: dict = {}
                for name, value in scope.get("headers", []):
                    headers.setdefault(name.decode("latin-1").lower(), value.decode("latin-1"))  # the FIRST of a repeated header: the one Starlette's CORS reads too
                if request_is_foreign(headers, cabinet_origin_allowed):
                    logger.warning("foreign origin refused: %s %s origin=%r", scope.get("method"), path, headers.get("origin"))
                    await JSONResponse({"detail": BAD_ORIGIN_TEXT, "code": BAD_ORIGIN_CODE}, status_code=403)(scope, receive, send)
                    return
        await self.app(scope, receive, send)
