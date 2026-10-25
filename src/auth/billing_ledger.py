"""
ALGO - PAY-2: the student's account in a group as ONE pure function (design ``pay2_tech_design.md`` §1).

No database, no clock, no time zone: everything arrives in ``LedgerInput`` (business dates of Asia/Tashkent, ``today``
as a parameter) and ``run_ledger`` returns the whole truth: the charges (segments), where every sum of money went
(allocations), debt / advance, "paid until", the status, the lessons covered and the statement events.  Facts are stored
(payments, write-offs, refunds, transfers, stays, prices, settings); everything else is derived here, so voiding or
correcting a payment, a new price or a transfer needs no "recalculation" - the next call gives the new answer.

The money line.  Credits (payments, transfers in, write-offs) are laid one after another in the order
``(on, created_at, id)``; segments (charges) are laid in chronological order.  The money that falls on a segment is the
intersection of the two intervals - "the oldest debt first" without any loop over payments.  A segment with ``n`` billable
days and charge ``c`` that holds ``m`` is paid for ``floor(m * n / c)`` days (a day is paid only when paid in full; the rest
is a "partial day" that goes on counting towards the next payment).  Only the charge is rounded (``round_1000``), never the
money.  Whole sums are ``int``; ``//`` is the only division.

PAY-F: a lesson that the CENTRE cancelled (kind ``centre``) is paid back by the next payment when the centre asked for it (``recalc_versions``).  The
"recalculation" is a credit of kind ``recalc`` that ``run_ledger`` derives from the facts (the cancelled lessons, the periods, the prices); it is laid on the
money line like a write-off, but it is never stored and it is not cash (no refund, no payroll).
"""
from __future__ import annotations

import calendar as _calendar
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import payment_status as ps
from . import payments_texts as tx

CALENDAR = ps.CALENDAR
LESSONS = ps.LESSONS
# PAY-F (owner, 25.10): from this day a PARTIAL period of the «Общий месяц» (fixed billing day) is counted by the REGULAR schedule only - the numerator and the
# denominator alike: an extra one-off lesson is in neither (a copy of a move stays neutral, a lesson the centre cancelled stays in the numerator).  A period that STARTS
# before the day is computed the old way (nothing that has begun is recomputed).  It is a constant, not a setting: one decision of the owner for every centre, and a
# month can never be counted two ways by two centres; moving the day is a reviewed commit like any other rule of the money.
REGULAR_SCHEDULE_FROM = date(2026, 11, 1)
MAX_FUTURE_PERIODS = 24
MAX_VIRTUAL_LESSONS = 5000

# credit kinds
PAYMENT = "payment"
TRANSFER_IN = "transfer_in"
WRITEOFF = "writeoff"
RECALC = "recalc"              # PAY-F: derived from the cancelled lessons, never stored
NOT_CASH = (WRITEOFF, RECALC)  # credits that are not money received
# outflow kinds
REFUND = "refund"
TRANSFER_OUT = "transfer_out"

# buckets (relative to the date of the credit)
PAST_DEBT = tx.PAST_DEBT
CURRENT = tx.CURRENT
ADVANCE = tx.ADVANCE
ADVANCE_SEGMENT = "advance"  # the id of the "segment" that holds money no charge has absorbed yet

# flags
F_PRICE_MISSING = "price_missing"
F_OUTFLOW_EXCEEDS = "outflow_exceeds_money"
F_HORIZON = "horizon_capped"


# ================================================================================================================
# Inputs
# ================================================================================================================


@dataclass(frozen=True)
class Stay:
    """A stay of the student in the group, business days, both ends included; ``last`` None = still in the group."""

    first: date
    last: Optional[date] = None


@dataclass(frozen=True)
class Regime:
    """A version of the centre's billing settings, valid from ``effective_from`` (the first one "always")."""

    effective_from: date = date.min
    mode: str = CALENDAR  # calendar | lessons
    anchor: str = ps.PAYMENT_DATE  # payment_date | fixed_day (calendar only)
    billing_day: int = 1  # 1..28 (fixed_day only; 1 = calendar months)
    package_size: int = ps.DEFAULT_LESSONS_PER_PAYMENT  # lessons only
    # LOGIC-A (F12): the version acts for a run from the START OF ITS NEXT PERIOD under the previous regime (see ``_window_starts``)
    # instead of from ``effective_from``; a version without the flag acts from its date as before.
    defer: bool = False


@dataclass(frozen=True)
class PriceInfo:
    """The effective price on a day (the fields of ``pricing.Price`` that the segments show)."""

    amount: Optional[int]
    source: str = "none"  # group | override | none
    valid_from: Optional[date] = None
    group_amount: Optional[int] = None
    reason: Optional[str] = None


NO_PRICE = PriceInfo(None)


@dataclass(frozen=True)
class Credit:
    id: str
    kind: str  # payment | transfer_in | writeoff
    on: date
    created_at: datetime
    amount: int


@dataclass(frozen=True)
class Outflow:
    id: str
    kind: str  # refund | transfer_out
    on: date
    created_at: datetime
    amount: int


@dataclass(frozen=True)
class CountedLesson:
    """A held lesson that spends a package (lessons mode), in hand-out order."""

    day: date
    lesson_id: Optional[str] = None


@dataclass(frozen=True)
class Pause:
    """A live pause of the membership, business days, both ends included: its days are not charged."""

    first: date
    last: date


@dataclass(frozen=True)
class Opening:
    """The "opening balance" of a membership (LOGIC-A, decision 1): paid until ``paid_until`` and / or owing ``debt_amount`` on ``debt_on``."""

    paid_until: Optional[date] = None
    debt_amount: int = 0
    debt_on: Optional[date] = None


@dataclass(frozen=True)
class CentreCancel:
    """A lesson of the group that the CENTRE cancelled with a recalculation in mind (cancellation kind ``centre``; a holiday, a move and the old cancellations are not here)."""

    day: date
    lesson_id: str = ""


@dataclass(frozen=True)
class RecalcVersion:
    """The centre's setting «уроки, отменённые центром» from ``effective_from``: ``on`` = deduct them from the next payment (a lesson counts by its own date)."""

    effective_from: date
    on: bool


@dataclass(frozen=True)
class LedgerInput:
    today: date
    accounting_start: date
    stays: Sequence[Stay]
    regimes: Sequence[Regime]
    price_at: Callable[[date], PriceInfo]
    credits: Sequence[Credit] = ()
    outflows: Sequence[Outflow] = ()
    # a membership made by a transfer starts being charged the day AFTER the transfer (that day belongs to the old group)
    first_billable_floor: Optional[date] = None
    # dates of the group's lessons in [start, end] (for an incomplete period of a fixed billing day); one entry per lesson
    lesson_days: Callable[[date, date], Sequence[date]] = lambda start, end: ()
    # the lessons of the FULL schedule pattern (slots' validFrom ignored): the denominator of a partial period; ``None`` = the same as ``lesson_days``
    pattern_days: Optional[Callable[[date, date], Sequence[date]]] = None
    # PAY-F: the same two without the extra one-off lessons, for a cell of the fixed billing day that starts on or after ``regular_only_from`` (``None`` = never: the old way)
    regular_lesson_days: Optional[Callable[[date, date], Sequence[date]]] = None
    regular_pattern_days: Optional[Callable[[date, date], Sequence[date]]] = None
    regular_only_from: Optional[date] = None
    counted_lessons: Sequence[CountedLesson] = ()
    expiring_days: int = ps.DEFAULT_EXPIRING_DAYS
    expiring_lessons: int = ps.DEFAULT_EXPIRING_LESSONS
    pauses: Sequence[Pause] = ()
    opening: Optional[Opening] = None
    centre_cancels: Sequence[CentreCancel] = ()
    recalc_versions: Sequence[RecalcVersion] = ()


# ================================================================================================================
# Outputs
# ================================================================================================================


