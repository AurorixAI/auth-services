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

Carried-out payments (N13 P2.1, "the payment moves with the student").  A transfer
copies a payment in force into the new group and stamps the original with
``carried_out_at`` = the INSTANT of the transfer (naive UTC, the same moment as the old
membership's ``left_at``).  T is that instant's business date.  From then on the original
covers, in its own membership, only what happened while the student was still in the group:

* calendar: it ends on ``min(valid_to, T)`` - day T included, because a lesson of the old
  group that started before the transfer is on that day (``PaymentRecord.end``).  Lessons
  after the transfer are not in the old group's roster, so the day is never counted in
  both groups.  A payment that moved before its period began (``valid_from > T``) covers
  nothing and is ignored by the status;
* lessons: it takes only counted lessons that STARTED before the instant (lesson date +
  scheduled start, Tashkent).  Lessons on other days compare by date; of the lessons on day T
  the service tells how many started at or after the instant (``carried_out_late``) and they
  are not taken.  Its capacity is what it took (``lesson_capacity``): the unspent rest
  travelled with the student, so the original never "resurrects" when the student comes back.

Conservation of a lessons package (money).  A carry-over does NOT freeze the remainder that
travelled: its capacity is derived live along the chain, ``capacity(C) = capacity(source) -
lessons the source took before it moved`` (``resolve_lesson_chains``, same counting rules: a
confirmed excuse consumes nothing, a cancelled lesson does not exist, a reconciled lesson
does).  The ``lessons_count`` stored on a carry-over is only the remainder at transfer time
(informational).  So confirming a pre-transfer excuse - or reconciling a pre-transfer lesson -
AFTER the transfer gives a lesson to - or takes one from - the student's current package, and
the lessons of the whole chain always add up to the original ``lessons_count``.  The excuse
deadline of a lesson that a moved package took follows the CURRENT remainder of the chain's live
package (``PaymentRecord.moved_left``), not "nothing left".

The same rule feeds the excuse deadline and (P3) payroll.  Payroll must therefore read the
period of a calendar payment through ``PaymentRecord.end`` (never the raw ``valid_to``) and
the lessons of a package through ``allocate_lessons_detail``; the carry-over rows (amount 0,
``carried_from_payment_id``) are ordinary live payments of the new group and cover it from T.
A lesson belongs to one group, so a lesson is paid by exactly one membership.  The money for
payroll is the effective PRICE of the student on the day of the lesson (``pricing.py``), never a payment ``amount``.

N13 P2.2 - one payment day for the centre (``billing_anchor = fixed_day``, ``billing_day`` 1..28).
The centre's periods then start on ``billing_day`` of every month (day 1: 01.10-31.10, day 10:
10.10-09.11).  ``default_fixed_day_period`` suggests the dates of a payment, ``is_partial_period`` tells
whether a payment covers whole periods, ``price_fixed_day`` prices an incomplete period by the lessons
of the group (the amount hint, ``PaymentService.quote``).  Nothing here changes how a payment that is
already saved is read: the status maths above knows only ``valid_from`` / ``valid_to``.

