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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy import and_, exists, or_
from sqlalchemy.orm import Session

from src.models.auth import (
    AttendanceExcuse,
    Group,
    GroupAttendance,
    GroupScheduleOneOffLesson,
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
from . import business_clock as bc
from . import membership_periods
from . import payment_status as ps
from . import pricing
from .sql_text import contains

logger = logging.getLogger(__name__)

MAX_TRANSFER_HOPS = 8
SCHEDULE_HORIZON_DAYS = 800  # the preload of the schedule reaches this far past today (24 future periods + slack)
FORECAST_DAYS = 400          # how far ahead the forecast of a package looks for the day of its last lesson (a package of 57 weekly lessons fits)


# ============================================================================================================
# #154: a make-up and the deduction of the lesson it makes up for
# ============================================================================================================


@dataclass(frozen=True)
class MakeupFacts:
    """The facts of ONE make-up that the deduction of a cancelled lesson waits for.  ``ahead``: the make-up is still to come or is going on - the deduction is paused for every student of the group;
    ``held``: it was held - it is lifted for the students it COUNTED for (``lifted``: in its roster with a mark that counts by rule 4.3: present / late / absent, an excused one without a confirmed
    excuse), the others keep it; ``missed``: its day has passed and the lesson is not closed (no journal row, or one left ``in_progress``) - not held, nothing is lifted (the journal entered later makes it ``held``); ``gone``: it was cancelled / deleted (the pointer is released by the cancellation; a stale one is no reason to lift anything)."""

    state: str
    day: Optional[date] = None
    lifted: frozenset = frozenset()

    def lifts(self, student_id: str, paused_days: Set[date]) -> bool:
        if self.state == "ahead":
            return True
        if self.state != "held":
            return False
        return student_id in self.lifted and (self.day is None or self.day not in paused_days)


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



def _moment(raw: Any) -> Optional[datetime]:
    """A naive UTC timestamp from the settings (ISO text), else None (an entry of an older version has none)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def first_day_of_period(period) -> date:
    """The first day a stay is CHARGED from (Money-1 B1 «Оплата с»): ``billing_from``, else the business day he joined.  ONE function for the account, the table of the openings and the batch of
    the openings (R3 code №19: two «first days» of one stay)."""
    return period.billing_from or ps.business_date(period.joined_at)


def stay_of_period(period) -> Optional[bl.Stay]:
    """The ledger stay of a membership period.  A stay that ended BEFORE the day he was to be charged from (written down on 21.10 «с 06.11», taken out on 22.10) charges NOTHING (R3 code №2 / money R-1:
    ``merge_stays`` used to turn the upside-down stay into the one day of ``first`` - a ghost debt of 10 000 for a student who never came)."""
    first = first_day_of_period(period)
    last = ps.business_date(period.left_at) if period.left_at else None
    if last is not None and last < first:
        return None
    return bl.Stay(first, last, period.joined_at)


def stays_of_membership(membership: StudentGroup, periods: Sequence) -> List[bl.Stay]:
    """The ledger stays of a membership from its periods; a legacy row without periods is one stay by its own dates (NOT a membership whose only stay charges
    nothing: R3 №2)."""
    spans = [stay for stay in (stay_of_period(p) for p in periods) if stay is not None]
    if not periods and membership.joined_at:
        return [bl.Stay(ps.business_date(membership.joined_at),
                        None if membership.is_active or not membership.left_at else ps.business_date(membership.left_at), membership.joined_at)]
    return spans


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
        at=_moment(block.get("at")),   # batch 25.10: when it was saved - a student who joins after it has no running period
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
        self.makeups: Dict[Tuple[UUID, date, str], MakeupFacts] = {}       # #154: (group, day, lesson) of a cancelled lesson that has a make-up -> its facts
        self.members: Dict[UUID, Tuple[StudentGroup, Student, Group, Optional[User]]] = {}
        self.stays: Dict[UUID, List[bl.Stay]] = {}
        self.left_days: Dict[UUID, Dict[UUID, Set[date]]] = {}   # student id -> membership id -> the days he LEFT that membership (Money-1 G-8)
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
        self._schedule: Optional[Any] = None          # the one-off lessons and overrides of the groups in ``_scheduled`` (it grows with every portion, see ``_occurrences``)
        self._scheduled: Set[UUID] = set()
        self.other_groups: Dict[UUID, Group] = {}          # the groups of the other memberships of the students (Р-10: where they left a day), not loaded as facts
        self._moved_one_offs: Dict[Any, Set[Tuple[str, str]]] = {}   # group id -> keys (loaded per group, the loader's groups grow)
        self._lesson_days_cache: Dict[Tuple[Any, ...], List[date]] = {}
        self._loaded: Set[UUID] = set()
        self._links_at: Dict[Tuple[UUID, Any], Dict[UUID, MembershipTransfer]] = {}      # (student, instant) -> the links loaded so far
        self._link_ranks: Dict[Tuple[UUID, Any], Tuple[Tuple[int, int], Dict[UUID, int]]] = {}      # (student, instant of the links) -> (what was known when it was worked out, link id -> place)

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
            start = bc.business_wall_to_utc(datetime.combine(self.settings.accounting_start, datetime.min.time()))   # MN-25: left_at is UTC, the start day is a Tashkent day
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
            pattern = contains(word)
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
            self.stays[mid] = stays_of_membership(self.members[mid][0], periods.get(mid, []))
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
            self._links_at.setdefault((t.student_id, t.at), {})[t.id] = t
            # a link is met again from its other end in a later hop: each side is indexed exactly once
            for side, mid, table in (("in", t.to_membership_id, self.t_in), ("out", t.from_membership_id, self.t_out)):
                if mid in self._loaded and (t.id, side) not in self._indexed:
                    self._indexed.add((t.id, side))
                    table[mid].append(t)
                    touched.add((side, mid))
        for side, mid in touched:
            (self.t_in if side == "in" else self.t_out)[mid].sort(key=self.link_key)
        if self._needs_lessons():
            self._load_counted([self.members[m][:3] for m in found])
        self._load_left_days(found, periods)       # after the counted lessons: for a package the day he left belongs to the group that COUNTED a lesson of his that day

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
        made: Dict[Tuple[UUID, str, str], str] = {}
        for group_id, day, lesson_id, made_up_by in self.db.query(GroupAttendance.group_id, GroupAttendance.lesson_date, GroupAttendance.lesson_id, GroupAttendance.made_up_by).filter(
            GroupAttendance.org_id == self.org_id, GroupAttendance.group_id.in_(wanted), GroupAttendance.status == "cancelled",
            GroupAttendance.cancellation_kind == "centre", GroupAttendance.lesson_date >= since,
        ):
            found.setdefault(group_id, []).append((day, lesson_id or ""))
            if made_up_by:
                made[(group_id, day, lesson_id or "")] = made_up_by
        if made:
            self._load_makeups(made)
        groups = {g.id: g for _, _, g, _ in self.members.values()}
        copies_of = self._cost_copies_of({gid: {lesson_id for _day, lesson_id in items} for gid, items in found.items() if gid in groups}, groups)      # wave C: one query for all the groups
        for group_id, items in found.items():
            group = groups.get(group_id)
            if group is None:
                continue
            last = max(date.fromisoformat(day) for day, _ in items)
            regular = {(o["occurrenceDate"], o["id"]) for o in self._occurrences(group, self.settings.accounting_start, last, pattern=True) if not o.get("isOneOff")}
            copies = copies_of.get(group_id, set())
            self.cancels[group_id] = [bl.CentreCancel(date.fromisoformat(day), lesson_id) for day, lesson_id in items if (day, lesson_id) in regular or lesson_id in copies]

    def _cost_copies_of(self, wanted: Dict[UUID, Set[str]], groups: Dict[UUID, Group]) -> Dict[UUID, Set[str]]:
        """``{group id: the copies of a move among its ``wanted`` lesson ids whose cancellation costs the students}``: ONLY ``purpose = 'moved'`` (the copy of a regular lesson, which the month is priced by).  The copy of a move of an
        additional lesson or of a make-up is a one-off lesson of the other purpose: it was never in the price of the month (LESSONS 53).  One query for all the groups (wave C: it was one per group)."""
        candidates = {gid: {lesson_id for g, lesson_id in self._moved_copies(groups[gid]) if g == str(gid) and lesson_id in ids} for gid, ids in wanted.items()}
        candidates = {gid: copies for gid, copies in candidates.items() if copies}
        if not candidates:
            return {}
        out: Dict[UUID, Set[str]] = {}
        for gid, lesson_id in self.db.query(GroupScheduleOneOffLesson.group_id, GroupScheduleOneOffLesson.id).filter(
            GroupScheduleOneOffLesson.group_id.in_(list(candidates)), GroupScheduleOneOffLesson.id.in_(set().union(*candidates.values())), GroupScheduleOneOffLesson.purpose == "moved"):
            if lesson_id in candidates[gid]:
                out.setdefault(gid, set()).add(lesson_id)
        return out

    def _load_makeups(self, made: Dict[Tuple[UUID, str, str], str]) -> None:
        """#154: the facts of the make-ups of the cancelled lessons ``made`` (``{(group, day, lesson): make-up lesson id}``): the state of the make-up and - when it was held - the students it counted for
        (the very predicate of the package: in the roster, a mark of rule 4.3 and no confirmed excuse).  A few queries whatever the number of make-ups."""
        ids = set(made.values())
        gids = {k[0] for k in made}
        ones = {o.id: o for o in self.db.query(GroupScheduleOneOffLesson).filter(GroupScheduleOneOffLesson.id.in_(ids), GroupScheduleOneOffLesson.group_id.in_(gids)).all()}
        rows = {(r.group_id, r.lesson_id, r.lesson_date): r for r in self.db.query(GroupAttendance).filter(
            GroupAttendance.org_id == self.org_id, GroupAttendance.group_id.in_(gids), GroupAttendance.lesson_id.in_(ids)).all()}
        held_ids = [r.id for r in rows.values() if r.status == "completed"]
        confirmed = {(r.attendance_id, str(r.student_id)) for r in self.db.query(AttendanceExcuse.attendance_id, AttendanceExcuse.student_id).filter(
            AttendanceExcuse.org_id == self.org_id, AttendanceExcuse.attendance_id.in_(held_ids), AttendanceExcuse.status == "confirmed").all()} if held_ids else set()
        for (group_id, day, lesson_id), make_up in made.items():
            one = ones.get(make_up)
            row = rows.get((group_id, make_up, one.lesson_date)) if one is not None and one.group_id == group_id else None
            key = (group_id, date.fromisoformat(day), lesson_id)
            if one is None:
                self.makeups[key] = MakeupFacts("gone")
            elif one.lesson_date >= self.today.isoformat() and (row is None or row.status == "in_progress"):
                self.makeups[key] = MakeupFacts("ahead")      # still to come (or going on today)
            elif row is None or row.status == "in_progress":
                self.makeups[key] = MakeupFacts("missed")      # the day has passed and the lesson was never closed (no row, or started and left open): «не проведена», the deduction stays; closed later, the facts lift it (LESSONS 54)
            elif row.status == "cancelled":
                self.makeups[key] = MakeupFacts("gone")
            else:
                records = row.records if isinstance(row.records, dict) else {}
                roster = {str(x.get("student_id")) for x in row.roster_snapshot if isinstance(x, dict) and x.get("student_id")} if isinstance(row.roster_snapshot, list) else set(records)
                lifted = frozenset(sid for sid in roster if ps.lesson_counts(mark=records.get(sid), excuse_status="confirmed" if (row.id, sid) in confirmed else None))
                self.makeups[key] = MakeupFacts("held", date.fromisoformat(one.lesson_date), lifted)

    def _cancels_for(self, mid: UUID, group: Group, student_id: UUID) -> Tuple[bl.CentreCancel, ...]:
        """The lessons the centre cancelled that are DEDUCTED from THIS student: those without a make-up, and those whose make-up does not count for him (he was not there, left the group, was on a pause,
        was excused with a confirmed excuse).  A function of the facts, per student - nothing is stored (LESSONS 18)."""
        sid = str(student_id)
        paused = {d for p in self.pauses.get(mid, []) for d in _days(p.first_day, p.last_day)} if any(k[0] == group.id for k in self.makeups) else set()
        out = []
        for cancel in self.cancels.get(group.id, ()):
            facts = self.makeups.get((group.id, cancel.day, cancel.lesson_id))
            if facts is not None and facts.lifts(sid, paused):
                continue
            out.append(cancel)
        return tuple(out)

    def _prices(self) -> None:
        self.books = pricing.load_books(
            self.db, {g.id for _, _, g, _ in self.members.values()}, list(self.members)
        )

    def _needs_lessons(self) -> bool:
        return any(r.mode == ps.LESSONS for r in self.settings.regimes)

    def _load_counted(self, members: Sequence[Tuple[StudentGroup, Any, Group]]) -> None:
        """The counted lessons of the given memberships (``self.counted``): the facts of the loaded ones and the days a student left the other memberships (``_load_left_days``)."""
        from .payments_service import PaymentService  # lazy: the service imports this module

        floor = min(
            (r.effective_from for r in self.settings.regimes if r.mode == ps.LESSONS), default=self.settings.accounting_start
        )
        since = max(floor, self.settings.accounting_start)
        ids = [m.id for m, _, _ in members]
        detail: Dict[UUID, List[tuple]] = {}
        skip = {
            mid: {d for p in self.pauses.get(mid, []) for d in _days(p.first_day, p.last_day)} for mid in ids if self.pauses.get(mid)
        }
        PaymentService(self.db)._counted_lesson_dates(self.org_id, members, since=since, detail_out=detail, exclude_days=skip)
        for mid in ids:
            self.counted[mid] = [bl.CountedLesson(day, str(lesson_id or "")) for day, lesson_id, _ in detail.get(mid, [])]

    # ---------------------------------------------------------------- the schedule (incomplete periods)

    def _occurrences(self, group: Group, start: date, end: date, *, pattern: bool = False) -> List[Dict[str, Any]]:
        """The resolved lessons of ``group`` in ``start .. end`` (cancelled ones included; ``pattern=True``: the FULL schedule pattern, the ``validFrom`` of the slots ignored)."""
        from .service import AuthService  # lazy: service is heavy

        todo = [gid for gid in {g.id for _, _, g, _ in self.members.values()} | set(self.other_groups) | {group.id} if gid not in self._scheduled]
        if todo:            # Д-4: the loader is run in portions (the night reconciliation: 150 memberships at a time) - the groups of a later portion are read too, with their one-offs and overrides
            low = self.settings.accounting_start - timedelta(days=62)
            high = self.today + timedelta(days=SCHEDULE_HORIZON_DAYS)
            found = AuthService(self.db)._preload_schedule_data(todo, date_from=low, date_to=high)
            if self._schedule is None:
                self._schedule = found
            else:
                self._schedule.one_offs.update(found.one_offs)
                self._schedule.overrides.update(found.overrides)
            self._scheduled.update(todo)
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
                if not (item.get("isOneOff") and ((str(group.id), str(item["id"])) in moved or regular or item.get("purpose") in ("extra", "makeup")))]      # #154: an additional lesson / a make-up (its cancelled lesson is counted) change nothing
        self._lesson_days_cache[key] = days
        return days

    def _moved_copies(self, group: Group) -> Set[Tuple[str, str]]:
        """``{(group id, lesson id)}`` of the one-off copies that a decision «перенос» of LOGIC-B2 made (loaded for all the loader's groups at once)."""
        if group.id not in self._moved_one_offs:
            from .payroll_hooks import moved_one_off_lesson_keys

            todo = [gid for gid in {g.id for _, _, g, _ in self.members.values()} | set(self.other_groups) | {group.id} if gid not in self._moved_one_offs]
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
                    if (o["occurrenceDate"], o["id"]) not in cancelled[e.group.id] and o.get("purpose") != "extra")      # #154: an additional lesson does not spend the package
            held = {(c.day, c.lesson_id) for c in e.ledger.counted}
            paused = {d for p in e.pauses for d in _days(p.first_day, p.last_day)}
            coming = [d for d, lesson_id in upcoming[e.group.id] if (d, lesson_id) not in held and d not in paused]
            left = e.ledger.lessons.left
            out[e.membership.id] = coming[left - 1] if len(coming) >= left else None
        return out

    # ---------------------------------------------------------------- inputs

    def _load_left_days(self, found: List[UUID], periods: Dict[UUID, list]) -> None:
        """Money-1 G-8: the days a student left a group of the centre, for EVERY group of his (the other memberships are not loaded as facts, one query for their periods)."""
        students = {self.members[mid][1].id for mid in found}
        rows = self.db.query(StudentGroup, Group).join(Group, Group.id == StudentGroup.group_id).filter(StudentGroup.student_id.in_(students), Group.org_id == self.org_id).all()      # Р-10: the groups come with the memberships, and only the groups of THIS centre
        self.other_groups.update({g.id: g for _, g in rows})          # their lessons are loaded together with the lessons of the loaded groups (``_occurrences``, ``_moved_copies``)
        periods = {**periods, **membership_periods.periods_by_membership(self.db, [m.id for m, _ in rows if m.id not in periods])}
        if self._needs_lessons():
            self._load_counted([(m, None, g) for m, g in rows if m.id not in self.counted])
        for membership, group in rows:
            runs = bl.merge_stays(stays_of_membership(membership, periods.get(membership.id, [])))
            for run in runs:
                # G-8b / Р-6: the day he left belongs to that group only when it really charges the day (a stay of one day without a lesson charges nothing, G-7)
                if run[1] is not None and not bl.lessonless_day(lambda a, b, _g=group: self._lesson_days(_g, a, b), run) and self._holds_day(group, run[1], membership.id):
                    self.left_days.setdefault(membership.student_id, {}).setdefault(membership.id, set()).add(run[1])

    def _holds_day(self, group: Group, day: date, mid: UUID) -> bool:
        """Money-1 Р-6 (R3 В5, Д-1): does the membership ``mid`` in ``group`` itself charge ``day``?  By a per-day share (the calendar mode without a schedule to count by) always; by lessons
        (a fixed billing day with a schedule) only when the group has a lesson that day; a PACKAGE charges only a lesson it COUNTED for the student (he is in its roster, he is marked present / late /
        absent without a confirmed excuse), so a later lesson of the day he was moved out of, a lesson the centre cancelled or an excused one holds nothing.  The ONE rule of «the day he left
        belongs to the old group» for a transfer and for a manual removal + add: a day the old group does not charge is charged by the new one, so it is neither paid twice nor lost."""
        regime = bl.regime_at(self.settings.regimes, day)
        if regime.mode == ps.LESSONS:
            return any(c.day == day for c in self.counted.get(mid, ()))
        if regime.mode == ps.CALENDAR:
            if regime.anchor == ps.PAYMENT_DATE:
                return True
            cell = (ps.billing_period_start(day, regime.billing_day), ps.billing_period_end(day, regime.billing_day))
            if not self._lesson_days(group, *cell, pattern=True):
                return True
        return bool(self._lesson_days(group, day, day))

    @staticmethod
    def _begin_after(stays: List[bl.Stay], days: Set[date]) -> List[bl.Stay]:
        """A stay that begins on one of ``days`` begins the day AFTER it; a stay that was only that day is nothing."""
        moved: List[bl.Stay] = []
        for s in stays:
            if s.first not in days:
                moved.append(s)
            elif s.last is None or s.last > s.first:
                moved.append(bl.Stay(s.first + timedelta(days=1), s.last))
        return moved

    def _stays_for(self, mid: UUID) -> List[bl.Stay]:
        """The stays of a membership.  The day a student left another group of the centre belongs to THAT group: a stay that began with a transfer in
        (Money-1 B6) or on the day of a manual «removed from A, added to B» (G-8) starts the day AFTER it - one day is never charged twice."""
        stays = list(self.stays.get(mid, []))
        for t in self.t_in.get(mid, []):
            day = ps.business_date(t.at)
            old = self.members.get(t.from_membership_id)           # not loaded (beyond the hops of the loader): nothing is known against the day, it stays the old group's
            if old is None or self._holds_day(old[2], day, t.from_membership_id):
                stays = self._begin_after(stays, {day})
        elsewhere = {d for other, days in self.left_days.get(self.members[mid][1].id, {}).items() if other != mid for d in days}
        return self._begin_after(stays, elsewhere)

    def link_key(self, link: MembershipTransfer) -> Tuple[Any, int]:
        """The place of a link of a student in time: ``(at, place among the links of the same instant)``.  Links are ordered by ``at``; when several of one student have the SAME ``at`` (a frozen clock:
        the seeding and the tests; a real instant has microseconds) the order is the CHAIN of the money, never a random uuid (stage walk D-6: «Г2 -> Г1 -> Г2» at one reading of the clock carried
        0 on the second transfer when the id of the first sorted after it, and the money stayed on a closed account): a link whose ``from`` is the ``to`` of another goes after it; in a cycle
        (G2 -> G1 and G1 -> G2) the one whose ``to`` is the account the student is in NOW goes last; what is left is ordered by ``(created_at, from, to, id)``.  ``x_of`` measures a link
        by the links BEFORE it in this order."""
        slot = (link.student_id, link.at)
        same = list(self._links_at.get(slot, {link.id: link}).values())
        stamp = (len(same), sum(1 for m in {e for t in same for e in (t.from_membership_id, t.to_membership_id)} if m in self.members))       # the links and the accounts known: both grow while the loader hops
        known = self._link_ranks.get(slot)
        if known is None or known[0] != stamp:
            known = (stamp, {t.id: place for place, t in enumerate(self._chain_order(same))})
            self._link_ranks[slot] = known
        return link.at, known[1].get(link.id, len(same))

    def _chain_order(self, links: List[MembershipTransfer]) -> List[MembershipTransfer]:
        """The links of one student at one instant, the first one first (see ``link_key``)."""
        if len(links) < 2:
            return list(links)
        active = {t.to_membership_id for t in links if t.to_membership_id in self.members and self.members[t.to_membership_id][0].is_active}
        remaining = sorted(links, key=lambda t: (t.created_at or datetime.min, str(t.from_membership_id), str(t.to_membership_id), str(t.id)))
        ordered: List[MembershipTransfer] = []
        while remaining:
            ready = [t for t in remaining if not any(o is not t and o.to_membership_id == t.from_membership_id for o in remaining)]
            if not ready:                                       # a cycle: the account the student is in now is the target of the LAST link
                ready = [t for t in remaining if t.to_membership_id not in active] or remaining
            ordered.append(ready[0])
            remaining.remove(ready[0])
        return ordered

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
        key = self.link_key(before_transfer) if before_transfer is not None else None
        for t in self.t_in.get(mid, []):
            if before_transfer is not None and self.link_key(t) >= key:
                continue  # a transfer in that came after (or with) the one being measured: not there yet
            if as_of is not None and ps.business_date(t.at) > as_of:
                continue
            amount = self.x_of(t)
            if amount > 0:
                credits.append(bl.Credit(f"tr:{t.id}", bl.TRANSFER_IN, ps.business_date(t.at), t.at, amount))
        for t in self.t_out.get(mid, []):
            if before_transfer is not None and self.link_key(t) >= key:
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
            first_priced_day=lambda day, _m=mid, _g=membership.group_id: books.first_priced_day(_m, _g, day),
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
            opening=self._opening_of(mid, as_of=as_of, before_transfer=before_transfer),
            centre_cancels=self._cancels_for(mid, group, membership.student_id),
            recalc_versions=self.recalc_versions,
        )
        return bl.sliced_input(inp, as_of) if as_of is not None else inp

    def _opening_of(self, mid: UUID, *, as_of: Optional[date] = None, before_transfer: Optional[MembershipTransfer] = None) -> Optional[bl.Opening]:
        """The opening of a membership as the ledger sees it: its own row plus the «оплачено до» that came with a TRANSFER (Money-1 B6, B7 of the devil)."""
        row = self.openings.get(mid)
        carried = self._carried_paid_until(mid, as_of=as_of, before_transfer=before_transfer)
        if row is None and carried is None:
            return None
        if row is None:
            return bl.Opening(paid_until=carried)
        paid_until = max((d for d in (row.paid_until, carried) if d is not None), default=None)
        return bl.Opening(paid_until, int(row.debt_amount or 0), row.debt_on)

    def _own_paid_until(self, mid: UUID) -> Optional[date]:
        row = self.openings.get(mid)
        return row.paid_until if row is not None else None

    def _carried_paid_until(self, mid: UUID, *, as_of: Optional[date] = None, before_transfer: Optional[MembershipTransfer] = None, _hops: int = 0) -> Optional[date]:
        """Money-1 B6: the days the student had PAID FOR WITHOUT MONEY (the «оплачено до» of an opening balance - a note of a notebook) travel with him: a transfer on 25.10 from a group that was
        paid until 20.11 gives the new group a student who is paid until 20.11 (the money is carried as an advance by ``x_of``; before, the days were lost and he owed the new group at once).  Derived like
        the money (never stored): voiding the transfer takes it away.  A chain A -> B -> C carries the same days along; only days after the transfer day count (the day itself belongs to the old group)."""
        if _hops >= MAX_TRANSFER_HOPS:
            return None
        key = self.link_key(before_transfer) if before_transfer is not None else None
        best: Optional[date] = None
        for t in self.t_in.get(mid, []):
            if before_transfer is not None and self.link_key(t) >= key:
                continue
            if as_of is not None and ps.business_date(t.at) > as_of:
                continue
            until = self.paid_days_of_link(t, _hops=_hops + 1)
            if until is not None and (best is None or until > best):
                best = until
        return best

    def paid_days_of_link(self, t: MembershipTransfer, *, _hops: int = 0) -> Optional[date]:
        """The «оплачено до» that ONE transfer brings (see ``_carried_paid_until``): the day the old membership was paid until WITHOUT money, if it is after the transfer day; else ``None``."""
        src, day = t.from_membership_id, ps.business_date(t.at)
        until = max((d for d in (self._own_paid_until(src), self._carried_paid_until(src, before_transfer=t, _hops=_hops)) if d is not None), default=None)
        return until if until is not None and until > day else None

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
