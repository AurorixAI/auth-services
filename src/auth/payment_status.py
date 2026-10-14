"""
ALGO - Payment status of a membership (N13 P2).  Pure functions, no database.

A student's membership is "paid" in one of two period modes (a centre setting
that is written into every payment):

* ``calendar`` - a payment covers ``valid_from .. valid_to`` (inclusive);
* ``lessons``  - a payment covers ``lessons_count`` counted lessons held on or
  after ``valid_from``.

The mode of a membership is the mode of its **latest** live payment (order:
``paid_on``, ``created_at``); the older payments of the other mode are
ignored, so changing the centre setting never breaks old records.

Lessons mode, FIFO.  Counted lessons (completed, student in the roster
snapshot, mark present/late/absent or an excused mark without a confirmed
excuse) are ordered by date and handed out to the live lessons payments in the
order ``valid_from, paid_on, created_at``.  A payment takes up to
``lessons_count`` not yet taken lessons dated on or after its ``valid_from``.
``left = max(0, sum(lessons_count) - taken)``.

"Today" is the business date (``BUSINESS_TIMEZONE``, Asia/Tashkent by default),
never the server's or the browser's local date.
"""
from __future__ import annotations

import calendar as _calendar
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

CALENDAR = "calendar"
LESSONS = "lessons"

NONE = "none"
FREE = "free"
EXPIRED = "expired"
ENDS_TODAY = "ends_today"
EXPIRING = "expiring"
ACTIVE = "active"

STATUSES = (NONE, FREE, EXPIRED, ENDS_TODAY, EXPIRING, ACTIVE)

# Tab buckets of the payments page.
BUCKET_DUE = "due"
BUCKET_EXPIRING = "expiring"
BUCKET_NONE = "none"
BUCKET_ACTIVE = "active"
BUCKET_ALL = "all"
BUCKETS = (BUCKET_DUE, BUCKET_EXPIRING, BUCKET_NONE, BUCKET_ACTIVE, BUCKET_ALL)

DEFAULT_EXPIRING_DAYS = 3
DEFAULT_EXPIRING_LESSONS = 3
DEFAULT_LESSONS_PER_PAYMENT = 12

# Hard limits (422) and soft thresholds (warnings) for entering a payment.
MAX_PERIOD_DAYS = 400
WARN_PERIOD_DAYS = 62
WARN_PAID_AGO_DAYS = 60
MAX_PAID_AGO_YEARS = 3


@dataclass(frozen=True)
class PaymentRecord:
    """The fields of a *live* (not voided) payment that matter for the status."""

    paid_on: date
    valid_from: date
    valid_to: Optional[date] = None
    lessons_count: Optional[int] = None
    created_at: datetime = datetime.min
    id: object = None

    @property
    def mode(self) -> str:
        return LESSONS if self.lessons_count is not None else CALENDAR


@dataclass(frozen=True)
class StatusSettings:
    expiring_days: int = DEFAULT_EXPIRING_DAYS
    expiring_lessons: int = DEFAULT_EXPIRING_LESSONS


@dataclass(frozen=True)
class StatusResult:
    status: str = NONE
    period_mode: Optional[str] = None
    paid_until: Optional[date] = None
    days_overdue: Optional[int] = None
    days_left: Optional[int] = None
    lessons_total: Optional[int] = None
    lessons_used: Optional[int] = None
    lessons_left: Optional[int] = None
    last_payment_id: object = None


@dataclass
class PaymentWarning:
    code: str
    message: str


WARNING_MESSAGES = {
    "overlaps_previous": "Период пересекается с предыдущей оплатой",
    "gap_before": "Между прошлой оплатой и этим периодом есть пропуск",
    "period_over_62_days": "Период длиннее 62 дней",
    "paid_over_60_days_ago": "Дата оплаты старше 60 дней",
}


# ---------------------------------------------------------------- dates ----


def business_date(moment: datetime, tz=None) -> date:
    """Calendar date of ``moment`` in the business timezone.

    A naive ``moment`` is taken as UTC.  ``tz`` defaults to the service's
    ``BUSINESS_TIMEZONE`` (Asia/Tashkent, UTC+5, no DST).
    """
    if tz is None:
        from .service import BUSINESS_TIMEZONE  # lazy: service is heavy

        tz = BUSINESS_TIMEZONE
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()


def business_today(tz=None) -> date:
    return business_date(datetime.now(timezone.utc), tz)


def add_month_minus_day(start: date) -> date:
    """``start`` + 1 calendar month - 1 day (05.10 -> 04.11; 31.01 -> 27.02/28.02)."""
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    day = min(start.day, _calendar.monthrange(year, month)[1])
    return date(year, month, day) - timedelta(days=1)


# --------------------------------------------------------------- status ----


def latest_payment(payments: Sequence[PaymentRecord]) -> Optional[PaymentRecord]:
    """The latest live payment: by ``paid_on``, then ``created_at``."""
    if not payments:
        return None
    return max(payments, key=lambda p: (p.paid_on, p.created_at))


