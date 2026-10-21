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
MAX_FUTURE_PERIODS = 24
MAX_VIRTUAL_LESSONS = 5000

# credit kinds
PAYMENT = "payment"
TRANSFER_IN = "transfer_in"
WRITEOFF = "writeoff"
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
    counted_lessons: Sequence[CountedLesson] = ()
    expiring_days: int = ps.DEFAULT_EXPIRING_DAYS
    expiring_lessons: int = ps.DEFAULT_EXPIRING_LESSONS


# ================================================================================================================
# Outputs
# ================================================================================================================


@dataclass(frozen=True)
class Segment:
    """One charge: a period of the student (or a lessons package that has held lessons)."""

    id: str
    kind: str  # period | package
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
    package_no: Optional[int] = None
    complete_package: bool = False
    # after the money is laid
    money: int = 0
    forgiven: int = 0
    covered_days: int = 0
    partial_credit: int = 0

    @property
    def unpaid(self) -> int:
        return max(0, self.amount - self.money - self.forgiven)

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
        return "partial" if self.money + self.forgiven > 0 else "open"

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
class LessonsView:
    package_size: int
    covered: int  # lessons paid for (held or not yet held)
    used: int  # counted lessons held
    left: int
    unpaid: int
    paid_flags: Tuple[bool, ...]  # per counted lesson, in hand-out order


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

    @property
    def reconciles(self) -> bool:
        """I2: charged - paid - transferred_in - written_off + refunded + transferred_out == debt - advance."""
        return (
            self.charged - self.paid - self.transferred_in - self.written_off + self.refunded + self.transferred_out
            == self.debt - self.advance
        )

    def lesson_paid(self, day: date, lesson_id: Optional[str] = None) -> bool:
        """Whether the lesson of ``day`` is paid (what payroll reads).

        Days before the first charged day are "grace" (not charged, so not owed); a lessons-mode lesson is paid when its
        place in hand-out order is covered; a calendar day when it is not after "paid until".
        """
        if self.first_billable is None or day < self.first_billable:
            return True
        if any(lo <= day and (hi is None or day <= hi) for lo, hi in self.lessons_windows) and self.lessons is not None:
            for index, lesson in enumerate(self.counted):
                if lesson.day == day and (lesson_id is None or lesson.lesson_id == lesson_id):
                    return self.lessons.paid_flags[index]
            return False
        return self.covered_through is not None and day <= self.covered_through


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
    """Every outflow takes money off the NEWEST money credits dated on or before it (LIFO); write-offs never come back."""
    net = {c.id: c.amount for c in credits}
    refunded_from: Dict[str, int] = {c.id: 0 for c in credits}
    transferred_from: Dict[str, int] = {c.id: 0 for c in credits}
    money = sorted((c for c in credits if c.kind != WRITEOFF), key=_credit_key)
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
        self._lesson_days_cache: Dict[Tuple[date, date], Sequence[date]] = {}

    def lesson_days(self, start: date, end: date) -> Sequence[date]:
        key = (start, end)
        if key not in self._lesson_days_cache:
            self._lesson_days_cache[key] = tuple(self.inp.lesson_days(start, end))
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
                      price=price, nominal_days=nominal, begun=begun, ended=end < self.today)
        if price.amount is None:
            return Segment(amount=0, basis="zero", lessons_in_part=None, lessons_in_period=None, unpriced=True, **common)
        amount = price.amount
        if amount == 0:
            return Segment(amount=0, basis="zero", lessons_in_part=None, lessons_in_period=None, **common)
        if n >= nominal:
            return Segment(amount=amount, basis="full", lessons_in_part=None, lessons_in_period=None, **common)
        if by_lessons:
            held = self.lesson_days(*cell)
            if held:
                inside = set(days)
                in_part = sum(1 for d in held if d in inside)
                return Segment(amount=round_1000(amount * in_part, len(held)), basis="lessons",
                               lessons_in_part=in_part, lessons_in_period=len(held), **common)
        return Segment(amount=round_1000(amount * n, nominal), basis="days", lessons_in_part=None,
                       lessons_in_period=None, **common)

    # ---------------------------------------------------------------- calendar: payment date

    def payment_date_chain(self, lo: date, hi: Optional[date], so_far: int) -> List[Segment]:
        chain = self.chain
        self.chain += 1
        out: List[Segment] = []
        anchor = lo.day
        k = 0
        cum = so_far
        future = 0
        while True:
            try:
                start = lo if k == 0 else _month_start(lo, anchor, k)
                nxt = _month_start(lo, anchor, k + 1)
            except (ValueError, OverflowError):
                break
            end = nxt - timedelta(days=1)
            if hi is not None and start > hi:
                break
            seg_hi = end if hi is None else min(end, hi)
            if start > self.today:
                if hi is None:
                    if cum >= self.total_credit:
                        break
                    if future >= MAX_FUTURE_PERIODS:
                        if out and out[-1].amount > 0:
                            self.flags.add(F_HORIZON)
                        break
                future += 1
            seg = self.segment(chain, start, end if seg_hi == end else seg_hi, _day_range(start, seg_hi),
                               (end - start).days + 1, by_lessons=False, cell=None)
            k += 1
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
            if pkg_price.amount is None:
                self.unpriced_from = self.unpriced_from or first_day
                self.flags.add(F_PRICE_MISSING)
                segs.append(Segment(amount=0, basis="zero", unpriced=True, **common))
                return segs, costs  # nothing is charged from here on
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
        return segs, costs


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


