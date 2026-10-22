"""The one source of "now" and "today" for the operations code (LOGIC-B O13, TIME_CONVENTIONS.md).

Two groups of time columns live side by side (see TIME_CONVENTIONS.md):

* **UTC** - an instant, naive ``timestamp`` holding UTC: ``created_at``, ``student_groups.joined_at / left_at``,
  ``group_audit.created_at``, everything new in this package;
* **Tashkent** - the wall time of the centre (UTC+5, no DST): ``lesson_date`` / ``HH:MM`` of the schedule and
  ``group_attendance.started_at / completed_at``.

They are mixed ONLY through the helpers below; a guard test (``tests/test_time_convention_guard.py``) forbids the
arithmetic / comparison of the Tashkent columns with ``utcnow`` / ``created_at`` / ``joined_at`` / ``left_at``
anywhere else.  Business dates are stored as ``YYYY-MM-DD`` strings.

Tests replace the clock with ``set_clock`` (a callable returning an aware datetime) or with the ``freeze`` helper
of ``tests/ops_helpers.py``.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = ZoneInfo(os.getenv("BUSINESS_TIMEZONE", "Asia/Tashkent"))

_clock: Optional[Callable[[], datetime]] = None


def set_clock(clock: Optional[Callable[[], datetime]]) -> None:
    """Install (or, with ``None``, remove) a replacement clock; the callable returns an aware datetime."""
    global _clock
    _clock = clock


def business_now() -> datetime:
    """Now, aware, in the business timezone."""
    moment = _clock() if _clock is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("the business clock must return an aware datetime")
    return moment.astimezone(BUSINESS_TIMEZONE)


def business_now_naive() -> datetime:
    """Now as the naive Tashkent wall time that ``group_attendance.started_at / completed_at`` hold."""
    return business_now().replace(tzinfo=None)


def business_today() -> date:
    return business_now().date()


def utc_now() -> datetime:
    """Now as a naive UTC instant (the type of every UTC column)."""
    return business_now().astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_business_date(moment: datetime) -> date:
    """The business day of a naive-UTC instant (a naive value is UTC; an aware one is converted)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(BUSINESS_TIMEZONE).date()


def business_wall_to_utc(wall: Optional[datetime]) -> Optional[datetime]:
    """Naive Tashkent wall time -> naive UTC (``None`` stays ``None``).  The only way to compare a Tashkent
    column with a UTC one."""
    if wall is None:
        return None
    if wall.tzinfo is not None:
        wall = wall.astimezone(BUSINESS_TIMEZONE).replace(tzinfo=None)
    return wall.replace(tzinfo=BUSINESS_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None)


def utc_to_business_wall(moment: Optional[datetime]) -> Optional[datetime]:
    """Naive UTC -> naive Tashkent wall time (the inverse of ``business_wall_to_utc``)."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(BUSINESS_TIMEZONE).replace(tzinfo=None)


def lesson_instant_utc(day: date, minutes: int) -> datetime:
    """``day`` + ``minutes`` since midnight (Tashkent wall time of the schedule) as a naive UTC instant."""
    wall = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minutes)
    return business_wall_to_utc(wall)  # type: ignore[return-value]


def attendance_started_utc(att) -> Optional[datetime]:
    """``group_attendance.started_at`` (Tashkent) as naive UTC; ``None`` for a reconciled lesson (no start)."""
    return business_wall_to_utc(getattr(att, "started_at", None))


def attendance_completed_utc(att) -> Optional[datetime]:
    """``group_attendance.completed_at`` (Tashkent) as naive UTC; ``None`` while the lesson is not completed."""
    return business_wall_to_utc(getattr(att, "completed_at", None))


def attendance_duration_minutes(att) -> Optional[int]:
    """Real duration of a lesson in whole minutes, both ends from the Tashkent columns (never ``created_at``)."""
    started, completed = getattr(att, "started_at", None), getattr(att, "completed_at", None)
    if started is None or completed is None:
        return None
    return int((completed - started).total_seconds() // 60)


def add_days(day: date, days: int) -> date:
    return day + timedelta(days=days)


def month_window_start(today: date) -> date:
    """First day of the previous month: the lower bound of the "unclosed lessons" window (same rule as
    ``service.missed_lesson_window_start``, without importing the service)."""
    first_of_month = today.replace(day=1)
    return (first_of_month - timedelta(days=1)).replace(day=1)