"Today" is the business date (``BUSINESS_TIMEZONE``, Asia/Tashkent by default),
never the server's or the browser's local date.
"""
from __future__ import annotations

import calendar as _calendar
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

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
BUCKETS = (BUCKET_DUE, BUCKET_EXPIRING, BUCKET_NONE, BUCKET_ACTIVE, BUCKET_ALL)

DEFAULT_EXPIRING_DAYS = 3
DEFAULT_EXPIRING_LESSONS = 3
DEFAULT_LESSONS_PER_PAYMENT = 12

# Hard limits (422) and soft thresholds (warnings) for entering a payment.
MAX_PERIOD_DAYS = 400
WARN_PERIOD_DAYS = 62
WARN_PAID_AGO_DAYS = 60
MAX_PAID_AGO_YEARS = 3
SHORT_PERIOD_DAYS = 7  # a calendar period shorter than this at the full monthly fee is suspicious


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
    # Counted lessons dated on the business day of ``carried_out_at`` that started at or after
    # that instant (filled in by the service, which knows the schedule); 0 = none / unknown.
    carried_out_late: int = 0
    # Chain bookkeeping (lessons packages): the membership the payment lives in, the payment it was
    # carried over from, and - for a payment that moved away - what is left in the live package at the
    # end of its chain and that package's size (the original ``lessons_count``).
    membership_id: object = None
    carried_from_id: object = None
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
    paused_until: Optional[date] = None  # LOGIC-A: the last day of the pause when ``status == "paused"``


@dataclass
class PaymentWarning:
    code: str
    message: str


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
        from .service import BUSINESS_TIMEZONE  # lazy: service is heavy

        tz = BUSINESS_TIMEZONE
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()


def business_today(tz=None) -> date:
    return business_date(datetime.now(timezone.utc), tz)


def add_month_minus_day(start: date) -> date:
    """Default end of a calendar period: the day before ``start``'s day in the next month.

    ``min(day - 1, last day of the next month)`` of the month after ``start``:
    05.10 -> 04.11, 31.01 -> 28.02 (29.02 in a leap year), 31.03 -> 30.04,
    30.11 -> 29.12.  The 1st has no day before it in the next month, so the
    period ends on the last day of the *same* month: 01.10 -> 31.10.

    Raises ``OverflowError`` when the result would not be a valid date.
    """
    if start.day == 1:
        return start.replace(day=_calendar.monthrange(start.year, start.month)[1])
    try:
        year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
        last = _calendar.monthrange(year, month)[1]
        return date(year, month, min(start.day - 1, last))
    except ValueError as exc:  # year 10000
        raise OverflowError("date out of range") from exc


# --------------------------------------------------------------- status ----


def effective_valid_to(valid_to: Optional[date], carried_out_at: Optional[datetime]) -> Optional[date]:
    """End of a calendar payment in its own membership: cut at the day it moved away (included)."""
    if valid_to is None or carried_out_at is None:
        return valid_to
    return min(valid_to, business_date(carried_out_at))


def lesson_capacity(payment: PaymentRecord, taken: int) -> int:
    """Lessons a lessons payment covers in its own membership.

    ``lessons_count``, unless the payment moved away with the student: then only the
    lessons it took before it left (the rest was carried over to the new group).
    """
    return taken if payment.carried_out_at is not None else payment.lessons_count


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
    """How a membership is billed: ``calendar`` or ``package`` - the mode of its LATEST live payment in force, the very
    payment ``compute_status`` reads (the centre setting only decides what the NEXT payment will be); ``None`` - no live
    payment, so nothing is written off a package.  ``payments`` must contain live payments only."""
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
    if payment.carried_out_at is not None:  # only lessons that started before the instant it moved away
        day = payment.carried_out_day
        first, after = bisect_left(pool, day), bisect_right(pool, day)
        # Of the lessons on that day the later ones come last; ``carried_out_late`` of them started after.
        stop = min(stop, first + max(0, (after - first) - payment.carried_out_late))
    taken = pool[start : max(start, stop)]
    del pool[start : start + len(taken)]
    return taken


def resolve_lesson_chains(
    payments: Sequence[PaymentRecord], pools: "dict[object, Iterable[date]]"
) -> tuple[dict, dict, dict]:
    """Live capacities of lessons packages that travel with a student -> ``(records, taken, left)``.

    ``payments``: every live lessons payment of the memberships involved (``membership_id`` and
    ``carried_from_id`` set); ``pools``: the counted lessons of each membership.  Payments are handed
    their lessons in the global order (valid_from, paid_on, created_at, id) - inside one membership it
    is the usual FIFO, and a carry-over always comes after its source, so the source's consumption is
    known when the carry-over's capacity is derived:
    ``capacity(carry-over) = capacity(source) - lessons the source took``.  A payment without a known
    source keeps its own ``lessons_count``.

    ``records[id]`` is the payment with the derived capacity as ``lessons_count`` (and ``moved_left`` /
    ``moved_size`` on one that moved away), ``taken[id]`` the dates it covers, ``left[id]`` its
    capacity minus what it took (for a payment that moved: what travelled on).
    """
    ordered = sorted(
        (p for p in payments if p.lessons_count is not None),
        key=lambda p: (p.valid_from, p.paid_on, p.created_at, str(p.id)),
    )
    state = {key: sorted(dates) for key, dates in pools.items()}
    records: dict = {}
    taken: dict = {}
    for payment in ordered:
        capacity = payment.lessons_count
        source = payment.carried_from_id
        if source is not None and source in records:
            capacity = max(0, records[source].lessons_count - len(taken[source]))
        derived = replace(payment, lessons_count=capacity)
        taken[payment.id] = _take(state.setdefault(payment.membership_id, []), derived)
        records[payment.id] = derived
    left = {pid: records[pid].lessons_count - len(got) for pid, got in taken.items()}

    children: dict = {}
    for payment in ordered:
        if payment.carried_from_id is not None:
            children.setdefault(payment.carried_from_id, []).append(payment.id)
    for payment in ordered:
        if payment.carried_out_at is None:
            continue
        leaf, seen = payment.id, set()
        while records[leaf].carried_out_at is not None and leaf not in seen:
            seen.add(leaf)
            kids = children.get(leaf)
            if not kids:
                leaf = None
                break
            leaf = max(kids, key=lambda pid: (records[pid].created_at, str(pid)))
        root, hops = payment.id, 0
        while records[root].carried_from_id in records and hops < 64:
            root, hops = records[root].carried_from_id, hops + 1
        records[payment.id] = replace(
            records[payment.id],
            moved_left=0 if leaf is None else left[leaf],
            moved_size=records[root].lessons_count,
        )
    return records, taken, left


def allocate_lessons(
    payments: Sequence[PaymentRecord], lesson_dates: Iterable[date]
) -> tuple[int, int]:
    """FIFO hand-out of counted lessons to lessons payments -> ``(total, used)``."""
    detail = allocate_lessons_detail(payments, lesson_dates)
    return (
        sum(lesson_capacity(payment, len(taken)) for payment, taken in detail),
        sum(len(taken) for _, taken in detail),
    )


def compute_status(
    *,
    today: date,
    monthly_fee: Optional[int],
    payments: Sequence[PaymentRecord],
    lesson_dates: Iterable[date] = (),
    settings: StatusSettings = StatusSettings(),
) -> StatusResult:
    """Status of one membership.  ``payments`` must contain live payments only."""
    # A calendar payment that moved away before its period began covers nothing at all.
    payments = payments_in_force(payments)
    last = latest_payment(payments)
    if last is None:
        return StatusResult(status=FREE if monthly_fee == 0 else NONE)

    if last.mode == CALENDAR:
        paid_until = max(p.end for p in payments if p.end is not None)
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
    if status == PAUSED:
        return BUCKET_PAUSED  # only the "all" tab shows a paused student
    return BUCKET_ACTIVE  # active + free


def in_bucket(status: str, bucket: str) -> bool:
    return bucket == BUCKET_ALL or bucket_of(status) == bucket


# ------------------------------------------------- entering a payment -----


def _debt_start(
    paid_on: date, previous_paid_until: Optional[date], membership_start: Optional[date]
) -> Optional[date]:
    """The day a payment starts when it first closes the debt: the day after the previous "paid until".

    Never earlier than ``membership_start`` (days outside the group are not charged: a student who left
    and came back starts on the day he returned).  ``None`` when there is no previous payment (the
    first payment is not reached back to ``joined_at``: imported students joined long before the centre
    started using ALGO) or when that day is so far in the past that ``valid_from_error`` would refuse it -
    a suggestion must degrade to the old behaviour, never raise.
    """
    if previous_paid_until is None:
        return None
    start = previous_paid_until + timedelta(days=1)
    if membership_start is not None:
        start = max(start, membership_start)
    if start < _years_ago(paid_on, MAX_PAID_AGO_YEARS):
        return None
    return start


def default_valid_from(
    mode: str,
    paid_on: date,
    previous_paid_until: Optional[date],
    membership_start: Optional[date] = None,
) -> date:
    """Suggested start of a payment (calendar month counted from it).

    Calendar: a student who is overdue first closes the debt, so with a previous payment the period starts
    the day after its "paid until" - NOT on the payment date: paid until 08.10, pays on 21.10 -> 09.10 (to
    08.11), and if the debt was longer than one period he is still overdue afterwards, which is correct.
    Paying ahead continues without a gap in the same way.  ``membership_start`` (the business date the
    student joined / came back to the group) is a floor.  The first payment starts on ``paid_on``.

    Lessons: ``paid_on`` as before - a package is counted in lessons, not days, so there is no debt
    period to close and no date of the package depends on the previous one.
    """
    if mode == CALENDAR:
        start = _debt_start(paid_on, previous_paid_until, membership_start)
        if start is not None:
            return start
    return paid_on


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


def billing_periods(valid_from: date, valid_to: date, billing_day: int) -> list[tuple[date, date]]:
    """Periods of the centre ``(start, end)`` that intersect ``valid_from .. valid_to`` (inclusive)."""
    if valid_to < valid_from:
        return []
    periods: list[tuple[date, date]] = []
    start = billing_period_start(valid_from, billing_day)
    while start <= valid_to:
        end = billing_period_end(start, billing_day)
        periods.append((start, end))
        if len(periods) > 200:  # a payment is at most 400 days: this is a bug, not a request
            raise ValueError("too many billing periods")
        start = end + timedelta(days=1)
    return periods


def is_partial_period(valid_from: date, valid_to: Optional[date], billing_day: int) -> bool:
    """True when the payment does not cover whole periods of the centre.

    ``valid_from`` is not the start of a period or ``valid_to`` is not the end of one.  A payment
    without ``valid_to`` (a lessons package) is never partial.
    """
    if valid_to is None:
        return False
    try:
        return (
            valid_from != billing_period_start(valid_from, billing_day)
            or valid_to != billing_period_end(valid_to, billing_day)
        )
    except OverflowError:
        return False


def default_fixed_day_period(
    paid_on: date,
    previous_paid_until: Optional[date],
    membership_start: Optional[date],
    billing_day: int,
) -> tuple[date, date]:
    """Suggested ``(valid_from, valid_to)`` of a payment when the centre has one payment day.

    With a previous payment a student who is overdue first closes the debt: ``valid_from`` is
    ``previous_paid_until + 1`` (paying two days late, or months late, continues without a gap; paying
    ahead as well), but not before ``membership_start`` - the business date the student joined the group
    (one who left and came back on the 15th starts on the 15th even though old payments exist).  There is
    NO clamp to the start of the centre's period of the payment date any more: the debt may begin in an
    older period, and the payment then covers the rest of that period (a partial period, priced by
    ``price_fixed_day``).  A previous payment that is too old for ``valid_from_error`` falls back to the
    first-payment rule.

    First payment (no previous one): the latest of
    * ``anchor`` - the start of the centre's period of the payment date;
    * ``membership_start`` (a student who came on the 15th pays 15.10-31.10; one who came in September
      and pays on 03.10 gets the whole of October); unknown: the payment date.
    ``valid_to`` is the end of the centre's period of ``valid_from``.
    """
    debt = _debt_start(paid_on, previous_paid_until, membership_start)
    if debt is not None:
        return debt, billing_period_end(debt, billing_day)
    start = billing_period_start(paid_on, billing_day)
    if membership_start is not None:
        start = max(start, membership_start)
    elif previous_paid_until is None:
        start = max(start, paid_on)
    return start, billing_period_end(start, billing_day)


# ------------------------------------------------------ amount hint (quote) ----

BASIS_FULL = "full"
BASIS_LESSONS = "lessons"
BASIS_DAYS = "days"


def round_1000(numerator: int, denominator: int = 1) -> int:
    """``numerator / denominator`` rounded to whole thousands, a half goes up (integer maths)."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return ((2 * numerator + 1000 * denominator) // (2000 * denominator)) * 1000