@dataclass(frozen=True)
class Segment:
    """One charge: a period of the student (or a lessons package that has held lessons)."""

    id: str
    kind: str  # period | package | opening (the debt of the opening balance: no days, no price)
    period_start: date
    period_end: date
    days: Tuple[date, ...]  # billable days (package: the dates of its held lessons)
    price: PriceInfo
    amount: int  # charged
    basis: str  # full | lessons | days | zero
    nominal_days: int  # length of the whole period (package: lessons in a package)
    lessons_in_part: Optional[int]
    lessons_in_period: Optional[int]
    begun: bool
    ended: bool
    unpriced: bool = False
    by_cell: bool = False       # PAY-F: a cell of the fixed billing day (the lessons of its month are counted)
    package_no: Optional[int] = None
    complete_package: bool = False
    # after the money is laid
    money: int = 0
    forgiven: int = 0
    recalculated: int = 0
    covered_days: int = 0
    partial_credit: int = 0

    @property
    def unpaid(self) -> int:
        return max(0, self.amount - self.money - self.forgiven - self.recalculated)

    @property
    def covered_through(self) -> Optional[date]:
        return self.days[self.covered_days - 1] if 0 < self.covered_days <= len(self.days) else None

    @property
    def state(self) -> str:
        if self.unpriced:
            return "unpriced"
        if self.unpaid == 0:
            return "closed"
        if not self.begun:
            return "future"
        return "partial" if self.money + self.forgiven + self.recalculated > 0 else "open"

    @property
    def bucket(self) -> str:
        """Relative to today: the period has ended (past debt), runs (current) or has not begun (future)."""
        return "past_debt" if self.ended else ("current" if self.begun else "future")

    @property
    def days_text(self) -> Optional[str]:
        return tx.days_text(
            basis=self.basis, billable_days=len(self.days), period_days=self.nominal_days,
            first_day=self.days[0] if self.days else None, lessons_in_part=self.lessons_in_part,
            lessons_in_period=self.lessons_in_period,
        ) if self.kind == "period" else None


@dataclass(frozen=True)
class Allocation:
    """A piece of one credit in one segment (``segment_id == ADVANCE_SEGMENT``: money no charge has absorbed yet)."""

    credit_id: str
    segment_id: str
    amount: int
    days: int
    bucket: str
    closes: bool
    price: Optional[int]
    label: str
    period_start: date
    period_end: date
    remaining_after: Optional[int] = None  # what is still unpaid of the segment right after this piece (null: advance)


@dataclass(frozen=True)
class StatementEvent:
    """A line of the statement before names are added by the service."""

    day: date
    rank: int  # 0 charge, 1 payment / transfer in, 2 write-off, 3 refund / transfer out
    kind: str  # charge | payment | transfer_in | writeoff | refund | transfer_out
    ref_id: str  # segment id or credit / outflow id
    debit: int = 0
    credit: int = 0
    outflow: int = 0
    balance_after: int = 0  # negative = debt, positive = advance


@dataclass(frozen=True)
class RecalcLine:
    """«Перерасчёт: урок ДД.ММ отменён центром»: ``amount`` comes off the next payment (a credit on the money line)."""

    id: str
    day: date
    lesson_id: str
    amount: int
    segment_id: str

    def credit(self) -> "Credit":
        return Credit(self.id, RECALC, self.day, datetime.min, self.amount)


@dataclass(frozen=True)
class LessonsView:
    package_size: int
    covered: int  # lessons paid for (held or not yet held)
    used: int  # counted lessons held
    left: int
    unpaid: int
    paid_flags: Tuple[bool, ...]  # per counted lesson, in hand-out order


@dataclass(frozen=True)
class LessonCoverInfo:
    """How one lesson is covered (what payroll reads).  ``source``: money | writeoff | before_start | unpaid | unpriced."""

    paid: bool
    source: str
    price: Optional[PriceInfo] = None  # the price of the SEGMENT that covers the lesson (null: no segment / no price)
    segment_id: Optional[str] = None


@dataclass(frozen=True)
class Ledger:
    mode: str
    segments: Tuple[Segment, ...]
    allocations: Tuple[Allocation, ...]
    net_money: Dict[str, int]  # credit id -> amount after refunds / transfers out
    refunded_from: Dict[str, int]  # credit id -> amount taken off it by REFUNDS
    transferred_from: Dict[str, int]  # credit id -> amount taken off it by transfers out
    charged: int
    paid: int  # payments, gross
    transferred_in: int
    written_off: int
    refunded: int
    transferred_out: int
    debt: int
    past_debt: int
    current_due: int
    advance: int
    refundable: int
    partial_credit: int
    covered_through: Optional[date]
    covered_days_total: int
    first_billable: Optional[date]
    unpriced_from: Optional[date]
    status: ps.StatusResult
    lessons: Optional[LessonsView]
    statement: Tuple[StatementEvent, ...]
    flags: Tuple[str, ...]
    counted: Tuple[CountedLesson, ...] = ()
    calendar_windows: Tuple[Tuple[date, Optional[date]], ...] = ()
    lessons_windows: Tuple[Tuple[date, Optional[date]], ...] = ()
    lesson_owner: Tuple[Optional[str], ...] = ()  # per counted lesson: the id of its package (null: held without a price)
    package_costs: Dict[str, Tuple[int, ...]] = field(default_factory=dict, repr=False, compare=False)
    pauses: Tuple[Tuple[date, date], ...] = ()
    opening: Optional[Opening] = None
    recalcs: Tuple[RecalcLine, ...] = ()       # PAY-F: the derived deductions for lessons the centre cancelled
    recalculated: int = 0                      # their sum
    _cache: Dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def reconciles(self) -> bool:
        """I2: charged - paid - transferred_in - written_off - recalculated + refunded + transferred_out == debt - advance."""
        return (
            self.charged - self.paid - self.transferred_in - self.written_off - self.recalculated + self.refunded + self.transferred_out
            == self.debt - self.advance
        )

    def lesson_paid(self, day: date, lesson_id: Optional[str] = None) -> bool:
        """Whether the lesson of ``day`` is paid (what payroll reads): see ``lesson_cover``."""
        return self.lesson_cover(day, lesson_id).paid

    def lesson_cover(self, day: date, lesson_id: Optional[str] = None) -> LessonCoverInfo:
        """How the lesson of ``day`` is covered and at WHAT PRICE (the price of the period / package that carries it).

        Days before the first charged day (before the accounting start, up to "paid until", the transfer day, before the student
        came) are "grace": not charged, so not owed - ``before_start``, paid, no price.  Lessons mode: the package of the place of
        the lesson in hand-out order; calendar: the segment that holds the day, covered when the day is not after "paid until".
        """
        if self.first_billable is None or day < self.first_billable:
            return LessonCoverInfo(True, "before_start")
        if any(lo <= day and (hi is None or day <= hi) for lo, hi in self.lessons_windows) and self.lessons is not None:
            for index, lesson in enumerate(self.counted):
                if lesson.day == day and (lesson_id is None or lesson.lesson_id == lesson_id):
                    paid = self.lessons.paid_flags[index]
                    owner = self.lesson_owner[index] if index < len(self.lesson_owner) else None
                    seg = self._segment(owner) if owner else None
                    if seg is None:
                        return LessonCoverInfo(paid, "unpriced")
                    if not paid:
                        return LessonCoverInfo(False, "unpaid", seg.price, seg.id)
                    place = sum(1 for j in range(index) if self.lesson_owner[j] == owner)
                    by_money = _cover(seg, seg.money, list(self.package_costs.get(seg.id, ())))
                    return LessonCoverInfo(True, "money" if place < by_money else "writeoff", seg.price, seg.id)
            return LessonCoverInfo(False, "unpaid")
        paid = self.covered_through is not None and day <= self.covered_through
        found = self._day_index().get(day)
        if found is None:
            return LessonCoverInfo(paid, "unpriced")
        seg, place = found
        if seg.unpriced:
            return LessonCoverInfo(paid, "unpriced", None, seg.id)
        if not paid:
            return LessonCoverInfo(False, "unpaid", seg.price, seg.id)
        return LessonCoverInfo(True, "money" if place < _cover(seg, seg.money) else "writeoff", seg.price, seg.id)

    def _segment(self, seg_id: str) -> Optional[Segment]:
        return next((s for s in self.segments if s.id == seg_id), None)

    def _day_index(self) -> Dict[date, Tuple[Segment, int]]:
        index = self._cache.get("days")
        if index is None:
            index = {d: (s, i) for s in self.segments if s.kind == "period" for i, d in enumerate(s.days)}
            self._cache["days"] = index
        return index


