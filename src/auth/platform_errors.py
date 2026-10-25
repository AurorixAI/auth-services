"""Errors of the platform API and the public demo request: ``{"detail": "<русский текст>", "code": "<machine_code>"}``.

``PlatformError`` is raised anywhere in the new modules; ``register(app)`` installs its handler and a Russian 422 for the
body-validation errors of the new routes (the other routes keep FastAPI's default).
"""
from __future__ import annotations

from typing import Dict, Optional

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

NEW_PREFIXES = ("/api/v1/platform", "/api/v1/public/demo-requests", "/api/v1/auth/password-links")

VALIDATION_TEXT = "Проверьте заполнение формы"


class PlatformError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        code: str,
        *,
        fields: Optional[Dict[str, str]] = None,
        retry_after: Optional[int] = None,
        extra: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.fields = fields
        self.retry_after = retry_after
        self.extra = extra or {}


def validation_error(fields: Dict[str, str], message: str = VALIDATION_TEXT) -> PlatformError:
    return PlatformError(422, message, "validation_error", fields=fields)


def _cors(request: Request) -> dict:
    from src.core.config import get_settings

    origin = request.headers.get("origin", "")
    allowed = get_settings().cors_origins
    if origin and (origin in allowed or "*" in allowed):
        return {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true"}
    return {}


def error_body(exc: PlatformError) -> dict:
    body: dict = {"detail": exc.message, "code": exc.code}
    if exc.fields is not None:
        body["fields"] = exc.fields
    if exc.retry_after is not None:
        body["retry_after"] = exc.retry_after
    body.update(exc.extra)
    return body


async def platform_error_handler(request: Request, exc: PlatformError):
    headers = _cors(request)
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(status_code=exc.status_code, content=error_body(exc), headers=headers)


_RU = {
    "missing": "Обязательное поле",
    "string_too_short": "Слишком короткое значение",
    "string_too_long": "Слишком длинное значение",
    "string_type": "Нужен текст",
    "int_type": "Нужно целое число",
    "int_parsing": "Нужно целое число",
    "bool_type": "Нужно значение да/нет",
    "uuid_parsing": "Некорректный идентификатор",
    "enum": "Недопустимое значение",
    "literal_error": "Недопустимое значение",
    "greater_than_equal": "Слишком маленькое значение",
    "less_than_equal": "Слишком большое значение",
    "value_error": "Некорректное значение",
}


async def validation_handler(request: Request, exc: RequestValidationError):
    """Russian 422 for the new routes; everything else is handled exactly as FastAPI does by default."""
    if not request.url.path.startswith(NEW_PREFIXES):
        from fastapi.exception_handlers import request_validation_exception_handler

        return await request_validation_exception_handler(request, exc)
    fields: Dict[str, str] = {}
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")]
        key = ".".join(loc) or "_"
        fields.setdefault(key, _RU.get(err.get("type", ""), "Некорректное значение"))
    err = PlatformError(422, VALIDATION_TEXT, "validation_error", fields=fields)
    return JSONResponse(status_code=422, content=error_body(err), headers=_cors(request))


def register(app) -> None:
    app.add_exception_handler(PlatformError, platform_error_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