@dataclass(frozen=True)
class QuotePart:
    """One period of the centre inside a payment, with what it costs."""

    start: date  # the part of the period the payment covers
    end: date
    period_start: date  # the whole period of the centre it belongs to
    period_end: date
    lessons: Optional[int]  # lessons in the part / in the period; None for a full period or counting by days
    period_lessons: Optional[int]
    amount: int
    basis: str  # full | lessons | days

    @property
    def is_full(self) -> bool:
        return self.start == self.period_start and self.end == self.period_end


def price_fixed_day(
    valid_from: date,
    valid_to: date,
    billing_day: int,
    monthly_fee: int,
    lesson_days,
) -> list[QuotePart]:
    """Price ``valid_from .. valid_to`` period by period of the centre.

    A period covered completely costs ``monthly_fee``.  An incomplete one costs
    ``monthly_fee * lessons in the part / lessons in the whole period`` (rounded to 1 000, a half
    goes up); a period without lessons falls back to calendar days.  ``lesson_days(start, end)``
    returns the dates of the group's lessons in ``start .. end`` (cancelled ones already removed,
    one entry per lesson); it is called once per incomplete period.
    """
    parts: list[QuotePart] = []
    for period_start, period_end in billing_periods(valid_from, valid_to, billing_day):
        start, end = max(valid_from, period_start), min(valid_to, period_end)
        if start == period_start and end == period_end:
            parts.append(QuotePart(start, end, period_start, period_end, None, None, monthly_fee, BASIS_FULL))
            continue
        days = list(lesson_days(period_start, period_end))
        if days:
            in_part = sum(1 for d in days if start <= d <= end)
            parts.append(
                QuotePart(
                    start, end, period_start, period_end, in_part, len(days),
                    round_1000(monthly_fee * in_part, len(days)), BASIS_LESSONS,
                )
            )
        else:
            whole = (period_end - period_start).days + 1
            covered = (end - start).days + 1
            parts.append(
                QuotePart(
                    start, end, period_start, period_end, None, None,
                    round_1000(monthly_fee * covered, whole), BASIS_DAYS,
                )
            )
    return parts