# ================================================================================================================
# Helpers
# ================================================================================================================


def round_1000(numerator: int, denominator: int = 1) -> int:
    return ps.round_1000(numerator, denominator)


def _last_day(year: int, month: int) -> int:
    return _calendar.monthrange(year, month)[1]


def _month_start(origin: date, anchor: int, k: int) -> date:
    """``s_k`` of a payment-date chain: month ``origin + k`` at ``min(anchor, last day)``."""
    index = origin.year * 12 + (origin.month - 1) + k
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(anchor, _last_day(year, month)))


def _day_range(first: date, last: date) -> List[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _credit_key(item) -> tuple:
    return (item.on, item.created_at, item.id)


def merge_stays(stays: Sequence[Stay]) -> List[Tuple[date, Optional[date]]]:
    """Overlapping and touching stays become one run."""
    ordered = sorted(((s.first, s.last if s.last is None or s.last >= s.first else s.first) for s in stays),
                     key=lambda r: (r[0], date.max if r[1] is None else r[1]))
    runs: List[List] = []
    for first, last in ordered:
        if runs and (runs[-1][1] is None or first <= runs[-1][1] + timedelta(days=1)):
            if runs[-1][1] is not None and (last is None or last > runs[-1][1]):
                runs[-1][1] = last
            continue
        runs.append([first, last])
    return [(a, b) for a, b in runs]


def regime_windows(regimes: Sequence[Regime]) -> List[Tuple[Regime, date, Optional[date]]]:
    ordered = sorted(regimes, key=lambda r: r.effective_from) or [Regime()]
    out = []
    for i, regime in enumerate(ordered):
        lo = ordered[i].effective_from if i else date.min
        hi = ordered[i + 1].effective_from - timedelta(days=1) if i + 1 < len(ordered) else None
        out.append((regime, lo, hi))
    return out


def regime_at(regimes: Sequence[Regime], day: date) -> Regime:
    for regime, lo, hi in regime_windows(regimes):
        if lo <= day and (hi is None or day <= hi):
            return regime
    return regimes[-1] if regimes else Regime()


# ================================================================================================================
# Outflows
# ================================================================================================================


def apply_outflows(credits: Sequence[Credit], outflows: Sequence[Outflow]):
    """Every outflow takes money off the NEWEST money credits dated on or before it (LIFO); write-offs and recalculations never come back."""
    net = {c.id: c.amount for c in credits}
    refunded_from: Dict[str, int] = {c.id: 0 for c in credits}
    transferred_from: Dict[str, int] = {c.id: 0 for c in credits}
    money = sorted((c for c in credits if c.kind not in NOT_CASH), key=_credit_key)
    exceeds = False
    for out in sorted(outflows, key=_credit_key):
        need = out.amount
        cut = refunded_from if out.kind == REFUND else transferred_from
        for c in reversed([c for c in money if c.on <= out.on]):
            take = min(need, net[c.id])
            net[c.id] -= take
            cut[c.id] += take
            need -= take
            if need == 0:
                break
        if need > 0:  # the service never allows it; the function does not fall over
            for c in reversed(money):
                take = min(need, net[c.id])
                net[c.id] -= take
                cut[c.id] += take
                need -= take
                if need == 0:
                    break
            exceeds = True
    return net, refunded_from, transferred_from, exceeds


# ================================================================================================================
# Generation of charges
# ================================================================================================================


class _Gen:
    """Builds the segments; keeps the horizon bookkeeping."""

    def __init__(self, inp: LedgerInput, total_credit: int, flags: set):
        self.inp = inp
        self.today = inp.today
        self.total_credit = total_credit
        self.flags = flags
        self.chain = 0
        self.unpriced_from: Optional[date] = None
        self._lesson_days_cache: Dict[tuple, Sequence[date]] = {}

    def regular(self, cell_start: date) -> bool:
        """Whether a cell of the fixed billing day that starts on ``cell_start`` is counted by the regular schedule only (PAY-F)."""
        return self.inp.regular_only_from is not None and cell_start >= self.inp.regular_only_from

    def lesson_days(self, start: date, end: date, *, regular: bool = False) -> Sequence[date]:
        fn = (self.inp.regular_lesson_days or self.inp.lesson_days) if regular else self.inp.lesson_days
        key = ("regular", start, end) if regular else (start, end)
        if key not in self._lesson_days_cache:
            self._lesson_days_cache[key] = tuple(fn(start, end))
        return self._lesson_days_cache[key]

    def pattern_days(self, start: date, end: date, *, regular: bool = False) -> Sequence[date]:
        """The lessons of a FULL period by the schedule pattern (the denominator)."""
        if regular:
            fn = self.inp.regular_pattern_days or self.inp.regular_lesson_days
            if fn is None:
                return self.pattern_days(start, end)
            key = ("regular-pattern", start, end)
        else:
            if self.inp.pattern_days is None:
                return self.lesson_days(start, end)
            fn, key = self.inp.pattern_days, ("pattern", start, end)
        if key not in self._lesson_days_cache:
            self._lesson_days_cache[key] = tuple(fn(start, end))
        return self._lesson_days_cache[key]

    def segment(
        self, chain: int, start: date, end: date, days: List[date], nominal: int, *, by_lessons: bool, cell: Optional[tuple]
    ) -> Segment:
        """The charge of one period; ``nominal`` = days of the whole period, ``by_lessons`` = fixed billing day."""
        # The price of a period is the price on its FIRST BILLABLE day (a full period: its start; a partial first period: the day
        # the student joined / came back / the accounting began) - a price that starts mid-period still prices the part.
        first = days[0] if days else start
        price = self.inp.price_at(first)
        begun = first <= self.today
        seg_id = f"seg:{chain}:{start.isoformat()}"
        n = len(days)
        common = dict(id=seg_id, kind="period", period_start=start, period_end=end, days=tuple(days),
                      price=price, nominal_days=nominal, begun=begun, ended=end < self.today, by_cell=by_lessons)
        if price.amount is None:
            return Segment(amount=0, basis="zero", lessons_in_part=None, lessons_in_period=None, unpriced=True, **common)
        amount = price.amount
        if amount == 0:
            return Segment(amount=0, basis="zero", lessons_in_part=None, lessons_in_period=None, **common)
        if n >= nominal:
            return Segment(amount=amount, basis="full", lessons_in_part=None, lessons_in_period=None, **common)
        if by_lessons:
            regular = self.regular(cell[0])             # PAY-F: from the cut-over day only the regular schedule is counted, in both
            held = self.lesson_days(*cell, regular=regular)             # REAL lessons: the numerator
            whole = self.pattern_days(*cell, regular=regular)            # lessons of a full month by the schedule: the denominator
            if whole:
                inside = set(days)
                in_part = sum(1 for d in held if d in inside)
                return Segment(amount=round_1000(amount * in_part, len(whole)), basis="lessons",
                               lessons_in_part=in_part, lessons_in_period=len(whole), **common)
        return Segment(amount=round_1000(amount * n, nominal), basis="days", lessons_in_part=None,
                       lessons_in_period=None, **common)

    # ---------------------------------------------------------------- calendar: payment date

    def payment_date_chain(self, lo: date, hi: Optional[date], so_far: int, pauses: Optional[List[Tuple[date, date]]] = None) -> List[Segment]:
        chain = self.chain
        self.chain += 1
        out: List[Segment] = []
        cum = so_far
        future = 0
        for start, end, length, days in _chain_periods(lo, pauses or []):
            if hi is not None and start > hi:
                break
            if hi is not None:
                days = [d for d in days if d <= hi]
                if not days:
                    break
                end = min(end, hi)
            if start > self.today:
                if hi is None:
                    if cum >= self.total_credit:
                        break
                    if future >= MAX_FUTURE_PERIODS:
                        if out and out[-1].amount > 0:
                            self.flags.add(F_HORIZON)
                        break
                future += 1
            seg = self.segment(chain, start, end, days, length, by_lessons=False, cell=None)
            if seg.unpriced:  # nothing is charged for it, it neither covers nor blocks; the next period is tried
                self.unpriced_from = self.unpriced_from or start
                if seg.begun:
                    self.flags.add(F_PRICE_MISSING)
                    out.append(seg)
                continue
            out.append(seg)
            cum += seg.amount
        return out

    # ---------------------------------------------------------------- calendar: fixed billing day

    def fixed_day_cells(self, regime: Regime, intervals: List[Tuple[date, Optional[date]]], so_far: int) -> List[Segment]:
        """One segment per cell of the centre's grid; days of all stays that fall into a cell are one segment."""
        chain = self.chain
        self.chain += 1
        day = regime.billing_day
        cells: Dict[date, List[date]] = {}
        open_lo: Optional[date] = None
        for lo, hi in intervals:
            if hi is None:
                open_lo = lo
                continue
            cell = ps.billing_period_start(lo, day)
            while cell <= hi:
                end = ps.billing_period_end(cell, day)
                cells.setdefault(cell, []).extend(_day_range(max(lo, cell), min(hi, end)))
                cell = end + timedelta(days=1)
        out: List[Segment] = []
        cum = so_far

        def emit(cell_start: date, days_in: List[date]) -> bool:
            """Append the segment (an unpriced one is recorded and skipped: it neither covers nor blocks)."""
            nonlocal cum
            end = ps.billing_period_end(cell_start, day)
            seg = self.segment(chain, cell_start, end, sorted(set(days_in)), (end - cell_start).days + 1,
                               by_lessons=True, cell=(cell_start, end))
            if seg.unpriced:
                self.unpriced_from = self.unpriced_from or cell_start
                if seg.begun:
                    self.flags.add(F_PRICE_MISSING)
                    out.append(seg)
                return True
            out.append(seg)
            cum += seg.amount
            return True

        first_open = ps.billing_period_start(open_lo, day) if open_lo is not None else None
        for start in sorted(cells):
            if first_open is not None and start >= first_open:
                break
            if not emit(start, cells[start]):
                return out
        if first_open is None:
            return out
        cell, future = first_open, 0
        while True:
            end = ps.billing_period_end(cell, day)
            days_in = _day_range(max(open_lo, cell), end) + cells.get(cell, [])
            if cell > self.today:
                if cum >= self.total_credit:
                    break
                if future >= MAX_FUTURE_PERIODS:
                    if out and out[-1].amount > 0:
                        self.flags.add(F_HORIZON)
                    break
                future += 1
            if not emit(cell, days_in):
                break
            try:
                cell = end + timedelta(days=1)
                ps.billing_period_end(cell, day)
            except OverflowError:
                break
        return out

    # ---------------------------------------------------------------- lessons packages

    def packages(self, regime: Regime, lessons: List[CountedLesson], last_window: bool, so_far: int):
        """Segments of the packages that hold lessons + the cost of EVERY lesson (held ones, then virtual ones)."""
        size = max(1, regime.package_size)
        segs: List[Segment] = []
        costs: List[int] = []
        chain = self.chain
        self.chain += 1
        pkg_price = NO_PRICE
        # a lesson held while no price was in force is skipped: neither charged nor paid (price_missing stays); the packages
        # go on from the first priced lesson
        priced = [self.inp.price_at(c.day).amount is not None for c in lessons]
        for c, ok in zip(lessons, priced):
            if not ok:
                self.unpriced_from = self.unpriced_from or c.day
                self.flags.add(F_PRICE_MISSING)
        lessons_all, lessons = lessons, [c for c, ok in zip(lessons, priced) if ok]
        owners: List[Optional[str]] = []
        place = 0
        for ok in priced:
            if ok:
                owners.append(f"seg:{chain}:pkg{place // size + 1}")
                place += 1
            else:
                owners.append(None)
        for k, start in enumerate(range(0, len(lessons), size), 1):
            chunk = lessons[start : start + size]
            first_day = chunk[0].day
            pkg_price = self.inp.price_at(first_day)
            days = [c.day for c in chunk]
            complete = len(chunk) == size
            common = dict(id=f"seg:{chain}:pkg{k}", kind="package", period_start=first_day, period_end=days[-1],
                          days=tuple(days), price=pkg_price, nominal_days=size, lessons_in_part=len(chunk),
                          lessons_in_period=size, begun=True, ended=complete,
                          package_no=k, complete_package=complete)
            p = pkg_price.amount
            part = [p * i // size - p * (i - 1) // size for i in range(1, len(chunk) + 1)]
            costs.extend(part)
            segs.append(Segment(amount=sum(part), basis="lessons" if p else "zero", **common))
        if last_window:
            # virtual lessons: the rest of the package in progress at its price, then the packages to come at today's
            p_now = self.inp.price_at(self.today).amount
            held = len(costs)
            pos = held % size
            price = pkg_price.amount if pos else p_now
            budget = self.total_credit - so_far - sum(costs)
            spent, i = 0, pos + 1
            while price and len(costs) - held < MAX_VIRTUAL_LESSONS and spent <= budget:   # a free group has no virtual lessons
                cost = price * i // size - price * (i - 1) // size
                costs.append(cost)
                spent += cost
                i += 1
                if i > size:
                    i, price = 1, p_now
        return segs, costs, priced, owners


# ================================================================================================================
# The ledger
# ================================================================================================================


def _cover(seg: Segment, volume: int, lesson_costs: Optional[List[int]] = None) -> int:
    """Days (lessons) of ``seg`` paid by ``volume`` of money on it."""
    n = len(seg.days)
    if seg.unpriced:
        return 0
    if seg.amount == 0:
        return n
    if seg.kind == "package":
        covered, run = 0, 0
        assert lesson_costs is not None
        for cost in lesson_costs[:n]:
            run += cost
            if run > volume:
                break
            covered += 1
        return covered
    return min(n, volume * n // seg.amount)


def _prefix(costs: Sequence[int]) -> List[int]:
    out, run = [], 0
    for c in costs:
        run += c
        out.append(run)
    return out


def _merge_pauses(pauses: Sequence[Pause]) -> List[Tuple[date, date]]:
    """Overlapping and touching pauses become one."""
    out: List[List[date]] = []
    for first, last in sorted((p.first, max(p.first, p.last)) for p in pauses):
        if out and first <= out[-1][1] + timedelta(days=1):
            out[-1][1] = max(out[-1][1], last)
        else:
            out.append([first, last])
    return [(a, b) for a, b in out]


def _subtract_pauses(lo: date, hi: Optional[date], pauses: List[Tuple[date, date]]) -> List[Tuple[date, Optional[date]]]:
    """``[lo, hi]`` (``hi`` None = open) without the days of the pauses: the pieces that are charged."""
    out: List[Tuple[date, Optional[date]]] = []
    cur: Optional[date] = lo
    for first, last in pauses:
        if cur is None or (hi is not None and first > hi):
            break
        if last < cur:
            continue
        if first > cur:
            out.append((cur, first - timedelta(days=1)))
        cur = last + timedelta(days=1)
        if hi is not None and cur > hi:
            cur = None
    if cur is not None:
        out.append((cur, hi))
    return out


def _is_paused(pauses: List[Tuple[date, date]], day: date) -> bool:
    i = bisect_right(pauses, (day, date.max)) - 1
    return i >= 0 and pauses[i][0] <= day <= pauses[i][1]


def _first_unpaused(pauses: List[Tuple[date, date]], day: date) -> date:
    while _is_paused(pauses, day):
        day += timedelta(days=1)
    return day


def _chain_periods(lo: date, pauses: List[Tuple[date, date]]):
    """The periods of a payment-date chain that begins on ``lo`` (LOGIC-A, owner decision: a pause never adds a charge).

    ``yield (start, end, length, days)``: period ``k`` has the NOMINAL length of its month (anchor = the day of ``lo``) in study days;
    the days of a pause are not study days, so a pause EXTENDS the period it falls into by the paused days and every later period
    begins the day after the previous one ends (the anchors shift, the chain is not restarted).  Without pauses this is the usual
    calendar chain ``s_k .. s_{k+1}-1``."""
    anchor = lo.day
    k = 0
    start = lo
    while True:
        try:
            cur = lo if k == 0 else _month_start(lo, anchor, k)
            nxt = _month_start(lo, anchor, k + 1)
        except (ValueError, OverflowError):
            return
        length = (nxt - cur).days
        days: List[date] = []
        day = start
        try:
            while len(days) < length:
                if not _is_paused(pauses, day):
                    days.append(day)
                day += timedelta(days=1)
        except OverflowError:
            return
        yield start, days[-1], length, days
        start = days[-1] + timedelta(days=1)
        k += 1


def _lessons_switch(size: int, priced_days: List[date], start: date, c: date) -> date:
    """A package under the old regime that has begun by ``c`` is finished first: the switch is the day after its last lesson
    (``c + 1`` when every package is full, ``c`` when none has begun, never while the package is open)."""
    held = [d for d in priced_days if d >= start]
    n = bisect_right(held, c)
    if n == 0:
        return c
    if n % size == 0:
        return c + timedelta(days=1)
    end = ((n + size - 1) // size) * size
    return held[end - 1] + timedelta(days=1) if len(held) >= end else date.max


def _window_starts(windows, piece_first: date, priced_days: List[date], pauses: Optional[List[Tuple[date, date]]] = None) -> List[date]:
    """The first day of every regime version FOR ONE PIECE of the stay (spec §1.5).

    A version without ``defer`` starts on its date.  A deferred one starts at the first day of the NEXT PERIOD of the regime that
    was in force for the piece on that date (the later decision wins: a deferred version that has not begun yet is not "the
    previous regime"): ``fixed_day`` - the next cell of the grid; ``payment_date`` - the next month of the piece's chain;
    ``lessons`` - after the package in progress is finished.  A piece that had nothing charged by then switches at once.
    """
    starts = [date.min]
    for k in range(1, len(windows)):
        regime, c, _ = windows[k]
        if not regime.defer:
            starts.append(c)
            continue
        j = max(i for i in range(k) if starts[i] <= c)
        prev = windows[j][0]
        begin = max(piece_first, starts[j])
        if begin > c:
            starts.append(c)
        elif prev.mode == LESSONS:
            starts.append(_lessons_switch(max(1, prev.package_size), priced_days, begin, c))
        elif prev.anchor == ps.FIXED_DAY:
            day = prev.billing_day
            starts.append(ps.billing_period_end(ps.billing_period_start(c, day), day) + timedelta(days=1))
        else:  # payment_date: the first period of the chain (pauses extend it) that begins after c
            first_day = _first_unpaused(pauses or [], begin)
            nxt = None
            for period_start, _end, _length, _days in _chain_periods(first_day, pauses or []):
                if period_start > c:
                    nxt = period_start
                    break
            starts.append(nxt if nxt is not None else date.max)
    return starts


def _window_bounds(starts: List[date]) -> List[Tuple[date, Optional[date]]]:
    """``(lo, hi)`` of every version for one piece: a day belongs to the LAST version that has begun by then."""
    out = []
    for k, lo in enumerate(starts):
        later = min(starts[k + 1:], default=None)
        out.append((lo, None if later is None or later == date.max else later - timedelta(days=1)))
    return out


def _pieces_of(inp: LedgerInput):
    """``(opening, floor, runs, pause_days, spans)``: the stay from the floor on (``spans``, the pauses are still inside: a payment-date
    chain is extended by them); the days that are charged are ``_subtract_pauses`` of a span."""
    opening = inp.opening
    if opening is not None and not (opening.paid_until or (opening.debt_amount and opening.debt_on)):
        opening = None
    floor = max(inp.accounting_start, inp.first_billable_floor or date.min,
                (opening.paid_until + timedelta(days=1)) if opening is not None and opening.paid_until else date.min)
    runs = merge_stays(inp.stays)
    pause_days = _merge_pauses(inp.pauses)
    spans: List[Tuple[date, Optional[date]]] = []
    for first, last in runs:
        lo = max(first, floor)
        if last is not None and lo > last:
            continue
        if _subtract_pauses(lo, last, pause_days):      # a span that lies wholly in a pause has no charged day
            spans.append((lo, last))
    return opening, floor, runs, pause_days, spans


def regime_switch_days(inp: LedgerInput, new: Regime) -> List[date]:
    """F12 «who switches when»: the first day under ``new`` (a version that begins today, ``defer``) of every piece of the stay that is still
    open - ``max`` / ``min`` of the list are the last / first switch.  Closed stays do not switch."""
    regimes = list(inp.regimes) + [replace(new, defer=True, effective_from=inp.today)]
    windows = regime_windows(regimes)
    _, _, _, pause_days, spans = _pieces_of(inp)
    priced = sorted(c.day for c in inp.counted_lessons if inp.price_at(c.day).amount is not None)
    return [_window_starts(windows, first, priced, pause_days)[-1] for first, last in spans if last is None]


def recalc_on(versions: Sequence[RecalcVersion], day: date) -> bool:
    """Whether the centre's setting «вычитать уроки, отменённые центром» was on for a lesson of ``day`` (the last version that began by then; none = off)."""
    on = False
    for version in sorted(versions, key=lambda v: v.effective_from):
        if version.effective_from <= day:
            on = version.on
    return on


def _forgiven_on(segments: Sequence[Segment], credits: Sequence[Credit], net: Dict[str, int]) -> Dict[str, int]:
    """What the write-offs cover on every segment when the credits are laid WITHOUT the recalculation (the same line as the money line below)."""
    spans: Dict[str, Tuple[int, int]] = {}
    pos = 0
    for c in credits:
        spans[c.id] = (pos, pos + net[c.id])
        pos += net[c.id]
    out: Dict[str, int] = {}
    cur = 0
    for s in segments:
        lo, hi = cur, cur + s.amount
        cur = hi
        out[s.id] = sum(max(0, min(spans[c.id][1], hi) - max(spans[c.id][0], lo)) for c in credits if c.kind == WRITEOFF)
    return out


def _recalc_lines(inp: LedgerInput, gen: "_Gen", segments: Sequence[Segment], forgiven: Dict[str, int]) -> List[RecalcLine]:
    """PAY-F: one deduction per lesson that the centre cancelled inside a charged period of a calendar mode.

    A lesson counts when the student was charged for its day (the day is one of the days of a period that has begun: not on a pause, not before he
    came or after he left, not in the lessons mode) and the setting was on that day.  It costs ``round_1000(what the period costs the student /
    lessons of the period by the schedule)``: the charge minus what the centre forgave on that period (a forgiven period gives no second discount, and
    what is paid back never exceeds what the student really bore).  A partial first period of a fixed billing day costs the same price of a lesson
    (``price / lessons of the full month``, scaled by the same share), so the total is the price x the lessons that were held / the lessons of the month.
    The deductions of a period never exceed that sum."""
    if not inp.centre_cancels or not any(v.on for v in inp.recalc_versions):
        return []
    owner = {d: s for s in segments if s.kind == "period" and s.begun and not s.unpriced and s.amount > 0 for d in s.days}
    bearable = {s.id: max(0, s.amount - forgiven.get(s.id, 0)) for s in owner.values()}
    left = dict(bearable)
    unit: Dict[str, int] = {}
    lines: List[RecalcLine] = []
    for cancel in sorted(inp.centre_cancels, key=lambda c: (c.day, c.lesson_id)):
        seg = owner.get(cancel.day)
        if seg is None or not recalc_on(inp.recalc_versions, cancel.day) or bearable[seg.id] <= 0:
            continue
        if seg.id not in unit:
            inside = set(seg.days)
            if seg.basis == "lessons" and seg.lessons_in_period:
                # the full month by the schedule is the divisor
                unit[seg.id] = round_1000(seg.price.amount * bearable[seg.id], seg.lessons_in_period * seg.amount)
            else:
                # the price of the month is the price of the REGULAR schedule: an extra one-off lesson of the period never cheapens the deducted lesson
                # (the recalculation is new, so there is no earlier rule to keep - unlike the share of a partial period, see REGULAR_SCHEDULE_FROM)
                count = sum(1 for d in gen.pattern_days(seg.period_start, seg.period_end, regular=True) if d in inside)
                unit[seg.id] = round_1000(bearable[seg.id], count) if count else 0
        amount = min(unit[seg.id], left[seg.id])
        if amount <= 0:
            continue
        left[seg.id] -= amount
        lines.append(RecalcLine(f"rc:{cancel.day.isoformat()}:{cancel.lesson_id}", cancel.day, cancel.lesson_id, amount, seg.id))
    return lines


def run_ledger(inp: LedgerInput) -> Ledger:
    """The account.  The recalculation is a credit too, so the future periods are generated for the money PLUS the deductions (a second pass, only when
    there is a deduction): otherwise the credit that settles beyond the running period would not find its period (``suggested_amount`` of the next one)."""
    ledger = _run_ledger(inp, 0)
    return _run_ledger(inp, ledger.recalculated) if ledger.recalculated else ledger


def _run_ledger(inp: LedgerInput, horizon_extra: int) -> Ledger:
    today = inp.today
    flags: set = set()
    opening, floor, runs, pause_days, spans_of_stay = _pieces_of(inp)
    credits = sorted(inp.credits, key=_credit_key)
    net, refunded_from, transferred_from, exceeds = apply_outflows(credits, inp.outflows)
    if exceeds:
        flags.add(F_OUTFLOW_EXCEEDS)
    total_credit = sum(net.values())
    gen = _Gen(inp, total_credit + horizon_extra, flags)
    windows = regime_windows(inp.regimes)
    deferred = any(w[0].defer for w in windows[1:])
    priced_days = sorted(c.day for c in inp.counted_lessons if inp.price_at(c.day).amount is not None) if deferred else []
    bounds = [_window_bounds(_window_starts(windows, first, priced_days, pause_days) if deferred else [w[1] for w in windows])
              for first, _ in spans_of_stay]
    # the piece that tells which version is in force today (the one that holds today, else the last before it)
    ref = next((i for i, (f, l) in enumerate(spans_of_stay) if f <= today and (l is None or l >= today)), None)
    if ref is None:
        before = [i for i, (f, _) in enumerate(spans_of_stay) if f <= today]
        ref = before[-1] if before else (0 if spans_of_stay else None)
    tail = len(windows) - 1
    cur_index = None
    if ref is not None:
        started = [i for i, (lo, _) in enumerate(bounds[ref]) if lo <= today]
        cur_index = started[-1] if started else 0
        if windows[-1][0].defer and bounds[ref][-1][0] > today:
            tail = cur_index

    segments: List[Segment] = []
    if opening is not None and opening.debt_amount and opening.debt_on:
        segments.append(Segment(
            id="seg:opening", kind="opening", period_start=opening.debt_on, period_end=opening.debt_on, days=(), price=NO_PRICE,
            amount=int(opening.debt_amount), basis="opening", nominal_days=0, lessons_in_part=None, lessons_in_period=None,
            begun=True, ended=opening.debt_on < today))
    cal_windows: List[Tuple[date, Optional[date]]] = []
    les_windows: List[Tuple[date, Optional[date]]] = []
    package_costs: Dict[str, List[int]] = {}
    lesson_costs_all: List[int] = []
    package_regime: Optional[Regime] = None
    held_all: List[CountedLesson] = []
    lesson_priced: List[bool] = []     # per held lesson: a price was in force that day
    lesson_owner: List[Optional[str]] = []
    first_billable: Optional[date] = None

    for index, (regime, wlo, whi) in enumerate(windows):
        last_window = index == tail
        intervals: List[Tuple[date, Optional[date]]] = []      # the charged pieces (the pauses cut out)
        span_ivs: List[Tuple[date, Optional[date]]] = []       # the same stay with the pauses still inside (payment-date chains)
        spans_here: List[Tuple[date, Optional[date]]] = []
        for (first, last), bnd in zip(spans_of_stay, bounds):
            wlo_p, whi_p = bnd[index]
            if wlo_p == date.max:
                continue
            lo = max(first, wlo_p)
            hi = last if whi_p is None else (whi_p if last is None else min(last, whi_p))
            if whi_p is None or wlo_p <= whi_p:
                spans_here.append((wlo_p, whi_p))
            if hi is not None and lo > hi:
                continue
            span_ivs.append((lo, hi))
            intervals.extend(_subtract_pauses(lo, hi, pause_days))
        if not spans_of_stay:
            spans_here = [(wlo, whi)]
        elif deferred or intervals:
            # a day is looked up in the version of ITS piece: the span is clipped to the pieces it holds (pieces are apart in time,
            # and a deferred version may begin earlier for a later piece than the old one ends for an earlier piece)
            spans_here = list(intervals)
        spans_here = sorted(set(spans_here), key=lambda x: (x[0], x[1] or date.max))
        if regime.mode == LESSONS:
            les_windows.extend(x for x in spans_here if x not in les_windows)
            window_lessons = [c for c in inp.counted_lessons if any(lo <= c.day and (hi is None or c.day <= hi) for lo, hi in intervals)]
            window_lessons.sort(key=lambda c: c.day)
            if intervals:
                first_billable = min(first_billable or date.max, min(lo for lo, _ in intervals))
            so_far = sum(s.amount for s in segments)
            segs, costs, priced_flags, owners = gen.packages(regime, window_lessons, last_window, so_far)
            lesson_priced.extend(priced_flags)
            lesson_owner.extend(owners)
            segments.extend(segs)
            held_all.extend(window_lessons)
            lesson_costs_all.extend(costs)
            package_regime = regime
            continue
        cal_windows.extend(x for x in spans_here if x not in cal_windows)
        if not intervals:
            continue
        first_billable = min(first_billable or date.max, min(lo for lo, _ in intervals))
        so_far = sum(s.amount for s in segments)
        if regime.anchor == ps.FIXED_DAY:
            segments.extend(gen.fixed_day_cells(regime, intervals, so_far))
        else:
            for lo, hi in span_ivs:
                so_far = sum(s.amount for s in segments)
                segments.extend(gen.payment_date_chain(_first_unpaused(pause_days, lo), hi, so_far, pause_days))

    segments.sort(key=lambda s: (s.kind != "opening", (s.days[0] if s.days else s.period_start), s.period_start, s.id))

    # ---- PAY-F: the recalculation for the lessons the centre cancelled is one more credit on the money line (derived here, never stored)
    recalcs = _recalc_lines(inp, gen, segments, _forgiven_on(segments, credits, net))
    if recalcs:
        credits = sorted([*credits, *(line.credit() for line in recalcs)], key=_credit_key)
        net, refunded_from, transferred_from, _ = apply_outflows(credits, inp.outflows)
        total_credit = sum(net.values())

    # ---- the money line
    spans: Dict[str, Tuple[int, int]] = {}
    pos = 0
    for c in credits:
        spans[c.id] = (pos, pos + net[c.id])
        pos += net[c.id]
    pieces: Dict[str, Dict[str, int]] = {}
    cur = 0
    seg_span: Dict[str, Tuple[int, int]] = {}
    for s in segments:
        seg_span[s.id] = (cur, cur + s.amount)
        cur += s.amount
    # package costs per segment (for coverage)
    cost_cursor = 0
    seg_costs: Dict[str, List[int]] = {}
    for s in segments:
        if s.kind == "package":
            seg_costs[s.id] = lesson_costs_all[cost_cursor : cost_cursor + len(s.days)]
            cost_cursor += len(s.days)
    kind_of = {c.id: c.kind for c in credits}
    final: List[Segment] = []
    vol_by_seg_credit: Dict[Tuple[str, str], int] = {}
    for s in segments:
        lo, hi = seg_span[s.id]
        money = forgiven = recalculated = 0
        for c in credits:
            a, b = spans[c.id]
            part = max(0, min(b, hi) - max(a, lo))
            if part:
                vol_by_seg_credit[(s.id, c.id)] = part
                if kind_of[c.id] == WRITEOFF:
                    forgiven += part
                elif kind_of[c.id] == RECALC:
                    recalculated += part
                else:
                    money += part
        vol = money + forgiven + recalculated
        covered = _cover(s, vol, seg_costs.get(s.id))
        partial = 0
        if covered < len(s.days) and s.amount and not s.unpriced:
            if s.kind == "package":
                partial = vol - sum(seg_costs[s.id][:covered])
            else:
                partial = vol - (covered * s.amount) // len(s.days)
        final.append(replace(s, money=money, forgiven=forgiven, recalculated=recalculated, covered_days=covered, partial_credit=partial))
    segments = final
    money_total_in_segments = cur

    # ---- allocations
    allocations: List[Allocation] = []
    running: Dict[str, int] = {s.id: 0 for s in segments}
    prefix_all = _prefix(lesson_costs_all)
    held_count = sum(len(s.days) for s in segments if s.kind == "package")
    base_offset = 0  # what the calendar charges before the first package absorb
    for s in segments:
        if s.kind == "package":
            break
        base_offset += s.amount
    cum_before_credit = 0
    for c in credits:
        placed = 0
        for s in segments:
            part = vol_by_seg_credit.get((s.id, c.id), 0)
            if not part:
                continue
            # lessons that cost nothing (a price of a few sum per package) are covered by the first piece
            before = _cover(s, running[s.id], seg_costs.get(s.id)) if running[s.id] else 0
            running[s.id] += part
            after = _cover(s, running[s.id], seg_costs.get(s.id))
            on = c.on
            if s.kind == "opening":
                bucket = PAST_DEBT
            elif s.kind == "package":
                bucket = PAST_DEBT if (s.complete_package and s.period_end <= on) else (
                    CURRENT if s.period_start <= on else ADVANCE)
            else:
                bucket = PAST_DEBT if s.period_end < on else (CURRENT if s.period_start <= on else ADVANCE)
            closes = running[s.id] >= s.amount
            allocations.append(Allocation(
                credit_id=c.id, segment_id=s.id, amount=part, days=after - before, bucket=bucket, closes=closes,
                price=s.price.amount,
                label=(tx.opening_label(s.period_start, closes) if s.kind == "opening" else
                       tx.allocation_label(bucket=bucket, closes=closes, start=s.period_start, end=s.period_end,
                                           package_no=s.package_no)),
                period_start=s.period_start, period_end=s.period_end,
                remaining_after=max(0, s.amount - running[s.id])))
            placed += part
        left = net[c.id] - placed
        if left > 0:
            extra = 0
            if prefix_all:  # lessons: how many more (not yet held) lessons this leftover pays for
                start_line = max(cum_before_credit, money_total_in_segments)
                end_line = cum_before_credit + net[c.id]
                extra = max(0, bisect_right(prefix_all, end_line - base_offset) - held_count) - \
                    max(0, bisect_right(prefix_all, start_line - base_offset) - held_count)
            label = tx.advance_label_lessons() if prefix_all else "Аванс на периоды, которые ещё не начались"
            allocations.append(Allocation(
                credit_id=c.id, segment_id=ADVANCE_SEGMENT, amount=left, days=extra, bucket=ADVANCE, closes=False,
                price=None, label=label, period_start=c.on, period_end=c.on))
        cum_before_credit += net[c.id]
    if recalcs:     # a recalculation is not a payment: it keeps its place on the money line but has no allocation of its own
        derived = {line.id for line in recalcs}
        allocations = [a for a in allocations if a.credit_id not in derived]

    # ---- totals
    begun = [s for s in segments if s.begun]
    charged = sum(s.amount for s in begun)
    paid = sum(c.amount for c in credits if c.kind == PAYMENT)
    transferred_in = sum(c.amount for c in credits if c.kind == TRANSFER_IN)
    written_off = sum(c.amount for c in credits if c.kind == WRITEOFF)
    recalculated = sum(line.amount for line in recalcs)
    refunded = sum(o.amount for o in inp.outflows if o.kind == REFUND)
    transferred_out = sum(o.amount for o in inp.outflows if o.kind == TRANSFER_OUT)
    debt = max(0, charged - total_credit)
    advance = max(0, total_credit - charged)
    past_debt = min(sum(s.unpaid for s in begun if s.ended), debt)
    current_due = debt - past_debt
    money_net = sum(v for k, v in net.items() if kind_of[k] not in NOT_CASH)
    refundable = min(advance, money_net)

    # ---- coverage prefix ("paid until" of the opening balance is where it starts; an unpaid opening debt stops it)
    through: Optional[date] = opening.paid_until if opening is not None else None
    for s in segments:
        if s.unpriced:
            continue
        if s.kind == "opening":
            if s.unpaid > 0:
                break
            continue
        if s.covered_days >= len(s.days):
            if s.days:
                through = s.days[-1]
            continue
        through = s.days[s.covered_days - 1] if s.covered_days > 0 else (
            through if through is not None else s.days[0] - timedelta(days=1))
        break
    # only the continuous prefix counts for the total
    covered_prefix = 0
    for s in segments:
        if s.unpriced:
            continue
        if s.kind == "opening":
            if s.unpaid > 0:
                break
            continue
        covered_prefix += s.covered_days
        if s.covered_days < len(s.days):
            break

    # ---- partial credit (the first segment that is not paid in full)
    partial_credit = 0
    if not prefix_all:
        for s in segments:
            if s.unpriced or s.kind == "opening":
                continue
            if s.covered_days < len(s.days):
                partial_credit = s.partial_credit
                break

    # ---- the lessons view
    current_regime = windows[cur_index][0] if cur_index is not None else regime_at(inp.regimes, today)
    mode = current_regime.mode
    lessons_view: Optional[LessonsView] = None
    held_lessons = held_all
    if mode == LESSONS or les_windows:
        available = total_credit - base_offset
        covered_lessons = bisect_right(prefix_all, available) if available >= 0 else 0
        used = sum(lesson_priced)          # lessons held under a price (the others are neither charged nor paid)
        if mode == LESSONS:
            partial_credit = max(0, available - (prefix_all[covered_lessons - 1] if covered_lessons else 0))
        flags_paid, seen = [], 0
        for ok in lesson_priced:
            flags_paid.append(ok and seen < covered_lessons)
            seen += ok
        flags_paid = tuple(flags_paid)
        lessons_view = LessonsView(
            package_size=(package_regime.package_size if package_regime else current_regime.package_size),
            covered=covered_lessons, used=used, left=max(0, covered_lessons - used),
            unpaid=max(0, used - covered_lessons), paid_flags=flags_paid)

    # ---- status
    has_event = bool(inp.credits) or opening is not None or bool(pause_days)
    open_now = any(last is None or last >= today for _, last in runs)
    price_today = inp.price_at(today).amount
    paused_until = next((last for first, last in pause_days if first <= today <= last), None) if open_now else None
    owed_since = None
    open_debt = next((s for s in segments if s.kind == "opening" and s.unpaid > 0), None)
    if open_debt is not None:
        owed_since = open_debt.period_start
    # m2: a student whose price is 0 stays FREE whatever pauses / openings he has - only money that was really paid (or moved in) changes that
    status = _status(inp, mode, has_event, open_now, through, lessons_view, price_today, paused_until, owed_since, pause_days,
                     has_money=bool(inp.credits))

    # ---- statement
    events: List[StatementEvent] = []
    for s in begun:
        if s.amount:
            events.append(StatementEvent(day=s.days[0] if s.days else s.period_start, rank=0,
                                         kind="opening_debt" if s.kind == "opening" else "charge", ref_id=s.id, debit=s.amount))
    if opening is not None and opening.paid_until:
        shown = min(today, max(min((r[0] for r in runs), default=today), inp.accounting_start))
        events.append(StatementEvent(day=shown, rank=0, kind="opening_paid_until", ref_id="opening:paid_until"))
    for first, last in pause_days:
        events.append(StatementEvent(day=first, rank=4, kind="pause", ref_id=f"pause:{first.isoformat()}:{last.isoformat()}"))
    for c in credits:
        rank = 2 if c.kind == WRITEOFF else 1
        events.append(StatementEvent(day=c.on, rank=rank, kind=c.kind, ref_id=c.id, credit=c.amount))
    for o in inp.outflows:
        events.append(StatementEvent(day=o.on, rank=3, kind=o.kind, ref_id=o.id, outflow=o.amount))
    events.sort(key=lambda e: (e.day, e.rank, e.ref_id))
    bal = 0
    stmt: List[StatementEvent] = []
    for e in events:
        bal += e.credit - e.debit - e.outflow
        stmt.append(replace(e, balance_after=bal))

    ledger = Ledger(
        mode=mode, segments=tuple(segments), allocations=tuple(allocations), net_money=net,
        refunded_from=refunded_from, transferred_from=transferred_from, charged=charged, paid=paid, transferred_in=transferred_in, written_off=written_off,
        refunded=refunded, transferred_out=transferred_out, debt=debt, past_debt=past_debt, current_due=current_due,
        advance=advance, refundable=refundable, partial_credit=partial_credit, covered_through=through,
        covered_days_total=covered_prefix, first_billable=first_billable, unpriced_from=gen.unpriced_from,
        status=status, lessons=lessons_view, statement=tuple(stmt), flags=tuple(sorted(flags)),
        counted=tuple(held_lessons), calendar_windows=tuple(cal_windows), lessons_windows=tuple(les_windows),
        lesson_owner=tuple(lesson_owner), package_costs={sid: tuple(c) for sid, c in seg_costs.items()},
        pauses=tuple(pause_days), opening=opening, recalcs=tuple(recalcs), recalculated=recalculated)
    return ledger


def overdue_days(through: date, today: date, pauses: Sequence[Tuple[date, date]]) -> int:
    """m1: the days after ``through`` up to ``today`` without the days of a pause (a pause never makes a student late: the count starts
    with the first CHARGED unpaid day)."""
    paused = sum((min(last, today) - max(first, through + timedelta(days=1))).days + 1
                 for first, last in pauses if last > through and first <= today)
    return max(1, (today - through).days - paused)


def _status(inp: LedgerInput, mode: str, has_event: bool, open_now: bool, through: Optional[date],
            lessons: Optional[LessonsView], price_today: Optional[int], paused_until: Optional[date] = None,
            owed_since: Optional[date] = None, pauses: Sequence[Tuple[date, date]] = (), has_money: Optional[bool] = None) -> ps.StatusResult:
    today = inp.today
    if price_today == 0 and (not (has_event if has_money is None else has_money) or mode == LESSONS):
        return ps.StatusResult(status=ps.FREE, period_mode=mode)
    if not has_event or not open_now:
        return ps.StatusResult(status=ps.NONE, period_mode=mode)
    if paused_until is not None:  # the day lies inside a pause: no "days overdue", the place is kept
        return ps.StatusResult(status=ps.PAUSED, period_mode=mode, paid_until=through, paused_until=paused_until)
    if mode == LESSONS and lessons is not None:
        left = lessons.left
        status = ps.EXPIRED if left == 0 else ps.EXPIRING if left <= inp.expiring_lessons else ps.ACTIVE
        return ps.StatusResult(status=status, period_mode=LESSONS, lessons_total=lessons.covered,
                               lessons_used=lessons.used, lessons_left=left)
    if through is None:
        if owed_since is not None:  # an opening debt and nothing paid until: the student owes since that day
            return ps.StatusResult(status=ps.EXPIRED, period_mode=CALENDAR, days_overdue=max(1, (today - owed_since).days))
        return ps.StatusResult(status=ps.NONE, period_mode=mode)
    delta = (through - today).days
    if delta < 0:
        status, left = ps.EXPIRED, None
        overdue = overdue_days(through, today, pauses)
    elif delta == 0:
        status, overdue, left = ps.ENDS_TODAY, None, 0
    elif delta <= inp.expiring_days:
        status, overdue, left = ps.EXPIRING, None, delta
    else:
        status, overdue, left = ps.ACTIVE, None, delta
    return ps.StatusResult(status=status, period_mode=CALENDAR, paid_until=through, days_overdue=overdue, days_left=left)


# ================================================================================================================
# Derived views
# ================================================================================================================


def ledger_with_extra_credit(inp: LedgerInput, credit: Credit) -> Tuple[Ledger, Ledger]:
    """``(before, after)`` the credit is added: the engine of «что закроет»."""
    return run_ledger(inp), run_ledger(replace(inp, credits=tuple(inp.credits) + (credit,)))


def sliced_input(inp: LedgerInput, as_of: date) -> LedgerInput:
    """The account as it stood on ``as_of``: later credits / outflows gone, stays closed on that day."""
    stays = []
    for s in inp.stays:
        if s.first > as_of:
            continue
        stays.append(Stay(s.first, as_of if s.last is None or s.last > as_of else s.last))
    return replace(
        inp, today=as_of, stays=tuple(stays),
        credits=tuple(c for c in inp.credits if c.on <= as_of), outflows=tuple(o for o in inp.outflows if o.on <= as_of),
        counted_lessons=tuple(c for c in inp.counted_lessons if c.day <= as_of),
        centre_cancels=tuple(c for c in inp.centre_cancels if c.day <= as_of),
        pauses=tuple(p for p in inp.pauses if p.first <= as_of),
    )


def refundable_at(inp: LedgerInput, as_of: date) -> int:
    """What can leave the account on ``as_of`` (the money a transfer takes along)."""
    return run_ledger(sliced_input(inp, as_of)).refundable


def coverage_records(ledger: Ledger, *, created_at: datetime = datetime.min) -> List[ps.PaymentRecord]:
    """Synthetic ``PaymentRecord``s for the excuse deadline: one per continuous paid range (calendar) or one package
    with ``lessons_count = covered`` (lessons)."""
    records: List[ps.PaymentRecord] = []
    if ledger.mode == LESSONS and ledger.lessons is not None:
        if ledger.lessons.covered:  # valid from "always": the pool of the counted lessons decides which ones it took
            records.append(ps.PaymentRecord(paid_on=date.min, valid_from=date.min, lessons_count=ledger.lessons.covered,
                                            created_at=created_at))
        return records
    start: Optional[date] = None
    last: Optional[date] = None
    for s in ledger.segments:
        if s.unpriced:
            continue
        if not s.days or s.covered_days <= 0:
            break
        first_day = s.days[0]
        end_day = s.days[min(s.covered_days, len(s.days)) - 1]
        if start is None:
            start = first_day
        last = end_day
        if s.covered_days < len(s.days):
            break
    if start is not None and last is not None:
        records.append(ps.PaymentRecord(paid_on=start, valid_from=start, valid_to=last, created_at=created_at))
    return records
