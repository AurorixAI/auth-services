"""
ALGO - PAY-2: the loader that turns the database into ``billing_ledger.LedgerInput`` (design §3.1).

The ONLY place where facts of the database (stays, payments, write-offs, refunds, transfers, prices, settings, schedule,
held lessons) become the input of the pure function ``run_ledger``.  A fixed number of queries whatever the number of
memberships: settings (1) + memberships (1) + stays (1) + payments / write-offs / refunds (3) + transfers (1 per hop of the
transfer chain, usually 1-2) + prices (2) + schedule (3, only when a period is incomplete under a fixed billing day) +
held lessons (2-3, only in lessons mode).

A transfer between groups is a link; the money that travels is ``refundable_at`` of the old membership on the transfer
day, computed here (memoised, sources first) and given to the new membership as a credit and to the old one as an outflow.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import Session

from src.models.auth import (
    Group,
    GroupAttendance,
    MembershipOpeningBalance,
    MembershipPause,
    MembershipTransfer,
    Organization,
    Student,
    StudentGroup,
    StudentGroupPeriod,
    StudentPayment,
    StudentRefund,
    StudentWriteoff,
    User,
)

from . import billing_ledger as bl
from . import membership_periods
from . import payment_status as ps
from . import pricing

logger = logging.getLogger(__name__)

MAX_TRANSFER_HOPS = 8
SCHEDULE_HORIZON_DAYS = 800  # the preload of the schedule reaches this far past today (24 future periods + slack)
FORECAST_DAYS = 400          # how far ahead the forecast of a package looks for the day of its last lesson (a package of 57 weekly lessons fits)


# ============================================================================================================
# Settings
# ============================================================================================================


@dataclass(frozen=True)
class LedgerSettings:
    accounting_start: date
    regimes: Tuple[bl.Regime, ...]
    expiring_days: int
    expiring_lessons: int
    recalc_versions: Tuple[bl.RecalcVersion, ...] = ()    # PAY-F: «вычитать уроки, отменённые центром» (own history: it never splits the periods)

    @property
    def current(self) -> bl.Regime:
        return self.regimes[-1]



def regime_from_block(block: Dict[str, Any], effective_from: date) -> bl.Regime:
    """One regime from a settings dict (``period_mode`` / ``billing_anchor`` / ``billing_day`` / ``lessons_per_payment``)."""
    mode = block.get("period_mode") or ps.CALENDAR
    anchor = block.get("billing_anchor") or ps.PAYMENT_DATE
    day = block.get("billing_day")
    size = block.get("lessons_per_payment")
    return bl.Regime(
        effective_from=effective_from,
        mode=mode if mode in (ps.CALENDAR, ps.LESSONS) else ps.CALENDAR,
        anchor=anchor if anchor in (ps.PAYMENT_DATE, ps.FIXED_DAY) else ps.PAYMENT_DATE,
        billing_day=day if isinstance(day, int) and 1 <= day <= 28 else 1,
        package_size=size if isinstance(size, int) and 1 <= size <= 60 else ps.DEFAULT_LESSONS_PER_PAYMENT,
        defer=block.get("defer") is True and effective_from != date.min,   # LOGIC-A (F12): from the start of the next period
    )


def parse_regimes(block: Dict[str, Any]) -> Tuple[bl.Regime, ...]:
    """The regimes of an organisation: ``billing_history`` (every version, the first one from always) or the one of the
    current settings."""
    history = block.get("billing_history")
    regimes: List[bl.Regime] = []
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("from")
            try:
                start = date.min if raw in (None, "", "min") else date.fromisoformat(str(raw)[:10])
            except ValueError:
                continue
            regimes.append(regime_from_block(entry, start))
    if not regimes:
        return (regime_from_block(block, date.min),)
    regimes.sort(key=lambda r: r.effective_from)
    return tuple(regimes)


def parse_recalc(block: Dict[str, Any]) -> Tuple[bl.RecalcVersion, ...]:
    """PAY-F: ``recalc_history`` = ``[{"from": "2026-10-25", "on": true}, ...]`` (the setting «уроки, отменённые центром» with the day each value began)."""
    out: List[bl.RecalcVersion] = []
    history = block.get("recalc_history")
    for entry in history if isinstance(history, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("on"), bool):
            continue
        raw = entry.get("from")
        try:
            start = date.min if raw in (None, "", "min") else date.fromisoformat(str(raw)[:10])
        except ValueError:
            continue
        out.append(bl.RecalcVersion(start, entry["on"]))
    return tuple(sorted(out, key=lambda v: v.effective_from))


def accounting_start_of(block: Dict[str, Any], created: Optional[datetime]) -> date:
    """The accounting start: the setting, else the day the centre was created (never a retro debt for "all the history")."""
    if block.get("accounting_start_date"):
        try:
            return date.fromisoformat(str(block["accounting_start_date"])[:10])
        except ValueError:
            pass
    return ps.business_date(created) if created else ps.business_today()


def load_settings(db: Session, org_id: str) -> LedgerSettings:
    row = db.query(Organization.settings, Organization.created_at).filter(Organization.id == org_id).first()
    raw = row[0] if row else None
    created = row[1] if row else None
    block = raw.get("payments") if isinstance(raw, dict) else None
    block = dict(block) if isinstance(block, dict) else {}
    return LedgerSettings(
        accounting_start=accounting_start_of(block, created),
        regimes=parse_regimes(block),
        expiring_days=_int(block.get("expiring_days"), ps.DEFAULT_EXPIRING_DAYS),
        expiring_lessons=_int(block.get("expiring_lessons"), ps.DEFAULT_EXPIRING_LESSONS),
        recalc_versions=parse_recalc(block),
    )


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


# ============================================================================================================
# Result
# ============================================================================================================


@dataclass
class MembershipLedger:
    membership: StudentGroup
    student: Student
    group: Group
    teacher: Optional[User]
    inp: bl.LedgerInput
    ledger: bl.Ledger
    price: pricing.Price  # the effective price today
    payments: List[StudentPayment] = field(default_factory=list)  # live, kind payment
    writeoffs: List[StudentWriteoff] = field(default_factory=list)
    refunds: List[StudentRefund] = field(default_factory=list)
    transfers_in: List[MembershipTransfer] = field(default_factory=list)
    transfers_out: List[MembershipTransfer] = field(default_factory=list)
    credit_of_transfer: Dict[str, int] = field(default_factory=dict)  # transfer id -> the sum that travelled
    pauses: List[MembershipPause] = field(default_factory=list)  # live (LOGIC-A)
    opening: Optional[MembershipOpeningBalance] = None  # the live opening balance (LOGIC-A)
    loader: Optional["LedgerLoader"] = field(default=None, repr=False, compare=False)  # PAY-F: for ``LedgerLoader.forecasts``


def price_info(price: pricing.Price) -> bl.PriceInfo:
    return bl.PriceInfo(price.amount, price.source, price.valid_from, price.group_amount, price.reason)


# ============================================================================================================
# Loader
# ============================================================================================================


def _days(first: date, last: date) -> List[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class LedgerLoader:
    """Loads the facts of a set of memberships (and of the memberships linked to them by transfers) and runs the ledger."""

    def __init__(self, db: Session, org_id: str, today: date, settings: Optional[LedgerSettings] = None, *, recalc: bool = True) -> None:
        """``recalc=False``: the account WITHOUT the deduction for lessons the centre cancelled (payroll reads the money of the students as it was: PAY-F, scenario 17)."""
        self.db = db
        self.org_id = org_id
        self.today = today
        self.settings = settings or load_settings(db, org_id)
        self.recalc_versions = self.settings.recalc_versions if recalc else ()
        self.cancels: Dict[UUID, List[bl.CentreCancel]] = {}      # group id -> lessons the centre cancelled (kind ``centre``)
        self._cancels_loaded: Set[UUID] = set()
        self.members: Dict[UUID, Tuple[StudentGroup, Student, Group, Optional[User]]] = {}
        self.stays: Dict[UUID, List[bl.Stay]] = {}
        self.payments: Dict[UUID, List[StudentPayment]] = {}
        self.writeoffs: Dict[UUID, List[StudentWriteoff]] = {}
        self.refunds: Dict[UUID, List[StudentRefund]] = {}
        self.pauses: Dict[UUID, List[MembershipPause]] = {}
        self.openings: Dict[UUID, Optional[MembershipOpeningBalance]] = {}
        self.t_in: Dict[UUID, List[MembershipTransfer]] = {}
        self.t_out: Dict[UUID, List[MembershipTransfer]] = {}
        self.transfers: Dict[UUID, MembershipTransfer] = {}
        self.books = pricing.PriceBooks()
        self.counted: Dict[UUID, List[bl.CountedLesson]] = {}
        self._x_memo: Dict[UUID, int] = {}
        self._x_active: Set[UUID] = set()
        self._guard_hits = 0
        self._indexed: Set[Tuple[UUID, str]] = set()
        self._schedule: Optional[Any] = None
        self._moved_one_offs: Dict[Any, Set[Tuple[str, str]]] = {}   # group id -> keys (loaded per group, the loader's groups grow)
        self._lesson_days_cache: Dict[Tuple[Any, ...], List[date]] = {}
        self._loaded: Set[UUID] = set()

    # ---------------------------------------------------------------- selecting

    def select(
        self,
        *,
        membership_id: Optional[UUID] = None,
        membership_ids: Optional[Iterable[UUID]] = None,
        student_id: Optional[UUID] = None,
        group_id: Optional[UUID] = None,
        teacher_id: Optional[UUID] = None,
        search: Optional[str] = None,
        scope: str = "tracked",
    ) -> List[UUID]:
        """The ids of the memberships a list needs, in a deterministic order.

        ``tracked``: active memberships of active groups of students who are not archived (the status lists);
        ``all``: those plus every membership that still matters for the money (a stay since the accounting start, or any
        money record) - the debt / advance of students who left or were archived stays visible.
        """
        query = (
            self.db.query(StudentGroup.id)
            .join(Student, Student.id == StudentGroup.student_id)
            .join(Group, Group.id == StudentGroup.group_id)
            .filter(Student.org_id == self.org_id, Group.org_id == self.org_id)
        )
        tracked = and_(StudentGroup.is_active.is_(True), Group.status == "active", Student.status != "archived")
        if scope == "tracked":
            query = query.filter(tracked)
        elif scope == "all":
            start = datetime.combine(self.settings.accounting_start, datetime.min.time())
            since = (
                exists().where(
                    and_(
                        StudentGroupPeriod.membership_id == StudentGroup.id,
                        or_(StudentGroupPeriod.left_at.is_(None), StudentGroupPeriod.left_at >= start),
                    )
                )
            )
            money = or_(
                exists().where(and_(StudentPayment.membership_id == StudentGroup.id, StudentPayment.voided_at.is_(None))),
                exists().where(and_(StudentWriteoff.membership_id == StudentGroup.id, StudentWriteoff.voided_at.is_(None))),
                exists().where(and_(StudentRefund.membership_id == StudentGroup.id, StudentRefund.voided_at.is_(None))),
                exists().where(and_(MembershipOpeningBalance.membership_id == StudentGroup.id, MembershipOpeningBalance.voided_at.is_(None))),
                exists().where(and_(MembershipPause.membership_id == StudentGroup.id, MembershipPause.voided_at.is_(None))),
            )
            query = query.filter(or_(tracked, since, money))
        if membership_id:
            query = query.filter(StudentGroup.id == membership_id)
        if membership_ids is not None:
            query = query.filter(StudentGroup.id.in_(list(membership_ids)))
        if student_id:
            query = query.filter(StudentGroup.student_id == student_id)
        if group_id:
            query = query.filter(StudentGroup.group_id == group_id)
        if teacher_id:
            query = query.filter(Group.teacher_id == teacher_id)
        for word in (search or "").split():
            pattern = _like(word)
            query = query.filter(
                or_(
                    Student.first_name.ilike(pattern, escape="\\"),
                    Student.last_name.ilike(pattern, escape="\\"),
                    Student.display_id.ilike(pattern, escape="\\"),
                )
            )
        return [row[0] for row in query.all()]

    # ---------------------------------------------------------------- loading the facts

    def load_facts(self, ids: Iterable[UUID]) -> None:
        """Facts of ``ids`` and (transitively) of every membership linked to them by a transfer."""
        wanted = [i for i in dict.fromkeys(ids) if i not in self._loaded]
        for _ in range(MAX_TRANSFER_HOPS):
            if not wanted:
                break
            self._load_batch(wanted)
            linked: Set[UUID] = set()
            for mid in wanted:
                for t in self.t_in.get(mid, []) + self.t_out.get(mid, []):
                    linked.update((t.from_membership_id, t.to_membership_id))
            wanted = [i for i in linked if i not in self._loaded]
        if any(v.on for v in self.recalc_versions):       # after the last hop: the schedule is preloaded for ALL the groups at once
            self._load_cancels({g.id for _, _, g, _ in self.members.values()})
        self._prices()

    def _load_batch(self, ids: List[UUID]) -> None:
        rows = (
            self.db.query(StudentGroup, Student, Group, User)
            .join(Student, Student.id == StudentGroup.student_id)
            .join(Group, Group.id == StudentGroup.group_id)
            .outerjoin(User, User.id == Group.teacher_id)
            .filter(StudentGroup.id.in_(ids), Student.org_id == self.org_id, Group.org_id == self.org_id)
            .all()
        )
        found = [m.id for m, *_ in rows]
        for membership, student, group, teacher in rows:
            self.members[membership.id] = (membership, student, group, teacher)
        self._loaded.update(ids)
        for mid in ids:
            self.payments.setdefault(mid, [])
            self.writeoffs.setdefault(mid, [])
            self.refunds.setdefault(mid, [])
            self.pauses.setdefault(mid, [])
            self.openings.setdefault(mid, None)
            self.t_in.setdefault(mid, [])
            self.t_out.setdefault(mid, [])
        if not found:
            return
        periods = membership_periods.periods_by_membership(self.db, found)
        for mid in found:
            membership = self.members[mid][0]
            spans = [
                bl.Stay(ps.business_date(p.joined_at), ps.business_date(p.left_at) if p.left_at else None)
                for p in periods.get(mid, [])
            ]
            if not spans and membership.joined_at:  # a legacy row without periods
                spans = [bl.Stay(ps.business_date(membership.joined_at),
                                 None if membership.is_active or not membership.left_at else ps.business_date(membership.left_at))]
            self.stays[mid] = spans
        for payment in self.db.query(StudentPayment).filter(
            StudentPayment.org_id == self.org_id, StudentPayment.membership_id.in_(found),
            StudentPayment.voided_at.is_(None), StudentPayment.kind == "payment",
        ):
            self.payments[payment.membership_id].append(payment)
        for writeoff in self.db.query(StudentWriteoff).filter(
            StudentWriteoff.org_id == self.org_id, StudentWriteoff.membership_id.in_(found), StudentWriteoff.voided_at.is_(None)
        ):
            self.writeoffs[writeoff.membership_id].append(writeoff)
        for refund in self.db.query(StudentRefund).filter(
            StudentRefund.org_id == self.org_id, StudentRefund.membership_id.in_(found), StudentRefund.voided_at.is_(None)
        ):
            self.refunds[refund.membership_id].append(refund)
        for pause in self.db.query(MembershipPause).filter(
            MembershipPause.org_id == self.org_id, MembershipPause.membership_id.in_(found), MembershipPause.voided_at.is_(None)
        ):
            self.pauses[pause.membership_id].append(pause)
        for opening in self.db.query(MembershipOpeningBalance).filter(
            MembershipOpeningBalance.org_id == self.org_id, MembershipOpeningBalance.membership_id.in_(found),
            MembershipOpeningBalance.voided_at.is_(None),
        ):
            self.openings[opening.membership_id] = opening
        touched: Set[Tuple[str, UUID]] = set()
        for t in self.db.query(MembershipTransfer).filter(
            MembershipTransfer.org_id == self.org_id, MembershipTransfer.voided_at.is_(None),
            or_(MembershipTransfer.from_membership_id.in_(found), MembershipTransfer.to_membership_id.in_(found)),
        ):
            self.transfers[t.id] = t
            # a link is met again from its other end in a later hop: each side is indexed exactly once
            for side, mid, table in (("in", t.to_membership_id, self.t_in), ("out", t.from_membership_id, self.t_out)):
                if mid in self._loaded and (t.id, side) not in self._indexed:
                    self._indexed.add((t.id, side))
                    table[mid].append(t)
                    touched.add((side, mid))
        for side, mid in touched:
            (self.t_in if side == "in" else self.t_out)[mid].sort(key=lambda t: (t.at, str(t.id)))
        if self._needs_lessons():
            self._load_counted(found)

    def _load_cancels(self, group_ids: Set[UUID]) -> None:
        """PAY-F: the lessons the CENTRE cancelled (``cancellation_kind = 'centre'``; a holiday, a move and an old cancellation have another kind or none) - one query.

        Only a lesson that is IN THE PRICE OF THE MONTH is kept: one of the regular schedule (the pattern, ``validFrom`` ignored - the very lessons the divisor counts) or the
        copy of a move; an extra one-off lesson was never part of the month's price, so its cancellation costs nothing."""
        wanted = group_ids - self._cancels_loaded
        for gid in wanted:
            self.cancels[gid] = []
        self._cancels_loaded |= wanted
        if not wanted:
            return
        since = self.settings.accounting_start.isoformat()
        found: Dict[UUID, List[Tuple[str, str]]] = {}
        for group_id, day, lesson_id in self.db.query(GroupAttendance.group_id, GroupAttendance.lesson_date, GroupAttendance.lesson_id).filter(
            GroupAttendance.org_id == self.org_id, GroupAttendance.group_id.in_(wanted), GroupAttendance.status == "cancelled",
            GroupAttendance.cancellation_kind == "centre", GroupAttendance.lesson_date >= since,
        ):
            found.setdefault(group_id, []).append((day, lesson_id or ""))
        groups = {g.id: g for _, _, g, _ in self.members.values()}
        for group_id, items in found.items():
            group = groups.get(group_id)
            if group is None:
                continue
            last = max(date.fromisoformat(day) for day, _ in items)
            regular = {(o["occurrenceDate"], o["id"]) for o in self._occurrences(group, self.settings.accounting_start, last, pattern=True) if not o.get("isOneOff")}
            copies = {lesson_id for gid, lesson_id in self._moved_copies(group) if gid == str(group_id)}
            self.cancels[group_id] = [bl.CentreCancel(date.fromisoformat(day), lesson_id) for day, lesson_id in items if (day, lesson_id) in regular or lesson_id in copies]

    def _prices(self) -> None:
        self.books = pricing.load_books(
            self.db, {g.id for _, _, g, _ in self.members.values()}, list(self.members)
        )

    def _needs_lessons(self) -> bool:
        return any(r.mode == ps.LESSONS for r in self.settings.regimes)

    def _load_counted(self, ids: List[UUID]) -> None:
        from .payments_service import PaymentService  # lazy: the service imports this module

        floor = min(
            (r.effective_from for r in self.settings.regimes if r.mode == ps.LESSONS), default=self.settings.accounting_start
        )
        since = max(floor, self.settings.accounting_start)
        members = [(self.members[m][0], self.members[m][1], self.members[m][2]) for m in ids if m in self.members]
        detail: Dict[UUID, List[tuple]] = {}
        skip = {
            mid: {d for p in self.pauses.get(mid, []) for d in _days(p.first_day, p.last_day)} for mid in ids if self.pauses.get(mid)
        }
        PaymentService(self.db)._counted_lesson_dates(self.org_id, members, {}, since=since, detail_out=detail, exclude_days=skip)
        for mid in ids:
            self.counted[mid] = [bl.CountedLesson(day, str(lesson_id or "")) for day, lesson_id, _ in detail.get(mid, [])]

    # ---------------------------------------------------------------- the schedule (incomplete periods)

    def _occurrences(self, group: Group, start: date, end: date, *, pattern: bool = False) -> List[Dict[str, Any]]:
        """The resolved lessons of ``group`` in ``start .. end`` (cancelled ones included; ``pattern=True``: the FULL schedule pattern, the ``validFrom`` of the slots ignored)."""
        from .service import AuthService  # lazy: service is heavy

        if self._schedule is None:
            low = self.settings.accounting_start - timedelta(days=62)
            high = self.today + timedelta(days=SCHEDULE_HORIZON_DAYS)
            gids = list({g.id for _, _, g, _ in self.members.values()})
            self._schedule = AuthService(self.db)._preload_schedule_data(gids, date_from=low, date_to=high)
        if pattern:
            from . import schedule_rules

            with schedule_rules.ignoring_valid_from():
                return AuthService(self.db)._resolved_occurrences_for_group(group, date_from=start, date_to=end, preloaded=self._schedule)
        return AuthService(self.db)._resolved_occurrences_for_group(group, date_from=start, date_to=end, preloaded=self._schedule)

    def _lesson_days(self, group: Group, start: date, end: date, *, pattern: bool = False, regular: bool = False) -> List[date]:
        """The lessons of ``group`` in ``start .. end``.  ``pattern=True``: the group's FULL schedule pattern, ignoring the ``validFrom`` of the slots - the
        denominator of the proration of a partial period (a partial month costs price x the student's lessons / the lessons of a full month by the schedule,
        also for a group created mid-month).  The numerator (``pattern=False``) counts only real lessons.  Archive intervals are real absence of the group:
        they cut both.  ``regular=True`` (PAY-F, a cell of the «Общий месяц» from ``REGULAR_SCHEDULE_FROM``): the extra one-off lessons are left out, in the numerator and in the
        denominator alike (a copy of a move is left out in every case, a cancelled regular lesson stays)."""
        key = (group.id, start, end, pattern, regular)
        if key in self._lesson_days_cache:
            return self._lesson_days_cache[key]
        # LOGIC-A (S-4, decision 3): a lesson the CENTRE cancelled still counts - the month costs the same, so the share of an incomplete
        # first period never depends on cancellations (and a late cancellation no longer rewrites a closed charge)
        occurrences = self._occurrences(group, start, end, pattern=pattern)
        # LOGIC-B2 (decision 3, neutral move): a lesson moved by a substitution decision = the CANCELLED original (counted above) + a one-off copy;
        # the copy is left out, so a move never changes the share of an incomplete first period
        moved = self._moved_copies(group)
        days = [date.fromisoformat(item["occurrenceDate"]) for item in occurrences
                if not (item.get("isOneOff") and ((str(group.id), str(item["id"])) in moved or regular))]
        self._lesson_days_cache[key] = days
        return days

    def _moved_copies(self, group: Group) -> Set[Tuple[str, str]]:
        """``{(group id, lesson id)}`` of the one-off copies that a decision «перенос» of LOGIC-B2 made (loaded for all the loader's groups at once)."""
        if group.id not in self._moved_one_offs:
            from .payroll_hooks import moved_one_off_lesson_keys

            todo = [gid for gid in {g.id for _, _, g, _ in self.members.values()} | {group.id} if gid not in self._moved_one_offs]
            found = moved_one_off_lesson_keys(self.db, todo)
            for gid in todo:
                self._moved_one_offs[gid] = {k for k in found if k[0] == str(gid)}
        return self._moved_one_offs[group.id]

    # ---------------------------------------------------------------- the forecast of a package (PAY-F, part 1)

    def forecasts(self, entries: Sequence["MembershipLedger"]) -> Dict[UUID, Optional[date]]:
        """«Хватит до ~ДД.ММ» of the memberships in the lessons mode: the day of the N-th FUTURE lesson of the student, N = the lessons he has paid and not yet had.

        The future lessons are those of the group's schedule from today on: without the cancelled ones (a holiday, a sick teacher; a lesson that was moved counts at its
        new date: the original is cancelled, the copy is a one-off lesson), without the days of the student's pause and without the lessons already counted.  ``None``:
        not a package, nothing left (the package is over), the student is not in the group, or the schedule does not reach that far.  It is a promise of the
        schedule, not of the money - the front says «по расписанию»."""
        wanted = [e for e in entries if e.ledger.mode == bl.LESSONS and e.ledger.lessons is not None and e.ledger.lessons.left > 0
                  and e.membership.is_active and e.group.status == "active"]
        out: Dict[UUID, Optional[date]] = {e.membership.id: None for e in entries}
        if not wanted:
            return out
        horizon = self.today + timedelta(days=FORECAST_DAYS)
        gids = {e.group.id for e in wanted}
        cancelled: Dict[UUID, Set[Tuple[str, str]]] = {gid: set() for gid in gids}
        for group_id, day, lesson_id in self.db.query(GroupAttendance.group_id, GroupAttendance.lesson_date, GroupAttendance.lesson_id).filter(
            GroupAttendance.org_id == self.org_id, GroupAttendance.group_id.in_(gids), GroupAttendance.status.in_(("cancelled", "completed")),
            GroupAttendance.lesson_date >= self.today.isoformat(), GroupAttendance.lesson_date <= horizon.isoformat(),
        ):
            cancelled[group_id].add((day, lesson_id or ""))
        upcoming: Dict[UUID, List[Tuple[date, str]]] = {}
        for e in wanted:
            if e.group.id not in upcoming:
                upcoming[e.group.id] = sorted(
                    (date.fromisoformat(o["occurrenceDate"]), o["id"]) for o in self._occurrences(e.group, self.today, horizon)
                    if (o["occurrenceDate"], o["id"]) not in cancelled[e.group.id])
            held = {(c.day, c.lesson_id) for c in e.ledger.counted}
            paused = {d for p in e.pauses for d in _days(p.first_day, p.last_day)}
            coming = [d for d, lesson_id in upcoming[e.group.id] if (d, lesson_id) not in held and d not in paused]
            left = e.ledger.lessons.left
            out[e.membership.id] = coming[left - 1] if len(coming) >= left else None
        return out

    # ---------------------------------------------------------------- inputs

    def _stays_for(self, mid: UUID) -> List[bl.Stay]:
        """The stays of a membership; a stay that began with a transfer in starts the day AFTER it (that day belongs to the
        group the student came from - one day is never charged twice)."""
        stays = list(self.stays.get(mid, []))
        for t in self.t_in.get(mid, []):
            day = ps.business_date(t.at)
            moved: List[bl.Stay] = []
            for s in stays:
                if s.first == day:
                    if s.last is not None and s.last <= day:
                        continue  # the whole stay was the transfer day
                    moved.append(bl.Stay(day + timedelta(days=1), s.last))
                else:
                    moved.append(s)
            stays = moved
        return stays

    def x_of(self, transfer: MembershipTransfer) -> int:
        """The money a transfer carries: what could leave the old membership on the transfer day.  The old membership is
        seen as it stood JUST BEFORE this transfer (every link is ordered by ``(at, id)``, so the recursion has no cycle).
        Memoised, except a value computed while the cycle guard fired (it would be wrong and then stick)."""
        if transfer.id in self._x_memo:
            return self._x_memo[transfer.id]
        if transfer.id in self._x_active:
            self._guard_hits += 1
            return 0
        hits = self._guard_hits
        self._x_active.add(transfer.id)
        try:
            as_of = ps.business_date(transfer.at)
            inp = self.build_input(transfer.from_membership_id, as_of=as_of, before_transfer=transfer)
            value = bl.refundable_at(inp, as_of) if inp is not None else 0
        finally:
            self._x_active.discard(transfer.id)
        if self._guard_hits == hits:
            self._x_memo[transfer.id] = value
        return value

    def build_input(
        self,
        mid: UUID,
        *,
        as_of: Optional[date] = None,
        before_transfer: Optional[MembershipTransfer] = None,
        exclude_payment: Optional[UUID] = None,
        extra_credits: Sequence[bl.Credit] = (),
    ) -> Optional[bl.LedgerInput]:
        """The input of one membership.  ``as_of``: the account on that day (a slice for a transfer); ``before_transfer``:
        the transfers out that came after this one are not yet there; ``exclude_payment``: a payment being corrected."""
        if mid not in self.members:
            return None
        membership, student, group, _ = self.members[mid]
        credits: List[bl.Credit] = []
        for p in self.payments.get(mid, []):
            if p.amount and p.amount > 0 and p.id != exclude_payment:
                credits.append(bl.Credit(str(p.id), bl.PAYMENT, p.paid_on, p.created_at or datetime.min, int(p.amount)))
        for w in self.writeoffs.get(mid, []):
            credits.append(bl.Credit(str(w.id), bl.WRITEOFF, w.effective_on, w.created_at or datetime.min, int(w.amount)))
        outflows: List[bl.Outflow] = []
        for r in self.refunds.get(mid, []):
            outflows.append(bl.Outflow(str(r.id), bl.REFUND, r.effective_on, r.created_at or datetime.min, int(r.amount)))
        key = (before_transfer.at, str(before_transfer.id)) if before_transfer is not None else None
        for t in self.t_in.get(mid, []):
            if before_transfer is not None and (t.at, str(t.id)) >= key:
                continue  # a transfer in that came after (or with) the one being measured: not there yet
            if as_of is not None and ps.business_date(t.at) > as_of:
                continue
            amount = self.x_of(t)
            if amount > 0:
                credits.append(bl.Credit(f"tr:{t.id}", bl.TRANSFER_IN, ps.business_date(t.at), t.at, amount))
        for t in self.t_out.get(mid, []):
            if before_transfer is not None and (t.at, str(t.id)) >= key:
                continue
            if as_of is not None and ps.business_date(t.at) > as_of:
                continue
            amount = self.x_of(t)
            if amount > 0:
                outflows.append(bl.Outflow(f"tr:{t.id}", bl.TRANSFER_OUT, ps.business_date(t.at), t.at, amount))
        credits.extend(extra_credits)
        books = self.books

        def price_at(day: date, _m=mid, _g=membership.group_id) -> bl.PriceInfo:
            return price_info(books.price(_m, _g, day))

        today = as_of or self.today
        inp = bl.LedgerInput(
            today=today,
            accounting_start=self.settings.accounting_start,
            stays=tuple(self._stays_for(mid)),
            regimes=self.settings.regimes,
            price_at=price_at,
            credits=tuple(credits),
            outflows=tuple(outflows),
            lesson_days=lambda a, b, _g=group: self._lesson_days(_g, a, b),
            pattern_days=lambda a, b, _g=group: self._lesson_days(_g, a, b, pattern=True),
            regular_lesson_days=lambda a, b, _g=group: self._lesson_days(_g, a, b, regular=True),
            regular_pattern_days=lambda a, b, _g=group: self._lesson_days(_g, a, b, pattern=True, regular=True),
            regular_only_from=bl.REGULAR_SCHEDULE_FROM,
            counted_lessons=tuple(self.counted.get(mid, [])),
            expiring_days=self.settings.expiring_days,
            expiring_lessons=self.settings.expiring_lessons,
            pauses=tuple(bl.Pause(p.first_day, p.last_day) for p in self.pauses.get(mid, [])),
            opening=self._opening_of(mid),
            centre_cancels=tuple(self.cancels.get(group.id, ())),
            recalc_versions=self.recalc_versions,
        )
        return bl.sliced_input(inp, as_of) if as_of is not None else inp

    def _opening_of(self, mid: UUID) -> Optional[bl.Opening]:
        row = self.openings.get(mid)
        if row is None:
            return None
        return bl.Opening(row.paid_until, int(row.debt_amount or 0), row.debt_on)

    # ---------------------------------------------------------------- results

    def run(self, ids: Sequence[UUID]) -> List[MembershipLedger]:
        """Facts -> ledgers of ``ids`` (in order); ids that do not exist are skipped."""
        self.load_facts(ids)
        out: List[MembershipLedger] = []
        for mid in ids:
            if mid not in self.members:
                continue
            membership, student, group, teacher = self.members[mid]
            inp = self.build_input(mid)
            out.append(
                MembershipLedger(
                    membership=membership, student=student, group=group, teacher=teacher, inp=inp,
                    ledger=bl.run_ledger(inp), price=self.books.price(mid, membership.group_id, self.today),
                    payments=sorted(self.payments.get(mid, []), key=lambda p: (p.paid_on, p.created_at or datetime.min)),
                    writeoffs=list(self.writeoffs.get(mid, [])), refunds=list(self.refunds.get(mid, [])),
                    transfers_in=list(self.t_in.get(mid, [])), transfers_out=list(self.t_out.get(mid, [])),
                    credit_of_transfer={str(t.id): self.x_of(t) for t in self.t_in.get(mid, []) + self.t_out.get(mid, [])},
                    pauses=sorted(self.pauses.get(mid, []), key=lambda p: p.first_day), opening=self.openings.get(mid),
                    loader=self,
                )
            )
        return out

    def load(self, **select_kwargs) -> List[MembershipLedger]:
        return self.run(self.select(**select_kwargs))