def quote_basis(parts: Sequence[QuotePart]) -> str:
    """``lessons`` if any period was prorated by lessons, else ``days`` if by days, else ``full``."""
    bases = {part.basis for part in parts}
    return BASIS_LESSONS if BASIS_LESSONS in bases else BASIS_DAYS if BASIS_DAYS in bases else BASIS_FULL


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
    elif paid_on < _years_ago(today, MAX_PAID_AGO_YEARS):
        errors.append("Дата оплаты старше 3 лет")
    if errors:  # the remaining checks only make sense around a sane payment date
        return errors
    if valid_from < _years_ago(paid_on, MAX_PAID_AGO_YEARS):
        errors.append("Начало периода слишком далеко в прошлом")
    elif (valid_from - paid_on).days > MAX_PERIOD_DAYS:
        errors.append("Начало периода слишком далеко в будущем")
    elif valid_to is not None:
        if valid_to < valid_from:
            errors.append("Дата «оплачено до» раньше начала периода")
        elif (valid_to - valid_from).days > MAX_PERIOD_DAYS:
            errors.append("Период не может быть длиннее 400 дней")
    return errors


def valid_from_error(paid_on: date, valid_from: date) -> Optional[str]:
    """The ``valid_from`` bound alone (used before a default end is derived)."""
    if valid_from < _years_ago(paid_on, MAX_PAID_AGO_YEARS):
        return "Начало периода слишком далеко в прошлом"
    if (valid_from - paid_on).days > MAX_PERIOD_DAYS:
        return "Начало периода слишком далеко в будущем"
    return None