def run_ledger(inp: LedgerInput) -> Ledger:
    today = inp.today
    flags: set = set()
    floor = max(inp.accounting_start, inp.first_billable_floor or date.min)
    credits = sorted(inp.credits, key=_credit_key)
    net, refunded_from, transferred_from, exceeds = apply_outflows(credits, inp.outflows)
    if exceeds:
        flags.add(F_OUTFLOW_EXCEEDS)
    total_credit = sum(net.values())
    gen = _Gen(inp, total_credit, flags)
    runs = merge_stays(inp.stays)
    windows = regime_windows(inp.regimes)

    segments: List[Segment] = []
    cal_windows: List[Tuple[date, Optional[date]]] = []
    les_windows: List[Tuple[date, Optional[date]]] = []
    package_costs: Dict[str, List[int]] = {}
    lesson_costs_all: List[int] = []
    package_regime: Optional[Regime] = None
    held_all: List[CountedLesson] = []
    first_billable: Optional[date] = None

    for index, (regime, wlo, whi) in enumerate(windows):
        last_window = index == len(windows) - 1
        lo_bound = max(wlo, floor)
        intervals: List[Tuple[date, Optional[date]]] = []
        for first, last in runs:
            lo = max(first, lo_bound)
            hi = last if whi is None else (whi if last is None else min(last, whi))
            if hi is not None and lo > hi:
                continue
            intervals.append((lo, hi))
        if regime.mode == LESSONS:
            les_windows.append((wlo, whi))
            window_lessons = [c for c in inp.counted_lessons if lo_bound <= c.day and (whi is None or c.day <= whi)]
            window_lessons.sort(key=lambda c: c.day)
            if intervals:
                first_billable = min(first_billable or date.max, intervals[0][0])
            so_far = sum(s.amount for s in segments)
            segs, costs = gen.packages(regime, window_lessons, last_window, so_far)
            segments.extend(segs)
            held_all.extend(window_lessons)
            lesson_costs_all.extend(costs)
            package_regime = regime
            continue
        cal_windows.append((wlo, whi))
        if not intervals:
            continue
        first_billable = min(first_billable or date.max, intervals[0][0])
        so_far = sum(s.amount for s in segments)
        if regime.anchor == ps.FIXED_DAY:
            segments.extend(gen.fixed_day_cells(regime, intervals, so_far))
        else:
            for lo, hi in intervals:
                so_far = sum(s.amount for s in segments)
                segments.extend(gen.payment_date_chain(lo, hi, so_far))

    segments.sort(key=lambda s: ((s.days[0] if s.days else s.period_start), s.period_start, s.id))

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
        money = forgiven = 0
        for c in credits:
            a, b = spans[c.id]
            part = max(0, min(b, hi) - max(a, lo))
            if part:
                vol_by_seg_credit[(s.id, c.id)] = part
                if kind_of[c.id] == WRITEOFF:
                    forgiven += part
                else:
                    money += part
        vol = money + forgiven
        covered = _cover(s, vol, seg_costs.get(s.id))
        partial = 0
        if covered < len(s.days) and s.amount and not s.unpriced:
            if s.kind == "package":
                partial = vol - sum(seg_costs[s.id][:covered])
            else:
                partial = vol - (covered * s.amount) // len(s.days)
        final.append(replace(s, money=money, forgiven=forgiven, covered_days=covered, partial_credit=partial))
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
            if s.kind == "package":
                bucket = PAST_DEBT if (s.complete_package and s.period_end <= on) else (
                    CURRENT if s.period_start <= on else ADVANCE)
            else:
                bucket = PAST_DEBT if s.period_end < on else (CURRENT if s.period_start <= on else ADVANCE)
            closes = running[s.id] >= s.amount
            allocations.append(Allocation(
                credit_id=c.id, segment_id=s.id, amount=part, days=after - before, bucket=bucket, closes=closes,
                price=s.price.amount,
                label=tx.allocation_label(bucket=bucket, closes=closes, start=s.period_start, end=s.period_end,
                                          package_no=s.package_no),
                period_start=s.period_start, period_end=s.period_end))
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

    # ---- totals
    begun = [s for s in segments if s.begun]
    charged = sum(s.amount for s in begun)
    paid = sum(c.amount for c in credits if c.kind == PAYMENT)
    transferred_in = sum(c.amount for c in credits if c.kind == TRANSFER_IN)
    written_off = sum(c.amount for c in credits if c.kind == WRITEOFF)
    refunded = sum(o.amount for o in inp.outflows if o.kind == REFUND)
    transferred_out = sum(o.amount for o in inp.outflows if o.kind == TRANSFER_OUT)
    debt = max(0, charged - total_credit)
    advance = max(0, total_credit - charged)
    past_debt = min(sum(s.unpaid for s in begun if s.ended), debt)
    current_due = debt - past_debt
    money_net = sum(v for k, v in net.items() if kind_of[k] != WRITEOFF)
    refundable = min(advance, money_net)

    # ---- coverage prefix
    through: Optional[date] = None
    for s in segments:
        if s.unpriced:
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
        covered_prefix += s.covered_days
        if s.covered_days < len(s.days):
            break

    # ---- partial credit (the first segment that is not paid in full)
    partial_credit = 0
    if not prefix_all:
        for s in segments:
            if s.unpriced:
                continue
            if s.covered_days < len(s.days):
                partial_credit = s.partial_credit
                break

    # ---- the lessons view
    current_regime = regime_at(inp.regimes, today)
    mode = current_regime.mode
    lessons_view: Optional[LessonsView] = None
    held_lessons = held_all
    if mode == LESSONS or les_windows:
        available = total_credit - base_offset
        covered_lessons = bisect_right(prefix_all, available) if available >= 0 else 0
        used = len(held_lessons)
        if mode == LESSONS:
            partial_credit = max(0, available - (prefix_all[covered_lessons - 1] if covered_lessons else 0))
        flags_paid = tuple(i < covered_lessons for i in range(used))
        lessons_view = LessonsView(
            package_size=(package_regime.package_size if package_regime else current_regime.package_size),
            covered=covered_lessons, used=used, left=max(0, covered_lessons - used),
            unpaid=max(0, used - covered_lessons), paid_flags=flags_paid)

    # ---- status
    has_event = bool(credits)
    open_now = any(last is None or last >= today for _, last in runs)
    price_today = inp.price_at(today).amount
    status = _status(inp, mode, has_event, open_now, through, lessons_view, price_today)

    # ---- statement
    events: List[StatementEvent] = []
    for s in begun:
        if s.amount:
            events.append(StatementEvent(day=s.days[0] if s.days else s.period_start, rank=0, kind="charge",
                                         ref_id=s.id, debit=s.amount))
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
        counted=tuple(held_lessons), calendar_windows=tuple(cal_windows), lessons_windows=tuple(les_windows))
    return ledger


def _status(inp: LedgerInput, mode: str, has_event: bool, open_now: bool, through: Optional[date],
            lessons: Optional[LessonsView], price_today: Optional[int]) -> ps.StatusResult:
    today = inp.today
    if price_today == 0 and (not has_event or mode == LESSONS):
        return ps.StatusResult(status=ps.FREE, period_mode=mode)
    if not has_event or not open_now:
        return ps.StatusResult(status=ps.NONE, period_mode=mode)
    if mode == LESSONS and lessons is not None:
        left = lessons.left
        status = ps.EXPIRED if left == 0 else ps.EXPIRING if left <= inp.expiring_lessons else ps.ACTIVE
        return ps.StatusResult(status=status, period_mode=LESSONS, lessons_total=lessons.covered,
                               lessons_used=lessons.used, lessons_left=left)
    if through is None:
        return ps.StatusResult(status=ps.NONE, period_mode=mode)
    delta = (through - today).days
    if delta < 0:
        status, overdue, left = ps.EXPIRED, -delta, None
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
