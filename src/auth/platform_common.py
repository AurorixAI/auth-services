"""Shared helpers of the platform API: timestamps, pagination, search escaping, client address."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from fastapi import Request

from src.api.rate_limit import client_ip

from .platform_errors import PlatformError, validation_error
from .platform_validation import Form

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
TASHKENT = timezone(timedelta(hours=5))


def utcnow() -> datetime:
    """Naive UTC (tests move the clock by replacing this function)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value: Optional[datetime]) -> Optional[str]:
    """A naive-UTC moment as ``2026-10-25T09:30:00Z``."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + "Z"


def like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def contains(value: str) -> str:
    return f"%{like_escape(value.strip())}%"


def client_meta(request: Optional[Request]) -> tuple[Optional[str], Optional[str]]:
    """The trusted client address (X-Real-IP set by nginx) and a short user agent."""
    if request is None:
        return None, None
    ip = client_ip(request)
    agent = (request.headers.get("user-agent") or "")[:200] or None
    return (None if ip == "unknown" else ip[:64]), agent


@dataclass(frozen=True)
class PageParams:
    page: int
    size: int
    sort: str
    order: str

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def page_params(request: Request, sorts: Sequence[str], default_sort: str, default_order: str = "desc") -> PageParams:
    form = Form(dict(request.query_params))
    page = form.integer("page", "page", minimum=1, maximum=1_000_000) or 1
    size = form.integer("page_size", "page_size", minimum=1, maximum=MAX_PAGE_SIZE) or DEFAULT_PAGE_SIZE
    sort = form.choice("sort", "sort", sorts, required=False) or default_sort
    order = form.choice("order", "order", ("asc", "desc"), required=False) or default_order
    form.done()
    return PageParams(page, size, sort, order)


def page_response(items: list, total: int, p: PageParams, **extra) -> dict:
    out = {"items": items, "total": total, "page": p.page, "page_size": p.size, "pages": max(1, math.ceil(total / p.size)) if total else 0}
    out.update(extra)
    return out


def query_form(request: Request) -> Form:
    return Form(dict(request.query_params))


def day_start_utc(day: str) -> datetime:
    """``YYYY-MM-DD`` (a Tashkent day) as the naive-UTC moment that day starts."""
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, tzinfo=TASHKENT).astimezone(timezone.utc).replace(tzinfo=None)


def date_range(form: Form, from_key: str = "date_from", to_key: str = "date_to") -> tuple[Optional[datetime], Optional[datetime]]:
    """[from, to) in naive UTC from two ``YYYY-MM-DD`` query values (the whole ``date_to`` day is included)."""
    out = []
    for key in (from_key, to_key):
        raw = form.data.get(key)
        if not raw:
            out.append(None)
            continue
        try:
            out.append(day_start_utc(str(raw)))
        except ValueError:
            form.fail(key, "Дата в формате ГГГГ-ММ-ДД")
            out.append(None)
    start, end = out
    if end is not None:
        end = end + timedelta(days=1)
    return start, end


def not_found(what: str = "Объект") -> PlatformError:
    return PlatformError(404, f"{what} не найден", "not_found")
