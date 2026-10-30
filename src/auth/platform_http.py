"""HTTP envelope of the console API: no CORS headers, ``Cache-Control: no-store``.

The console is same-origin (its own host proxies /api/v1/platform): no other origin is ever allowed to read an answer, so the
``Access-Control-*`` headers that the global CORS middleware adds for the cabinets' origins are removed from every /api/v1/platform
response (a preflight included), and nothing the console says may be cached by a browser or a proxy.
"""
from __future__ import annotations

from fastapi import Response

PREFIX = "/api/v1/platform"


class PlatformResponseHeaders:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == PREFIX or path.startswith(PREFIX + "/")):
            await self.app(scope, receive, send)
            return

        async def wrapped(message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", [])
                    if not k.lower().startswith(b"access-control-") and k.lower() not in (b"cache-control", b"pragma", b"expires")
                ]
                headers += [(b"cache-control", b"no-store"), (b"pragma", b"no-cache")]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


def no_store(response: "Response") -> None:
    """A dependency of the GET of a page of a one-time token (a reset link, a new address, an invitation): the answer names the person / the address and must not be
    kept by a browser or a proxy (K2-2 R3, 9)."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
