"""
ALGO - PAY-DUE «Срок оплаты»: the ONE rule that names an unpaid sum - «К оплате» (still within its term) or «Долг» (the term has passed).

A pure function over a READY ``bl.Ledger``: it is outside ``run_ledger`` and ``_status`` on purpose.  Nothing here changes a charge, a payment, «оплачено до», the payroll or the reconciliation - the
setting of the term never reaches the ledger; it only decides the NAME of what the ledger already says is unpaid (the invariant of the package, tested as a snapshot of the money records).

* periods (``Segment.kind == "period"``): the term ``D`` of an unpaid begun period is its ``pay_due_days``-th day without a pause, the first charged day being the first (``0`` = a debt from the first day);
  ``today <= D`` -> «к оплате до D», later -> «долг».  Only the pauses the student asked for move it (``Ledger.real_pauses``; the gap of a bridged return does not).
* the opening balance (``kind == "opening"``): always a debt.
* packages (``kind == "package"``): by lesson - of the held unpaid lessons (the payment settles the oldest first) at most ``pay_due_lessons`` of the NEWEST are «к оплате», and only while
  ``today <= lesson date + PACKAGE_DUE_MAX_DAYS``; the rest are a debt.  A closed participation (a leaving, a transfer) or a package window that a change of the mode has ended owes at once.
* ``in_centre = False`` (the student left the centre: archived or no active group): every unpaid sum of every part is a debt at once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import billing_ledger as bl

DEFAULT_DUE_DAYS = 5            # ``pay_due_days`` of a centre that has not set it (periods): the one value of the code
DEFAULT_DUE_LESSONS = 2         # ``pay_due_lessons`` (M) of a centre that has not set it (packages)
MAX_DUE_DAYS = 14
MAX_DUE_LESSONS = 4
PACKAGE_DUE_MAX_DAYS = 14       # an unpaid lesson stops being «к оплате» this many days after its date whatever M is (a holiday, a student who stopped coming)

DEBT, DUE, NONE = "debt", "due", "none"


@dataclass(frozen=True)
class DuePart:
    """One unpaid part of an account (a period, the opening balance, or the lessons of a package), already named."""

    segment_id: str
    kind: str                           # period | package | opening
    unpaid: int
    name: str                           # debt | due
    due_until: Optional[date] = None    # the last day of «к оплате» (the oldest lesson + 14 for a package); ``None`` for a debt
    overdue_days: int = 0               # K: the days the debt is overdue (0 for «к оплате»)
    lessons: Optional[int] = None       # package: how many lessons the part holds
    term: Optional[date] = None         # period: the day ``D`` the term ended / ends (``None`` when the term is 0 days: a debt from the first day; the opening balance and packages have none)
    lapsed: Optional[date] = None       # a package DEBT: the last day of the term of its oldest lesson (words only: «срок был ...» of a statement row)


@dataclass(frozen=True)
class DueResult:
    debt_amount: int = 0
    due_amount: int = 0
    due_until: Optional[date] = None             # the nearest last day of «к оплате» among the parts that are «к оплате»
    due_lessons: Optional[Dict[str, int]] = None  # package accounts: {"used": lessons «к оплате», "limit": M}
    debt_lessons: int = 0
    overdue_days: int = 0                        # K of the oldest debt part (0 when the debt is not overdue yet or there is none)
    name: str = NONE
    parts: Tuple[DuePart, ...] = field(default_factory=tuple)

    def lessons_said(self, name: str) -> Optional[int]:
        """The number of lessons to say with the sum of ``name`` («debt» / «due»): only when EVERY part of that name is made of package lessons - a sum of a package and a period is not «N уроков» (17.2); else ``None``."""
        parts = [p for p in self.parts if p.name == name]
        if not parts or any(p.kind != "package" for p in parts):
            return None
        return sum(p.lessons or 0 for p in parts)

    @property
    def unpaid(self) -> int:
        return self.debt_amount + self.due_amount

    def parts_of(self, segment_id: str) -> List[DuePart]:
        return [p for p in self.parts if p.segment_id == segment_id]


NOTHING = DueResult()


def clamp_days(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_DUE_DAYS else DEFAULT_DUE_DAYS


def clamp_lessons(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_DUE_LESSONS else DEFAULT_DUE_LESSONS


def _paused(pauses: Sequence[Tuple[date, date]], day: date) -> bool:
    return any(first <= day <= last for first, last in pauses)


def term_day(first: date, days: int, pauses: Sequence[Tuple[date, date]]) -> date:
    """``D``: the ``days``-th day without a pause counting ``first`` as the first; ``days`` of 0 gives the day before ``first`` (a debt from the first day)."""
    if days <= 0:
        return first - timedelta(days=1)
    day, counted = first, 0
    while True:
        if not _paused(pauses, day):
            counted += 1
            if counted == days:
                return day
        day += timedelta(days=1)


def _first_day(seg: bl.Segment) -> date:
    """The first charged day: the first lesson of a part-period of the common month, else the first billable day."""
    return seg.first_lesson or (seg.days[0] if seg.days else seg.period_start)


def _package_closed(ledger: bl.Ledger, seg: bl.Segment, today: date, closed: bool) -> bool:
    """The package window of ``seg`` has ended (a change of the mode) or the participation is closed (a leaving, a transfer): its unpaid lessons are a debt now."""
    if closed or not ledger.open_now:
        return True
    for lo, hi in ledger.lessons_windows:
        if lo <= seg.period_start and (hi is None or seg.period_start <= hi):
            return hi is not None and hi < today
    return False


def classify(ledger: bl.Ledger, today: date, pay_due_days: int, pay_due_lessons: int, in_centre: bool, closed: bool = False) -> DueResult:
    """Names the unpaid sum of ``ledger`` (see the module docstring).  Pure: reads the ledger, never changes it.  ``closed``: the stay is being closed by the action the window describes."""
    if ledger.debt <= 0:
        return NOTHING
    pauses = ledger.real_pauses
    parts: List[DuePart] = []
    lessons: List[Tuple[date, int, str]] = []          # the unpaid lessons of the packages, oldest first: (day, unpaid cost, segment id)
    closed_pkg: Dict[str, bool] = {}
    for seg in ledger.segments:
        if not seg.begun or seg.unpriced:
            continue
        if seg.kind == "package":
            costs = ledger.package_costs.get(seg.id, ())
            closed_pkg[seg.id] = _package_closed(ledger, seg, today, closed)
            for i in range(seg.covered_days, len(seg.days)):
                cost = costs[i] if i < len(costs) else 0
                if i == seg.covered_days:
                    cost -= seg.partial_credit
                if cost > 0:
                    lessons.append((seg.days[i], cost, seg.id))
            continue
        if seg.unpaid <= 0:
            continue
        if seg.kind == "opening":
            since = seg.period_start
            parts.append(DuePart(seg.id, "opening", seg.unpaid, DEBT, None, max(0, (today - since).days)))
            continue
        term = term_day(_first_day(seg), pay_due_days, pauses)
        if in_centre and today <= term:
            parts.append(DuePart(seg.id, "period", seg.unpaid, DUE, term, term=term))
        else:
            parts.append(DuePart(seg.id, "period", seg.unpaid, DEBT, None, bl.overdue_days(term, today, pauses) if today > term else 0, term=term if pay_due_days > 0 else None))
    parts.extend(_package_parts(lessons, closed_pkg, today, pay_due_lessons, in_centre))
    parts = _fit(parts, ledger.debt)
    if not parts:
        return NOTHING
    debt = sum(p.unpaid for p in parts if p.name == DEBT)
    due = sum(p.unpaid for p in parts if p.name == DUE)
    until = min((p.due_until for p in parts if p.name == DUE and p.due_until), default=None)
    debt_parts = [p for p in parts if p.name == DEBT]
    has_packages = any(p.kind == "package" for p in parts)          # the lessons are said of package PARTS only: a centre that once had packages is not a «package» centre (17.2)
    return DueResult(
        debt_amount=debt, due_amount=due, due_until=until,
        due_lessons={"used": sum(p.lessons or 0 for p in parts if p.name == DUE and p.kind == "package"), "limit": pay_due_lessons} if has_packages else None,
        debt_lessons=sum(p.lessons or 0 for p in debt_parts if p.kind == "package"),
        overdue_days=max((p.overdue_days for p in debt_parts), default=0),
        name=DEBT if debt > 0 else DUE if due > 0 else NONE, parts=tuple(parts),
    )


def _package_parts(lessons: List[Tuple[date, int, str]], closed: Dict[str, bool], today: date, limit: int, in_centre: bool) -> List[DuePart]:
    """The unpaid lessons of the packages by the sliding rule; the lessons of one package with one name make one part."""
    n = len(lessons)
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for j, (day, cost, seg_id) in enumerate(lessons):
        window_end = day + timedelta(days=PACKAGE_DUE_MAX_DAYS)
        newer = n - 1 - j
        is_due = in_centre and not closed.get(seg_id, False) and newer < limit and today <= window_end
        name = DUE if is_due else DEBT
        slot = grouped.setdefault((seg_id, name), {"unpaid": 0, "n": 0, "oldest": day, "age": 0, "term": None})
        slot["unpaid"] += cost
        slot["n"] += 1
        if name == DEBT:
            # the day the lesson became a debt: 15 days after it, or the day of the lesson that pushed it out of the ``limit`` newest - the earlier one; K counts from there
            pushed_by = lessons[j + limit][0] - timedelta(days=1) if j + limit < n else None
            term = window_end if pushed_by is None else min(window_end, pushed_by)
            slot["age"] = max(slot["age"], max(0, (today - term).days))
            slot["term"] = term if slot["term"] is None else min(slot["term"], term)          # the last day of the term of the OLDEST lesson of the part: «срок был ...» of the statement row
    out: List[DuePart] = []
    for (seg_id, name), slot in grouped.items():
        out.append(DuePart(seg_id, "package", slot["unpaid"], name,
                           slot["oldest"] + timedelta(days=PACKAGE_DUE_MAX_DAYS) if name == DUE else None,
                           slot["age"] if name == DEBT else 0, slot["n"], None, slot["term"] if name == DEBT else None))
    return out


def _fit(parts: List[DuePart], debt: int) -> List[DuePart]:
    """The parts never add up to more than the account owes (``Ledger.debt`` is ``max(0, charged - credits)``): an excess comes off the newest «к оплате» parts first."""
    excess = sum(p.unpaid for p in parts) - debt
    if excess <= 0:
        return [p for p in parts if p.unpaid > 0]
    out = list(parts)
    for name in (DUE, DEBT):
        for i in range(len(out) - 1, -1, -1):
            if excess <= 0:
                break
            p = out[i]
            if p.name != name:
                continue
            cut = min(excess, p.unpaid)
            out[i] = DuePart(p.segment_id, p.kind, p.unpaid - cut, p.name, p.due_until, p.overdue_days, p.lessons, p.term, p.lapsed)
            excess -= cut
    return [p for p in out if p.unpaid > 0]
