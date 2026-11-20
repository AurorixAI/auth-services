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

TIME-TESTS (the controllable clock of the end-to-end stand).  The stand shifts the business time by an offset in seconds kept in Redis
(``algo:test:clock_offset_seconds``, shared with exam-service) and set through ``/api/v1/auth/dev/clock`` (``dev_router``).  The mode is decided ONCE,
at import: ``APP_ENV=development`` AND ``ALGO_TEST_CLOCK=1`` (``TEST_CLOCK_MODE``); ``ALGO_TEST_CLOCK`` with any value on another ``APP_ENV`` refuses
to import this module (``startup_guard.check_test_clock_allowed``).  Outside the mode the offset is 0 and Redis is never touched.  The clock only moves
forward (``set_offset`` refuses a smaller offset); a reset is a fresh stand.
"""
from __future__ import annotations

import math
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from src.core.startup_guard import TEST_CLOCK_REDIS_VARIABLE, TEST_CLOCK_VARIABLE, check_test_clock_allowed, is_dev_env

BUSINESS_TIMEZONE = ZoneInfo(os.getenv("BUSINESS_TIMEZONE", "Asia/Tashkent"))

check_test_clock_allowed()  # ALGO_TEST_CLOCK outside APP_ENV=development: the service does not start
TEST_CLOCK_MODE: bool = is_dev_env() and (os.environ.get(TEST_CLOCK_VARIABLE) or "").strip() == "1"  # decided once; the only gate of the offset code

OFFSET_KEY = "algo:test:clock_offset_seconds"
OFFSET_CACHE_SECONDS = 0.2
MAX_OFFSET_SECONDS = 10 * 366 * 86400  # a sanity bound for a typed value

_clock: Optional[Callable[[], datetime]] = None

_offset_lock = threading.Lock()
_offset_cache: tuple[int, float] = (0, 0.0)  # (seconds, valid until a ``time.monotonic()`` value)
_redis_client = None


class TestClockError(RuntimeError):
    """The test clock is on but cannot be read or written (no Redis address, Redis down, a junk value): loud, never a silent zero."""

    __test__ = False  # not a pytest class


class ClockMovedBackError(ValueError):
    """The stand clock moves forward only."""


def _redis():
    """The client of the clock's Redis.  Created lazily, ONLY under ``_offset_lock`` (every caller - the read and the write of the offset - holds it)."""
    global _redis_client
    if _redis_client is None:
        url = (os.environ.get(TEST_CLOCK_REDIS_VARIABLE) or "").strip()
        if not url:
            raise TestClockError(f"{TEST_CLOCK_VARIABLE}=1, но не задан {TEST_CLOCK_REDIS_VARIABLE}: нет адреса Redis со смещением часов")
        import redis

        _redis_client = redis.Redis.from_url(url, socket_connect_timeout=1.0, socket_timeout=1.0, decode_responses=True)
    return _redis_client


def _read_offset() -> int:
    try:
        raw = _redis().get(OFFSET_KEY)
    except TestClockError:
        raise
    except Exception as exc:  # noqa: BLE001 - any Redis failure in the test mode must stop the request, not become "no shift"
        raise TestClockError(f"управляемые часы: Redis недоступен ({type(exc).__name__}: {exc})") from exc
    if raw in (None, ""):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise TestClockError(f"управляемые часы: в {OFFSET_KEY} не целое число ({raw!r})") from exc


def current_offset_seconds() -> int:
    """The shift of the stand clock in seconds.  Outside the test mode: 0, at once, with no Redis call and no lock."""
    global _offset_cache
    if not TEST_CLOCK_MODE:
        return 0
    with _offset_lock:
        value, valid_until = _offset_cache
        now = time.monotonic()
        if now < valid_until:
            return value
        value = _read_offset()
        _offset_cache = (value, now + OFFSET_CACHE_SECONDS)
        return value


