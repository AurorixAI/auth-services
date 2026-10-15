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
payroll is ``monthly_fee`` of the membership, never a payment ``amount``.

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
    payments = [p for p in payments if not p.covers_nothing]
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
) -> list[PaymentWarning]:
    """Soft rules: the payment is saved anyway.  ``existing`` = other live payments."""
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
