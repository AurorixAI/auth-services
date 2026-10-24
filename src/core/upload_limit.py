"""Refuse an oversized upload BEFORE its body is read (DM-001).

``file: UploadFile = File(...)`` makes FastAPI/Starlette receive and spool the whole multipart body (to memory, then to disk) before the route
runs, so a size check inside the route comes too late.  This ASGI middleware looks only at the request line and the ``Content-Length`` header:
a body that is declared too large is answered with 413 at once and never read; a body with no declared length is answered with 411.
"""
from __future__ import annotations

import json
from typing import Dict

from starlette.types import ASGIApp, Receive, Scope, Send

MULTIPART_OVERHEAD = 64 * 1024  # boundaries and headers around the file part


class UploadSizeLimitMiddleware:
    """``limits``: {path suffix: (max file bytes, human text of the limit)} for POST requests."""

    def __init__(self, app: ASGIApp, limits: Dict[str, tuple]) -> None:
        self.app = app
        self.limits = limits

    def _limit_for(self, scope: Scope):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return None
        path = scope.get("path", "")
        for suffix, limit in self.limits.items():
            if path.endswith(suffix):
                return limit
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit = self._limit_for(scope)
        if limit is None:
            await self.app(scope, receive, send)
            return
        max_bytes, human = limit
        declared = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break
        if declared is None:
            await self._refuse(scope, send, 411, "Не указан размер файла.")
            return
        if declared > max_bytes + MULTIPART_OVERHEAD:
            await self._refuse(scope, send, 413, f"Файл слишком большой (макс. {human}).")
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope: Scope, send: Send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close")]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
