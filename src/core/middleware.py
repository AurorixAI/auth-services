"""
Auth Service — Shared middleware.

  • RequestIDMiddleware  — attaches / generates X-Request-ID per request
  • configure_logging()  — sets up structlog JSON logging (prod) or pretty (dev)
"""
from __future__ import annotations

import logging
import re
import sys
import uuid

import httpx

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# ---------------------------------------------------------------------------
# Request-ID middleware
# ---------------------------------------------------------------------------

class DiagnosticCapabilityLogFilter(logging.Filter):
    """Keep route/status evidence while removing public invite capabilities."""
    _path = re.compile(r'(/(?:api/v1/)?(?:auth/|internal/)?diagnostic/)(?!sessions(?:/|\b))[^/?\s\"\x27]+')
    _query = re.compile(r'([?&]invite_code=)[^&\s\"\x27]+')
    # «Задать пароль» / «Сброс пароля»: the token in the path IS the credential (one-time, but valid for 72 h / 1 h) - never in a log line
    _links = re.compile(r'(/(?:api/v1/)?(?:auth/password-links|set-password)/)[^/?\s\"\x27]+')

    @classmethod
    def _redact(cls, value):
        if not isinstance(value, (str, httpx.URL)):
            return value
        text = cls._links.sub(r'\1[redacted]', str(value))
        return cls._query.sub(r'\1[redacted]', cls._path.sub(r'\1[redacted]', text))

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            # Uvicorn AccessFormatter unpacks the five original arguments.
            # Preserve them; redact paths and HTTPX URL objects in place.
            if not record.args:
                record.msg = self._redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(self._redact(value) for value in record.args)
            elif isinstance(record.args, dict):
                record.args = {key: self._redact(value) for key, value in record.args.items()}
        return True


def sentry_before_send(event, hint=None):
    """``before_send`` for sentry_sdk (Sentry is not initialised in this service yet; when it is, pass this function): the request URL,
    the breadcrumbs and the exception text of an event never carry a link token or an invite code."""
    redact = DiagnosticCapabilityLogFilter._redact
    request = event.get("request") or {}
    if isinstance(request.get("url"), str):
        request["url"] = redact(request["url"])
    if isinstance(request.get("query_string"), str):
        request["query_string"] = redact("?" + request["query_string"])[1:]
    for crumb in (event.get("breadcrumbs") or {}).get("values", []) if isinstance(event.get("breadcrumbs"), dict) else event.get("breadcrumbs") or []:
        for key in ("message",):
            if isinstance(crumb.get(key), str):
                crumb[key] = redact(crumb[key])
        data = crumb.get("data")
        if isinstance(data, dict):
            for k, v in list(data.items()):
                data[k] = redact(v)
    if isinstance(event.get("transaction"), str):
        event["transaction"] = redact(event["transaction"])
    for value in (event.get("exception") or {}).get("values", []):
        if isinstance(value.get("value"), str):
            value["value"] = redact(value["value"])
    return event


_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")


def safe_request_id(candidate: str | None) -> str:
    """The client's X-Request-ID when it is 1-64 characters of [A-Za-z0-9-] (LD-22), otherwise a fresh one.

    The value goes into JSON logs and the response header: a newline, a multi-megabyte string or any other free text from the
    client must never get there.
    """
    if candidate and _REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Propagate (a well-formed one) or generate X-Request-ID and bind it to structlog context."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = safe_request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id  # the 500 handler (outside this middleware) reports it to the client
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ---------------------------------------------------------------------------
# structlog configuration
# ---------------------------------------------------------------------------

def configure_logging(is_development: bool = False) -> None:
    """Call once at application startup."""
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if is_development:
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(DiagnosticCapabilityLogFilter())
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in ("uvicorn.access", "gunicorn.access"):
        logging.getLogger(name).addFilter(DiagnosticCapabilityLogFilter())