def _years_ago(day: date, years: int) -> date:
    if day.year - years < date.min.year:
        return date.min
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
    amount: Optional[int] = None,
    monthly_fee: Optional[int] = None,
) -> list[PaymentWarning]:
    """Soft rules: the payment is saved anyway.  ``existing`` = other live payments.

    ``amount`` / ``monthly_fee`` (both known) enable ``short_period_full_amount``: a calendar
    period shorter than 7 days paid at the full monthly fee (a free membership, fee 0, is not asked).
    """
    codes: list[str] = []
    if valid_to is not None:
        # (start, end) of what each calendar payment still covers in this membership
        calendar_payments = [(p.valid_from, p.end) for p in existing if p.end is not None and not p.covers_nothing]
        if any(start <= valid_to and end >= valid_from for start, end in calendar_payments):
            codes.append("overlaps_previous")
        elif calendar_payments:
            before = [end for _, end in calendar_payments if end < valid_from]
            if before and max(before) < valid_from - timedelta(days=1):
                codes.append("gap_before")
        if (valid_to - valid_from).days + 1 > WARN_PERIOD_DAYS:
            codes.append("period_over_62_days")
        if (
            (valid_to - valid_from).days + 1 < SHORT_PERIOD_DAYS
            and amount is not None
            and monthly_fee is not None
            and monthly_fee > 0
            and amount >= monthly_fee
        ):
            codes.append("short_period_full_amount")
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