def set_offset(seconds: int) -> int:
    """Test mode only: move the stand clock to ``seconds`` of shift (never back); the local cache is updated at once.  Returns the new offset."""
    global _offset_cache
    if not TEST_CLOCK_MODE:
        raise TestClockError("управляемые часы выключены (нужны APP_ENV=development и ALGO_TEST_CLOCK=1)")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or abs(seconds) > MAX_OFFSET_SECONDS:
        raise ValueError("смещение должно быть целым числом секунд в разумных пределах")
    with _offset_lock:
        current = _read_offset()  # fresh, not the cache: the other service (exam) may share the key
        if seconds < current:
            raise ClockMovedBackError("часы стенда идут только вперёд от настоящего времени; нужное время в прошлом - свежий стенд и дата впереди")
        try:
            _redis().set(OFFSET_KEY, str(seconds))
        except TestClockError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TestClockError(f"управляемые часы: Redis недоступен ({type(exc).__name__}: {exc})") from exc
        _offset_cache = (seconds, time.monotonic() + OFFSET_CACHE_SECONDS)
        return seconds


def offset_for_business_now(target_naive: datetime) -> int:
    """The shift that makes the business time ``target_naive`` (a naive Tashkent wall time) right now."""
    target = target_naive.replace(tzinfo=BUSINESS_TIMEZONE)
    return math.ceil((target - datetime.now(timezone.utc)).total_seconds())   # rounded UP: the stand time is the target or a fraction of a second after it, never before


def real_utc_now() -> datetime:
    """The real clock, aware UTC (for the dev clock endpoint only)."""
    return datetime.now(timezone.utc)


def set_clock(clock: Optional[Callable[[], datetime]]) -> None:
    """Install (or, with ``None``, remove) a replacement clock; the callable returns an aware datetime."""
    global _clock
    _clock = clock


def business_now() -> datetime:
    """Now, aware, in the business timezone."""
    if _clock is not None:
        moment = _clock()
    else:
        moment = datetime.now(timezone.utc)
        offset = current_offset_seconds()  # 0 and no Redis call outside the test mode
        if offset:
            moment += timedelta(seconds=offset)
    if moment.tzinfo is None:
        raise ValueError("the business clock must return an aware datetime")
    return moment.astimezone(BUSINESS_TIMEZONE)


class ActionMoments:
    """The journal lines of ONE action: ``moment + 1 us``, ``+ 2 us``, ... in the order of the calls - the journal of decisions sorts by ``created_at``, so
    the lines keep the order they were written in (before the stand clock they got the clock of their own insert).  Only the JOURNAL lines draw from it: the
    data rows (and whatever is joined by equality or ``>= since``) stay at the exact ``moment``."""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment
        self._steps = 0

    def next(self) -> datetime:
        self._steps += 1
        return self._moment + timedelta(microseconds=self._steps)


def action_moments(moment: datetime) -> ActionMoments:
    return ActionMoments(moment)


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


def as_utc(moment: Optional[datetime]) -> Optional[datetime]:
    """An AWARE UTC moment from a stored one (SV-21): a naive value is UTC already, an aware one is converted; ``None`` stays ``None``.  The one way to compare a
    stored ``timestamp`` with ``datetime.now(timezone.utc)``."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


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


def month_window_start(today: date) -> date:
    """First day of the previous month: the lower bound of the "unclosed lessons" window (same rule as
    ``service.missed_lesson_window_start``, without importing the service)."""
    first_of_month = today.replace(day=1)
    return (first_of_month - timedelta(days=1)).replace(day=1)


MONTH_FORMAT_MESSAGE = "month должен иметь формат YYYY-MM"


def month_bounds(month: str) -> tuple[date, date]:
    """First and last day of ``month`` (``YYYY-MM``); ``ValueError(MONTH_FORMAT_MESSAGE)`` for anything else (``2026-1``, ``2026-13``, a date)."""
    try:
        first = datetime.strptime(month + "-01", "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(MONTH_FORMAT_MESSAGE) from exc
    if first.strftime("%Y-%m") != month:
        raise ValueError(MONTH_FORMAT_MESSAGE)
    following = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
    return first, following - timedelta(days=1)