def allocate_lessons(
    payments: Sequence[PaymentRecord], lesson_dates: Iterable[date]
) -> tuple[int, int]:
    """FIFO hand-out of counted lessons to lessons payments -> ``(total, used)``."""
    lessons_payments = sorted(
        (p for p in payments if p.lessons_count is not None),
        key=lambda p: (p.valid_from, p.paid_on, p.created_at),
    )
    pool = sorted(lesson_dates)
    total = used = 0
    for payment in lessons_payments:
        total += payment.lessons_count
        start = bisect_left(pool, payment.valid_from)
        taken = pool[start : start + payment.lessons_count]
        used += len(taken)
        del pool[start : start + len(taken)]
    return total, used


def compute_status(
    *,
    today: date,
    monthly_fee: Optional[int],
    payments: Sequence[PaymentRecord],
    lesson_dates: Iterable[date] = (),
    settings: StatusSettings = StatusSettings(),
) -> StatusResult:
    """Status of one membership.  ``payments`` must contain live payments only."""
    last = latest_payment(payments)
    if last is None:
        return StatusResult(status=FREE if monthly_fee == 0 else NONE)

    if last.mode == CALENDAR:
        paid_until = max(p.valid_to for p in payments if p.valid_to is not None)
        delta = (paid_until - today).days
        if delta < 0:
            status, overdue, left = EXPIRED, -delta, None
        elif delta == 0:
            status, overdue, left = ENDS_TODAY, None, 0
        elif delta <= settings.expiring_days:
            status, overdue, left = EXPIRING, None, delta
        else:
            status, overdue, left = ACTIVE, None, delta
        return StatusResult(
            status=status,
            period_mode=CALENDAR,
            paid_until=paid_until,
            days_overdue=overdue,
            days_left=left,
            last_payment_id=last.id,
        )

    total, used = allocate_lessons(payments, lesson_dates)
    remaining = max(0, total - used)
    if remaining == 0:
        status = EXPIRED
    elif remaining <= settings.expiring_lessons:
        status = EXPIRING
    else:
        status = ACTIVE
    return StatusResult(
        status=status,
        period_mode=LESSONS,
        lessons_total=total,
        lessons_used=used,
        lessons_left=remaining,
        last_payment_id=last.id,
    )


def bucket_of(status: str) -> str:
    """Tab of the payments page for a status ("Требуют оплаты" = expired + ends today)."""
    if status in (EXPIRED, ENDS_TODAY):
        return BUCKET_DUE
    if status == EXPIRING:
        return BUCKET_EXPIRING
    if status == NONE:
        return BUCKET_NONE
    return BUCKET_ACTIVE  # active + free


def in_bucket(status: str, bucket: str) -> bool:
    return bucket == BUCKET_ALL or bucket_of(status) == bucket


# ------------------------------------------------- entering a payment -----


def default_valid_from(mode: str, paid_on: date, previous_paid_until: Optional[date]) -> date:
    """Calendar: the day after the previous "paid until" if not before ``paid_on``."""
    if mode == CALENDAR and previous_paid_until is not None:
        following = previous_paid_until + timedelta(days=1)
        if following >= paid_on:
            return following
    return paid_on


def validation_errors(
    *,
    today: date,
    paid_on: date,
    valid_from: date,
    valid_to: Optional[date],
) -> list[str]:
    """Hard (422) rules, in Russian.  Empty list = acceptable."""
    errors: list[str] = []
    if paid_on > today:
        errors.append("Дата оплаты не может быть в будущем")
    oldest = _years_ago(today, MAX_PAID_AGO_YEARS)
    if paid_on < oldest:
        errors.append("Дата оплаты старше 3 лет")
    if valid_to is not None:
        if valid_to < valid_from:
            errors.append("Дата «оплачено до» раньше начала периода")
        elif (valid_to - valid_from).days > MAX_PERIOD_DAYS:
            errors.append("Период не может быть длиннее 400 дней")
    return errors


def _years_ago(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


def payment_warnings(
    *,
    today: date,
    paid_on: date,
    valid_from: date,
    valid_to: Optional[date],
    existing: Sequence[PaymentRecord],
) -> list[PaymentWarning]:
    """Soft rules: the payment is saved anyway.  ``existing`` = other live payments."""
    codes: list[str] = []
    if valid_to is not None:
        calendar_payments = [p for p in existing if p.valid_to is not None]
        if any(p.valid_from <= valid_to and p.valid_to >= valid_from for p in calendar_payments):
            codes.append("overlaps_previous")
        elif calendar_payments:
            before = [p.valid_to for p in calendar_payments if p.valid_to < valid_from]
            if before and max(before) < valid_from - timedelta(days=1):
                codes.append("gap_before")
        if (valid_to - valid_from).days + 1 > WARN_PERIOD_DAYS:
            codes.append("period_over_62_days")
    if (today - paid_on).days > WARN_PAID_AGO_DAYS:
        codes.append("paid_over_60_days_ago")
    return [PaymentWarning(code, WARNING_MESSAGES[code]) for code in codes]


# ------------------------------------------------------ counted lessons ----

COUNTED_MARKS = ("present", "late", "absent", "excused")


def lesson_counts(*, mark: Optional[str], excuse_status: Optional[str]) -> bool:
    """Whether a mark of a *completed* lesson spends a lessons payment (rule 4.3).

    present / late / absent count; an excused mark counts unless its excuse is
    confirmed (pending, rejected and a missing excuse row still count).
    """
    if mark not in COUNTED_MARKS:
        return False
    return not (mark == "excused" and excuse_status == "confirmed")
