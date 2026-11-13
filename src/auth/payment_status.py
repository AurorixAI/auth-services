"""
ALGO - Payment records of a membership (N13 P2).  Pure functions, no database.

A student's membership is "paid" in one of two period modes (a centre setting
that is written into every payment):

* ``calendar`` - a payment covers ``valid_from .. valid_to`` (inclusive);
* ``lessons``  - a payment covers ``lessons_count`` counted lessons held on or
  after ``valid_from``.

The mode of a membership is the mode of its **latest** live payment (order:
``paid_on``, ``created_at``; ``billing_mode``); the older payments of the other mode are
ignored, so changing the centre setting never breaks old records.  The status and the debt
themselves are computed by ``billing_ledger``.

Lessons mode, FIFO (``allocate_lessons_detail``).  Counted lessons (completed, student in the
roster snapshot, mark present/late/absent or an excused mark without a confirmed excuse -
``lesson_counts``) are ordered by date and handed out to the live lessons payments in the
order ``valid_from, paid_on, created_at``.  A payment takes up to ``lessons_count`` not yet
taken lessons dated on or after its ``valid_from``.

Carried-out payments (N13 P2.1, "the payment moves with the student").  A transfer
copies a payment in force into the new group and stamps the original with
``carried_out_at`` = the INSTANT of the transfer (naive UTC, the same moment as the old
membership's ``left_at``).  T is that instant's business date.  From then on the original
covers, in its own membership, only what happened while the student was still in the group:

* calendar: it ends on ``min(valid_to, T)`` - day T included, because a lesson of the old
  group that started before the transfer is on that day (``PaymentRecord.end``).  Lessons
  after the transfer are not in the old group's roster, so the day is never counted in
  both groups.  A payment that moved before its period began (``valid_from > T``) covers
  nothing and is ignored (``payments_in_force``);
* lessons: it takes only counted lessons dated up to and including day T (Tashkent); what is
  left of the package travelled with the student, so the original never "resurrects" when
  the student comes back.

The same rule feeds the excuse deadline and (P3) payroll.  Payroll must therefore read the
period of a calendar payment through ``PaymentRecord.end`` (never the raw ``valid_to``) and
the lessons of a package through ``allocate_lessons_detail``; the carry-over rows (amount 0,
``carried_from_payment_id``) are ordinary live payments of the new group and cover it from T.
A lesson belongs to one group, so a lesson is paid by exactly one membership.  The money for
payroll is the effective PRICE of the student on the day of the lesson (``pricing.py``), never a payment ``amount``.

N13 P2.2 - one payment day for the centre (``billing_anchor = fixed_day``, ``billing_day`` 1..28):
the centre's periods start on ``billing_day`` of every month (day 1: 01.10-31.10, day 10:
10.10-09.11); ``billing_period_start`` / ``billing_period_end`` give the period of a day.

"Today" is the business date (``BUSINESS_TIMEZONE``, Asia/Tashkent by default),
never the server's or the browser's local date.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

from . import business_clock as bc

CALENDAR = "calendar"
LESSONS = "lessons"

# Where a calendar period starts (N13 P2.2): the payment date, or one centre-wide "payment day".
PAYMENT_DATE = "payment_date"
FIXED_DAY = "fixed_day"
MIN_BILLING_DAY = 1
MAX_BILLING_DAY = 28  # no 29..31: the period then never depends on the length of a month

NONE = "none"
FREE = "free"
EXPIRED = "expired"
ENDS_TODAY = "ends_today"
EXPIRING = "expiring"
ACTIVE = "active"
PAUSED = "paused"  # LOGIC-A: today lies inside a pause of the membership

STATUSES = (NONE, FREE, EXPIRED, ENDS_TODAY, EXPIRING, ACTIVE, PAUSED)

# Tab buckets of the payments page.
BUCKET_DUE = "due"
BUCKET_EXPIRING = "expiring"
BUCKET_NONE = "none"
BUCKET_ACTIVE = "active"
BUCKET_ALL = "all"
BUCKET_PAUSED = "paused"  # not a tab of its own: in_bucket() is true for ``all`` only

DEFAULT_EXPIRING_DAYS = 3
DEFAULT_EXPIRING_LESSONS = 3
DEFAULT_LESSONS_PER_PAYMENT = 12

# Soft thresholds (warnings) for entering a payment.
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
    # The instant (naive UTC) the payment moved to another group with the student; it covers
    # only what happened while the student was still in this group (see the module docstring).
    carried_out_at: Optional[datetime] = None
    # For a lessons payment that moved away: what is left in the live package at the end of its chain
    # and that package's size (the original ``lessons_count``), read by the excuse deadline.
    moved_left: Optional[int] = None
    moved_size: Optional[int] = None

    @property
    def mode(self) -> str:
        return LESSONS if self.lessons_count is not None else CALENDAR

    @property
    def carried_out_day(self) -> Optional[date]:
        """Business date (Tashkent) of the transfer instant."""
        return business_date(self.carried_out_at) if self.carried_out_at is not None else None

    @property
    def end(self) -> Optional[date]:
        """Last covered day of a calendar payment (``valid_to``, cut at the transfer day, included)."""
        return effective_valid_to(self.valid_to, self.carried_out_at)

    @property
    def covers_nothing(self) -> bool:
        """A calendar payment that was moved away before its period began."""
        end = self.end
        return end is not None and end < self.valid_from


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
    paused_until: Optional[date] = None  # LOGIC-A: the last day of the pause when ``status == "paused"``


WARNING_MESSAGES = {
    "overlaps_previous": "Период пересекается с предыдущей оплатой",
    "gap_before": "Между прошлой оплатой и этим периодом есть пропуск",
    "period_over_62_days": "Период длиннее 62 дней",
    "paid_over_60_days_ago": "Дата оплаты старше 60 дней",
    "short_period_full_amount": "Короткий период при полной сумме — проверьте даты",
}


# ---------------------------------------------------------------- dates ----


def business_date(moment: datetime, tz=None) -> date:
    """Calendar date of ``moment`` in the business timezone.

    A naive ``moment`` is taken as UTC.  ``tz`` defaults to the service's
    ``BUSINESS_TIMEZONE`` (Asia/Tashkent, UTC+5, no DST).
    """
    if tz is None:
        tz = bc.BUSINESS_TIMEZONE
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()


def business_today(tz=None) -> date:
    """MN-22: today of the business clock (``set_clock`` of the tests reaches it); a given ``tz`` reads the real clock in that zone."""
    if tz is None:
        return bc.business_today()
    return business_date(datetime.now(timezone.utc), tz)


# --------------------------------------------------------------- status ----


def effective_valid_to(valid_to: Optional[date], carried_out_at: Optional[datetime]) -> Optional[date]:
    """End of a calendar payment in its own membership: cut at the day it moved away (included)."""
    if valid_to is None or carried_out_at is None:
        return valid_to
    return min(valid_to, business_date(carried_out_at))


def latest_payment(payments: Sequence[PaymentRecord]) -> Optional[PaymentRecord]:
    """The latest live payment: by ``paid_on``, then ``created_at``."""
    if not payments:
        return None
    return max(payments, key=lambda p: (p.paid_on, p.created_at))


def payments_in_force(payments: Sequence[PaymentRecord]) -> list[PaymentRecord]:
    """The live payments the status reads: a calendar payment that moved away before its period began covers nothing."""
    return [p for p in payments if not p.covers_nothing]


# How the mode of a membership is named outside the status maths (the excuse dialog): a lessons payment is a "package".
BILLING_CALENDAR = CALENDAR
BILLING_PACKAGE = "package"


def billing_mode(payments: Sequence[PaymentRecord]) -> Optional[str]:
    """How a membership is billed: ``calendar`` or ``package`` - the mode of its LATEST live payment in force (the centre
    setting only decides what the NEXT payment will be); ``None`` - no live payment, so nothing is written off a package.  ``payments`` must contain live payments only."""
    last = latest_payment(payments_in_force(payments))
    if last is None:
        return None
    return BILLING_PACKAGE if last.mode == LESSONS else BILLING_CALENDAR


def allocate_lessons_detail(
    payments: Sequence[PaymentRecord], lesson_dates: Iterable[date]
) -> list[tuple[PaymentRecord, list[date]]]:
    """FIFO hand-out of counted lessons: ``[(payment, dates it took), ...]`` in hand-out order."""
    lessons_payments = sorted(
        (p for p in payments if p.lessons_count is not None),
        key=lambda p: (p.valid_from, p.paid_on, p.created_at),
    )
    pool = sorted(lesson_dates)
    return [(payment, _take(pool, payment)) for payment in lessons_payments]


def _take(pool: list[date], payment: PaymentRecord) -> list[date]:
    """One FIFO step: remove from the sorted ``pool`` the lessons ``payment`` covers and return them."""
    start = bisect_left(pool, payment.valid_from)
    stop = start + payment.lessons_count
    if payment.carried_out_at is not None:  # only lessons up to the day it moved away (that day included)
        stop = min(stop, bisect_right(pool, payment.carried_out_day))
    taken = pool[start : max(start, stop)]
    del pool[start : start + len(taken)]
    return taken


def bucket_of(status: str) -> str:
    """Tab of the payments page for a status ("Требуют оплаты" = expired + ends today)."""
    if status in (EXPIRED, ENDS_TODAY):
        return BUCKET_DUE
    if status == EXPIRING:
        return BUCKET_EXPIRING
    if status == NONE:
        return BUCKET_NONE
    if status == PAUSED:
        return BUCKET_PAUSED  # only the "all" tab shows a paused student
    return BUCKET_ACTIVE  # active + free


def in_bucket(status: str, bucket: str) -> bool:
    return bucket == BUCKET_ALL or bucket_of(status) == bucket


# ------------------------------------- one payment day for the centre -----
#
# "Единый день оплаты" (N13 P2.2): the centre has periods that start on ``billing_day`` of every
# month: day 1 -> 01.10-31.10, day 10 -> 10.10-09.11.  ``billing_day`` is 1..28, so a period never
# depends on the length of a month.


def _check_billing_day(billing_day: int) -> None:
    if not MIN_BILLING_DAY <= billing_day <= MAX_BILLING_DAY:
        raise ValueError(f"billing_day must be {MIN_BILLING_DAY}..{MAX_BILLING_DAY}")


def billing_period_start(day: date, billing_day: int) -> date:
    """Start of the centre's period that contains ``day`` (``billing_day`` of this or of the previous month).

    Raises ``OverflowError`` when the result would not be a valid date.
    """
    _check_billing_day(billing_day)
    if day.day >= billing_day:
        return day.replace(day=billing_day)
    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    try:
        return date(year, month, billing_day)
    except ValueError as exc:  # year 0
        raise OverflowError("date out of range") from exc


def billing_period_end(day: date, billing_day: int) -> date:
    """Last day of the centre's period that contains ``day``: the next ``billing_day`` minus one day.

    With ``billing_day`` 1 that is the last day of the month; passing the period's start gives its end.
    Raises ``OverflowError`` when the result would not be a valid date.
    """
    start = billing_period_start(day, billing_day)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    try:
        return date(year, month, billing_day) - timedelta(days=1)
    except ValueError as exc:  # year 10000
        raise OverflowError("date out of range") from exc


# ------------------------------------------------------ rounding, dates ----


def round_1000(numerator: int, denominator: int = 1) -> int:
    """``numerator / denominator`` rounded to whole thousands, a half goes up (integer maths)."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return ((2 * numerator + 1000 * denominator) // (2000 * denominator)) * 1000


def prorated(price: int, numerator: int, denominator: int) -> int:
    """ONE rounding of the money a student owes for a part of a period (Money-1 G-7): ``price x numerator / denominator`` in whole thousands, a half goes
    up, never more than the price itself (a price that is not a whole number of thousands must not make a part dearer than the whole); the whole price for
    a whole period and 0 for no part.  Every partial charge of the ledger - days, lessons of a month, lessons of a package - goes through it."""
    if numerator >= denominator:
        return price
    return min(price, round_1000(price * numerator, denominator)) if numerator > 0 else 0


def package_share(price: int, lessons: int, size: int) -> int:
    """Money-1 G-7b: what ``lessons`` held lessons of a package of ``size`` lessons cost - EXACT where the price of one lesson is a whole number (``price`` divides by ``size``: every lesson costs
    the same, the pay of the teacher for each is the same), otherwise ``prorated`` (whole thousands).  The whole package is the price; the one place of this rule."""
    if size > 0 and 0 < lessons < size and price % size == 0:
        return price // size * lessons
    return prorated(price, lessons, size)


def _years_ago(day: date, years: int) -> date:
    if day.year - years < date.min.year:
        return date.min
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


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
