"""
ALGO - LOGIC-A: the opening balance of a membership (decision 1) and the pause of a student (decision 2).

Both are FACTS read by ``ledger_service`` into ``billing_ledger`` (``Opening`` / ``Pause``) - nothing is "recalculated" when they are
created or cancelled; the next call of the ledger gives the new answer.  Every write: the family lock (student -> memberships by id, the order
of ``payments_service``), the ledger "before / after" for the impact, one ``group_audit`` row, one transaction.  ``dry_run`` does the whole
write inside the transaction, measures the impact and rolls back.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID, uuid4

from sqlalchemy import func

from src.models.auth import (
    Group,
    GroupAttendance,
    GroupAudit,
    MembershipOpeningBalance,
    MembershipPause,
    MembershipTransfer,
    Organization,
    PayrollSheet,
    StudentGroup,
    StudentPayment,
    StudentRefund,
    StudentWriteoff,
)

from . import billing_ledger as bl
from .closed_period import ClosedPeriods, group_teacher_ids
from .ops_rules import OpsError
from . import membership_periods
from . import payment_status as ps
from . import payments_texts
from .payments_schemas import (
    ImpactMoney,
    MAX_MONEY,
    OpeningOut,
    OpeningPut,
    OpeningSaved,
    OpeningVoided,
    PauseCreate,
    PauseLessonMark,
    PauseLessons,
    PauseOut,
    PausePatch,
    PausePayroll,
    PauseSaved,
    PauseVoided,
)
from .payments_service import (
    AlreadyVoided,
    AmountRange,
    OpeningOwnerOnly,
    PaymentConflict,
    PaymentError,
    PaymentInvalid,
    PaymentNotFound,
    PaymentService,
    ReasonRequired,
    _iso,
    _person,
    _Moved,
    _utcnow,
    deadlock_guard,
    invalidate_summary,
)

logger = logging.getLogger(__name__)

AUDIT_OPENING_SET = "opening_balance_set"
AUDIT_OPENING_VOID = "opening_balance_void"
AUDIT_PAUSE_SET = "membership_pause_set"
AUDIT_PAUSE_VOID = "membership_pause_void"
PAUSE_MAX_DAYS = 366
OPENING_AHEAD_DAYS = 366
OPENING_MIN_DATE = date(2020, 1, 1)


# ---- errors (the codes of the contract) -----------------------------------------------------------------------------


class OpeningRange(PaymentInvalid):
    code = "OPENING_RANGE"


class OpeningNotAllowed(PaymentConflict):
    code = "OPENING_NOT_ALLOWED"


class PauseRange(PaymentInvalid):
    code = "PAUSE_RANGE"


class PauseLocked(PaymentInvalid):
    code = "PAUSE_LOCKED"


class PauseOverlap(PaymentConflict):
    code = "PAUSE_OVERLAP"


class PauseLessonInProgress(PaymentConflict):
    """Money-1 A6 (ОШ-1): a lesson of the group is going on inside the pause - its roster is frozen like for a transfer / an archive, or the student would be marked after the pause was put."""

    code = "LESSON_IN_PROGRESS"


class PauseOverlapsAttended(PaymentConflict):
    code = "PAUSE_OVERLAPS_ATTENDED"


class PauseBreaksRefund(PaymentConflict):
    code = "PAUSE_BREAKS_REFUND"


class MembershipClosed(PaymentConflict):
    code = "MEMBERSHIP_CLOSED"


def impact_of(before: bl.Ledger, after: bl.Ledger) -> ImpactMoney:
    return ImpactMoney(
        debt_before=before.debt, debt_after=after.debt, advance_before=before.advance, advance_after=after.advance,
        paid_until_before=before.covered_through, paid_until_after=after.covered_through,
        charged_delta=after.charged - before.charged, refundable_before=before.refundable, refundable_after=after.refundable,
    )


def launch_closed(state: str, state_by: Optional[str]) -> bool:
    """R3d Д2: the launch of the accounting is CLOSED when a HUMAN finished it (``done`` / ``skipped``, ``openings_state_by`` is not ``"auto"``); the automatic ``done`` (the list is filled: the last «не заполнено»
    row is gone) closes nothing - the typo of the last row is still the administrator's to correct."""
    return state in ("done", "skipped") and state_by != "auto"


def opening_allowed(closed: bool, actor_role: str) -> bool:
    """R3c Г1 / R3d Д2: THE one rule of the opening balance - the owner always; any other role (an administrator with the right to money: the routes check it) only while the launch of the accounting is not
    closed by a human (``launch_closed``).  Every road of the opening balance and the field ``can_set_opening`` of the screens ask this function."""
    return actor_role == "owner" or not closed


class _Base:
    def __init__(self, db) -> None:
        self.db = db
        self.pay = PaymentService(db)

    def opening_state(self, org_id: str) -> str:
        """``payments.openings_state`` of the centre (``none`` when it was never set or holds an unknown word)."""
        settings = self.db.query(Organization.settings).filter(Organization.id == org_id).scalar() or {}
        value = (settings.get("payments") or {}).get("openings_state")
        return value if value in ("none", "pending", "done", "skipped") else "none"

    def is_launch_closed(self, org_id: str) -> bool:
        """R3d Д2: has a human finished the launch of the accounting of the centre (``launch_closed`` of its settings block)."""
        block = (self.db.query(Organization.settings).filter(Organization.id == org_id).scalar() or {}).get("payments") or {}
        return launch_closed(self.opening_state(org_id), block.get("openings_state_by"))

    def check_opening_allowed(self, org_id: str, actor_role: str, closed: Optional[bool] = None) -> None:
        if not opening_allowed(closed if closed is not None else self.is_launch_closed(org_id), actor_role):
            raise OpeningOwnerOnly("Учёт запущен — остаток на начало вносит и меняет владелец")

    def _first_stay_day(self, membership: StudentGroup) -> date:
        periods = membership_periods.periods_by_membership(self.db, [membership.id]).get(membership.id, [])
        if periods:
            from .ledger_service import first_day_of_period      # the one «first day» of a stay (R3 code №19): «Оплата с» when it was given

            return first_day_of_period(periods[0])
        return ps.business_date(membership.joined_at) if membership.joined_at else ps.business_today()

    def _has_money_records(self, membership_id: UUID) -> bool:
        for model in (StudentPayment, StudentWriteoff, StudentRefund):
            query = self.db.query(func.count(model.id)).filter(model.membership_id == membership_id, model.voided_at.is_(None))
            if model is StudentPayment:
                query = query.filter(StudentPayment.kind == "payment")
            if query.scalar():
                return True
        return False

    def _audit(self, org_id: str, membership: StudentGroup, actor_id: str, actor_role: str, action: str, snapshot: Dict[str, Any]) -> None:
        self.pay._audit(org_id, membership, actor_id, actor_role, action, snapshot)

    def _row(self, org_id: str, membership_id: UUID, today: date):
        return self.pay._rows([self.pay._entry_of(org_id, membership_id, today)])[0]


# ============================================================================================================
# Opening balance
# ============================================================================================================


@dataclass
class OpeningFacts:
    """What ``OpeningService.apply`` reads about ONE membership, loaded once for a whole batch (see ``openings_service``)."""

    first_day: date
    accounting_start: date
    existing: Optional[MembershipOpeningBalance]
    has_money: bool
    transfer_target: bool


def over_payments_warning(paid_total: int, covered_until: Optional[date]) -> Optional[Dict[str, str]]:
    """Money-1 R3 Д4: the ONE function of the warning «У ученика уже есть оплаты на N сум — покрытие станет до ДД.ММ» (the batch of openings, the single window, the adding to a group)."""
    if paid_total <= 0 or covered_until is None:
        return None
    from . import payments_texts as tx

    return {"code": "OPENING_OVER_PAYMENTS", "message": tx.opening_over_payments_text(int(paid_total), covered_until)}


class OpeningService(_Base):
    def _out(self, row: MembershipOpeningBalance) -> OpeningOut:
        users = self.pay._users([row.created_by])
        return OpeningOut(
            id=str(row.id), membership_id=str(row.membership_id), paid_until=row.paid_until,
            debt_amount=int(row.debt_amount) if row.debt_amount else None, debt_on=row.debt_on, note=row.note,
            source=row.source, created_by_name=_person(users.get(row.created_by)), created_at=_iso(row.created_at),
            voided_at=_iso(row.voided_at), void_reason=row.void_reason,
        )

    def live(self, membership_id: UUID) -> Optional[MembershipOpeningBalance]:
        return (
            self.db.query(MembershipOpeningBalance)
            .filter(MembershipOpeningBalance.membership_id == membership_id, MembershipOpeningBalance.voided_at.is_(None))
            .first()
        )

    def get(self, org_id: str, membership_id: UUID) -> Optional[OpeningOut]:
        self.pay._membership(org_id, membership_id)
        row = self.live(membership_id)
        return self._out(row) if row else None

    # ---- validation

    @staticmethod
    def floor_of(first_day: date, accounting_start: date) -> date:
        """``L``: the earliest «оплачено до» and the latest «долг на дату».  The day before the accounting start is the seam between the
        old (their) books and ours: a student who came before it is "paid until / owing on" that day (S0, 22.10)."""
        return max(first_day, accounting_start - timedelta(days=1))

    def validate(self, body, *, first_day: date, accounting_start: date, today: date) -> None:
        """422 ``OPENING_RANGE`` / ``AMOUNT_RANGE`` (``body``: anything with paid_until / debt_amount / debt_on)."""
        floor = self.floor_of(first_day, accounting_start)
        if body.debt_amount is not None or body.debt_on is not None:
            if body.debt_amount is None or body.debt_on is None:
                raise OpeningRange("Долг на начало: укажите и сумму, и дату долга")
            if not isinstance(body.debt_amount, int) or body.debt_amount < 1 or body.debt_amount > MAX_MONEY:
                raise AmountRange(f"Сумма долга: от 1 до {payments_texts.num(MAX_MONEY)}")
            if body.debt_on > floor:
                raise OpeningRange(f"Долг на дату — не позже {floor.strftime('%d.%m.%Y')}: долг после начала учёта считается сам")
            if body.debt_on < OPENING_MIN_DATE:
                raise OpeningRange("Дата долга: не раньше 01.01.2020")
        if body.paid_until is not None:
            if body.paid_until < floor:
                hint = ": за более ранние дни внесите как долг" if floor == accounting_start - timedelta(days=1) else ""
                raise OpeningRange(f"Оплачено до — не раньше {floor.strftime('%d.%m.%Y')}{hint}")
            if body.paid_until > today + timedelta(days=OPENING_AHEAD_DAYS):
                raise OpeningRange("Оплачено до — не позже чем через год")

    def _transfer_target(self, membership_id: UUID) -> bool:
        return self.db.query(MembershipTransfer.id).filter(
            MembershipTransfer.to_membership_id == membership_id, MembershipTransfer.voided_at.is_(None)
        ).first() is not None

    # ---- write

    def apply(
        self, org_id: str, membership: StudentGroup, actor_id: str, actor_role: str, body, *, source: str, today: date,
        reason: Optional[str] = None, note: Optional[str] = None, require_reason_when_money: bool = True,
        facts: Optional["OpeningFacts"] = None, flush: bool = True, voided: Optional[List[UUID]] = None, launch_is_closed: Optional[bool] = None,
    ) -> Tuple[Optional[MembershipOpeningBalance], bool]:
        """The write inside the caller's transaction (no commit).  Returns ``(live row or None, changed)``; the id of the row it cancelled is appended to ``voided`` (the journal of the caller names it:
        the reconciliation looks for a line of the journal with the id of every cancelled row).

        Nothing but paid_until / debt given = the live one is voided; the same values again = noop.  S0: «оплачено до» = the day before the
        accounting start ("nothing is owed at the start", a reviewed row) is a REAL opening: the ledger gives the same debt and charges with or without it.
        ``facts`` (the bulk write): what the single call would read itself - read ONCE for all the rows, so the query count of a batch does
        not depend on its size; ``flush=False``: the caller flushes once (the rows get their ids here)."""
        self.check_opening_allowed(org_id, actor_role, launch_is_closed)      # R3c Г1: ONE rule for every road (the card, the table, the window of adding); the batch passes the state it has already read
        accounting_start = facts.accounting_start if facts else self.pay._loader(org_id, today).settings.accounting_start
        first_day = facts.first_day if facts else self._first_stay_day(membership)
        paid_until, debt_amount, debt_on = body.paid_until, body.debt_amount, body.debt_on
        empty = paid_until is None and debt_amount is None and debt_on is None
        if not empty:
            self.validate(body, first_day=first_day, accounting_start=accounting_start, today=today)
            if facts.transfer_target if facts else self._transfer_target(membership.id):
                raise OpeningNotAllowed("Остаток нельзя внести: ученик пришёл переводом")
        existing = facts.existing if facts else self.live(membership.id)
        if existing is not None and not empty and (existing.paid_until, int(existing.debt_amount or 0) or None, existing.debt_on) == (
            paid_until, debt_amount, debt_on
        ):
            return existing, False
        if existing is None and empty:
            return None, False
        # m13: replacing a live opening of a membership that has money always needs a reason (a return through «Добавить в группу» included)
        if (require_reason_when_money or existing is not None) and (facts.has_money if facts else self._has_money_records(membership.id)):
            reason = (reason or "").strip()
            if not 3 <= len(reason) <= 500:
                raise ReasonRequired("У ученика уже есть оплаты: укажите причину изменения остатка (от 3 до 500 символов)")
        actor = UUID(str(actor_id))
        if existing is not None:
            existing.voided_at, existing.voided_by = _utcnow(), actor
            if voided is not None:
                voided.append(existing.id)
            existing.void_reason = (reason or "").strip() or "заменён новым остатком"
            if flush:
                self.db.flush()
        new = None
        if not empty:
            new = MembershipOpeningBalance(
                id=uuid4(), org_id=UUID(str(org_id)), membership_id=membership.id, student_id=membership.student_id, group_id=membership.group_id,
                paid_until=paid_until, debt_amount=debt_amount, debt_on=debt_on, note=(note or "").strip() or None,
                source=source, created_by=actor, created_at=_utcnow(),
            )
            self.db.add(new)
            if flush:
                self.db.flush()
        return new, True

    def over_payments(self, org_id: str, membership_id: UUID, today: date) -> Optional[Dict[str, str]]:
        """Money-1 R3 Д4: the warning «оплачено до поверх оплат» of ONE membership as it stands NOW (after the write was flushed): the live payments and the coverage the ledger gives.  The batch plan
        (``openings_service``) and the window of adding use the same ``over_payments_warning``."""
        paid = self.db.query(func.coalesce(func.sum(StudentPayment.amount), 0)).filter(
            StudentPayment.org_id == org_id, StudentPayment.membership_id == membership_id, StudentPayment.kind == "payment", StudentPayment.voided_at.is_(None)).scalar()
        return over_payments_warning(int(paid or 0), self.pay._entry_of(org_id, membership_id, today).ledger.status.paid_until)

    @deadlock_guard
    def put(
        self, org_id: str, membership_id: UUID, actor_id: str, body: OpeningPut, *, dry_run: bool = False, actor_role: str = "admin",
    ) -> OpeningSaved:
        today = ps.business_today()
        try:
            membership = self.pay._membership(org_id, membership_id, lock=True, writable=True)
            self.pay._lock_family(org_id, membership.student_id)
            self.check_opening_allowed(org_id, actor_role)      # R3c Г1: the state of the launch, not the age of the days
            before = self.pay._entry_of(org_id, membership_id, today).ledger
            cancelled: List[UUID] = []
            row, changed = self.apply(org_id, membership, actor_id, actor_role, body, source="manual", today=today,
                                      reason=body.reason, note=body.note, voided=cancelled)
            after = self.pay._entry_of(org_id, membership_id, today).ledger
            if changed:
                self.pay._family_check(org_id, [membership_id])
                if not dry_run:
                    self._audit(org_id, membership, actor_id, actor_role, AUDIT_OPENING_SET if row else AUDIT_OPENING_VOID, {
                        "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                        "opening_id": str(row.id) if row else None, "voided_opening_id": str(cancelled[0]) if cancelled else None,
                        "paid_until": row.paid_until.isoformat() if row and row.paid_until else None,
                        "debt_amount": int(row.debt_amount) if row and row.debt_amount else None,
                        "debt_on": row.debt_on.isoformat() if row and row.debt_on else None,
                        "reason": (body.reason or "").strip() or None, "debt_before": before.debt, "debt_after": after.debt,
                        "advance_before": before.advance, "advance_after": after.advance})
            warning = self.over_payments(org_id, membership_id, today) if (changed and row is not None and row.paid_until is not None) else None
            out = OpeningSaved(opening=self._out(row) if row else None, impact=impact_of(before, after), dry_run=dry_run,
                               row=self._row(org_id, membership_id, today), warnings=[warning] if warning else [])
            if dry_run:
                self.db.rollback()
                return out
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        logger.info("opening balance set membership=%s actor=%s changed=%s", membership_id, actor_id, changed)
        return OpeningSaved(opening=self._out(row) if row else None, impact=out.impact, dry_run=False,
                            row=self._row(org_id, membership_id, today), warnings=out.warnings)

    @deadlock_guard
    def void(self, org_id: str, opening_id: UUID, actor_id: str, reason: str, *, actor_role: str = "admin") -> OpeningVoided:
        today = ps.business_today()
        try:
            reason = self.pay._check_reason(reason)
            probe = self.db.query(MembershipOpeningBalance).filter(
                MembershipOpeningBalance.id == opening_id, MembershipOpeningBalance.org_id == org_id).first()
            if probe is None:
                raise PaymentNotFound("Остаток на начало не найден")
            self.pay._lock_family(org_id, probe.student_id)
            row = (self.db.query(MembershipOpeningBalance).filter(MembershipOpeningBalance.id == opening_id)
                   .populate_existing().with_for_update().first())
            if row.voided_at is not None:
                raise AlreadyVoided("Остаток на начало уже отменён")
            self.check_opening_allowed(org_id, actor_role)      # R3c Г1
            membership = self.pay._membership(org_id, row.membership_id)
            before = self.pay._entry_of(org_id, row.membership_id, today).ledger
            row.voided_at, row.voided_by, row.void_reason = _utcnow(), UUID(str(actor_id)), reason
            self.db.flush()
            after = self.pay._entry_of(org_id, row.membership_id, today).ledger
            self.pay._family_check(org_id, [row.membership_id])
            self._audit(org_id, membership, actor_id, actor_role, AUDIT_OPENING_VOID, {
                "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                "opening_id": str(row.id), "reason": reason, "debt_before": before.debt, "debt_after": after.debt,
                "advance_before": before.advance, "advance_after": after.advance})
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        return OpeningVoided(opening=self._out(row), impact=impact_of(before, after), row=self._row(org_id, row.membership_id, today))


# ============================================================================================================
# Pause
# ============================================================================================================


def _days(first: date, last: date) -> Set[date]:
    return {first + timedelta(days=i) for i in range((last - first).days + 1)}


class PauseService(_Base):
    def _out(self, row: MembershipPause) -> PauseOut:
        users = self.pay._users([row.created_by])
        return PauseOut(
            id=str(row.id), membership_id=str(row.membership_id), first_day=row.first_day, last_day=row.last_day,
            days=(row.last_day - row.first_day).days + 1, reason=row.reason, created_by_name=_person(users.get(row.created_by)),
            created_at=_iso(row.created_at), voided_at=_iso(row.voided_at), void_reason=row.void_reason,
        )

    def list(self, org_id: str, membership_id: UUID) -> List[PauseOut]:
        self.pay._membership(org_id, membership_id)
        rows = (self.db.query(MembershipPause).filter(MembershipPause.membership_id == membership_id, MembershipPause.org_id == org_id)
                .order_by(MembershipPause.first_day.desc(), MembershipPause.created_at.desc()).all())
        return [self._out(r) for r in rows]

    def _live(self, membership_id: UUID) -> List[MembershipPause]:
        return (self.db.query(MembershipPause)
                .filter(MembershipPause.membership_id == membership_id, MembershipPause.voided_at.is_(None))
                .order_by(MembershipPause.first_day).all())

    # ---- checks

    @staticmethod
    def _check_range(first: date, last: date, today: date) -> None:
        if last < first:
            raise PauseRange("Пауза: «по» не раньше «с»")
        if (last - first).days + 1 > PAUSE_MAX_DAYS:
            raise PauseRange(f"Пауза не длиннее {PAUSE_MAX_DAYS} дней")

    def _assert_no_lesson_in_progress(self, membership: StudentGroup, first: date, last: date, only_days: Optional[Set[date]] = None) -> None:
        """A lesson of the group that is IN PROGRESS inside the days of the pause blocks it (the same as a transfer, an archive, a leave): the roster of a lesson is frozen while it goes on, and a pause
        put now would leave the student in the roster, to be marked and paid.  ``only_days`` (a change of a pause): just the days that ENTER the pause - a lesson on days that were in it already is not new.
        R3 m3: the sentence names the day when the lesson is not today's (a lesson left going last week is a lesson nobody finished, not «сейчас идёт»)."""
        rows = (self.db.query(GroupAttendance.lesson_date).filter(GroupAttendance.group_id == membership.group_id, GroupAttendance.status == "in_progress",
                                                                   GroupAttendance.lesson_date >= first.isoformat(), GroupAttendance.lesson_date <= last.isoformat())
                .order_by(GroupAttendance.lesson_date).all())
        going = [d for (d,) in rows if only_days is None or date.fromisoformat(d) in only_days]
        if going:
            if going[0] == ps.business_today().isoformat():
                raise PauseLessonInProgress("Сейчас идёт урок группы: поставить паузу можно после его завершения или отмены")
            raise PauseLessonInProgress(f"Урок группы {date.fromisoformat(going[0]).strftime('%d.%m')} начат и не завершён: сначала завершите или отмените его, потом ставьте паузу")

    def _assert_days_open(self, org_id: str, membership: StudentGroup, days: Set[date], today: date) -> None:
        """Money-1 A1 (was: «not older than 30 days»): the days of a pause that were / will be changed must not lie in a month of a teacher of this group that a saved sheet has closed
        (``closed_period.py``) - a pause takes the lessons of those days off the pay of the teacher.  Days that have not come yet touch no sheet."""
        past = sorted(d for d in days if d <= today)
        if not past:
            return
        group = self.db.get(Group, membership.group_id)
        if group is None:
            return
        periods = ClosedPeriods.of(self.db, org_id)
        if not periods.months_with_closed():
            return
        try:
            periods.assert_range_open(group_teacher_ids(self.db, group, past[0], past[-1]), past[0], past[-1], "паузу")
        except OpsError as exc:
            raise PauseLocked(exc.message, **exc.extra) from exc

    def _lessons(self, membership: StudentGroup, first: date, last: date) -> Tuple[List[Tuple[date, Optional[str]]], List[Tuple[date, str]]]:
        """``(excluded, attended)``: the lessons of the group inside the pause where the student has a mark."""
        sid = str(membership.student_id)
        rows = (self.db.query(GroupAttendance.lesson_date, GroupAttendance.records, GroupAttendance.status, GroupAttendance.roster_snapshot)
                .filter(GroupAttendance.group_id == membership.group_id, GroupAttendance.status.in_(("completed", "in_progress")),
                        GroupAttendance.lesson_date >= first.isoformat(), GroupAttendance.lesson_date <= last.isoformat())
                .order_by(GroupAttendance.lesson_date).all())
        excluded, attended = [], []
        for lesson_date, records, status, roster in rows:
            if isinstance(roster, list) and roster and sid not in {str(x.get("student_id")) for x in roster if isinstance(x, dict)}:
                continue
            mark = (records or {}).get(sid) if isinstance(records, dict) else None
            day = date.fromisoformat(lesson_date)
            if mark in ("present", "late"):
                attended.append((day, mark))
            elif status == "completed" and mark in ("absent", "excused"):
                excluded.append((day, mark))
        return excluded, attended

    def _payroll(self, org_id: str, membership: StudentGroup, first: date, last: date) -> PausePayroll:
        months, cursor = [], date(first.year, first.month, 1)
        while cursor <= last:
            months.append(cursor)
            cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        sheets = (self.db.query(PayrollSheet.lines).filter(PayrollSheet.org_id == org_id, PayrollSheet.period_month.in_(months)).all()
                  if months else [])
        mid = str(membership.id)
        changed = sum(1 for (lines,) in sheets if any(isinstance(x, dict) and x.get("membership_id") == mid for x in (lines or [])))
        return PausePayroll(months=[m.strftime("%Y-%m") for m in months], saved_sheets_changed=changed)

    # ---- write

    def _others(self, org_id: str, family: List[UUID], was: Dict[UUID, Any], own: UUID, *, done: bool) -> Tuple["_Moved", str]:
        """Money-2 T9: THE one check of a pause for the OTHER groups of the student (the ones a transfer tied to this stay: the sum that went there depends on the days of this pause).  The accounts
        of the whole family now against ``was``: a group whose returned money no longer fits (a refund made there) refuses the change (409 ``PAUSE_BREAKS_REFUND``, the group is named); else
        the groups whose advance falls / a debt appears / that gain are named - the same rule and the same sentences as a cancelled payment."""
        now = self.pay._accounts(org_id, family)
        for m, entry in now.items():
            if m != own and bl.F_OUTFLOW_EXCEEDS in entry.ledger.flags and bl.F_OUTFLOW_EXCEEDS not in was[m].ledger.flags:
                name = self.db.get(Group, entry.membership.group_id).name
                raise PauseBreaksRefund(f"Из группы «{name}» уже возвращены деньги, которые перешли туда из этой группы: без этого изменения паузы их стало бы меньше. Сначала отмените возврат в группе «{name}»")
        moved = self.pay._moved_by_correction(was, now, own)
        text = "; ".join(t for t in (payments_texts.new_debts_text(moved.new_debts, done=done, advance_drops=moved.advance_drops), payments_texts.other_gains_text(moved.gains, done=done)) if t)
        return moved, text

    def _saved(self, org_id, membership, row, before, after, *, money: bool, owner: bool, dry_run: bool, today: date,
               first: date, last: date, others: Optional[Tuple["_Moved", str]] = None) -> PauseSaved:
        excluded, attended = self._lessons(membership, first, last)
        moved, text = others if others and money else (None, None)
        return PauseSaved(
            new_debts=moved.new_debts if moved else [], advance_drops=moved.advance_drops if moved else [], other_gains=moved.gains if moved else [], other_groups_text=text or None,
            pause=self._out(row), impact=impact_of(before, after) if money else None,
            lessons=PauseLessons(excluded=[PauseLessonMark(date=d, marks=m) for d, m in excluded],
                                 attended=[PauseLessonMark(date=d, marks=m) for d, m in attended]),
            payroll=self._payroll(org_id, membership, first, last) if owner else None, dry_run=dry_run,
            row=None if dry_run or not money else self._row(org_id, membership.id, today))

    def _guard_open(self, org_id: str, membership_id: UUID, *, lock: bool = True) -> StudentGroup:
        membership = self.pay._membership(org_id, membership_id, lock=lock, writable=True)
        if not membership.is_active:
            raise MembershipClosed("Ученик уже не в этой группе: паузу поставить нельзя")
        return membership

    @deadlock_guard
    def create(
        self, org_id: str, membership_id: UUID, actor_id: str, body: PauseCreate, *, dry_run: bool = False, money: bool = True,
        owner: bool = False, actor_role: str = "admin",
    ) -> PauseSaved:
        today = ps.business_today()
        try:
            self._check_range(body.first_day, body.last_day, today)
            self.pay._check_backdate_days([body.first_day], today, actor_role, changing=False)      # R3b В4: an administrator sets no pause with a day older than 7 days
            membership = self._guard_open(org_id, membership_id)
            family = self.pay._lock_family(org_id, membership.student_id)
            live = self._live(membership_id)
            same = next((p for p in live if (p.first_day, p.last_day) == (body.first_day, body.last_day)), None)
            if same is not None:  # the same range again (a double click, a lost answer): nothing new - BEFORE the gates, which a lesson begun meanwhile would trip (R3 m3)
                before = after = self.pay._entry_of(org_id, membership_id, today).ledger
                out = self._saved(org_id, membership, same, before, after, money=money, owner=owner, dry_run=dry_run, today=today,
                                  first=same.first_day, last=same.last_day)
                self.db.rollback()
                return out
            self._assert_days_open(org_id, membership, _days(body.first_day, body.last_day), today)
            self._assert_no_lesson_in_progress(membership, body.first_day, body.last_day)
            if any(p.first_day <= body.last_day and body.first_day <= p.last_day for p in live):
                raise PauseOverlap("Пауза пересекается с другой паузой этого ученика")
            _, attended = self._lessons(membership, body.first_day, body.last_day)
            if attended:
                dates = ", ".join(d.strftime("%d.%m") for d, _ in attended)
                raise PauseOverlapsAttended(f"Ученик был отмечен на уроках {dates} — на эти даты паузу поставить нельзя",
                                            dates=[d.isoformat() for d, _ in attended])
            was = self.pay._accounts(org_id, family)
            before = was[membership.id].ledger
            row = MembershipPause(
                org_id=UUID(str(org_id)), membership_id=membership.id, student_id=membership.student_id, group_id=membership.group_id,
                first_day=body.first_day, last_day=body.last_day, reason=(body.reason or "").strip() or None,
                created_by=UUID(str(actor_id)), created_at=_utcnow())
            self.db.add(row)
            self.db.flush()
            self._close_excuses(membership, row.first_day, row.last_day, actor_id)
            others = self._others(org_id, family, was, membership.id, done=not dry_run)
            after = self.pay._entry_of(org_id, membership_id, today).ledger
            if not dry_run:
                self._audit(org_id, membership, actor_id, actor_role, AUDIT_PAUSE_SET, {
                    "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                    "pause_id": str(row.id), "first_day": row.first_day.isoformat(), "last_day": row.last_day.isoformat(),
                    "reason": row.reason, "charged_before": before.charged, "charged_after": after.charged,
                    "advance_before": before.advance, "advance_after": after.advance, **self._other_facts(others[0])})
            out = self._saved(org_id, membership, row, before, after, money=money, owner=owner, dry_run=dry_run, today=today,
                              first=row.first_day, last=row.last_day, others=others)
            if dry_run:
                self.db.rollback()
                return out
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        except Exception as exc:
            from sqlalchemy.exc import IntegrityError

            if isinstance(exc, IntegrityError) and "ex_membership_pauses_no_overlap" in str(exc):
                self.db.rollback()
                raise PauseOverlap("Пауза пересекается с другой паузой этого ученика") from exc
            raise
        invalidate_summary(org_id)
        logger.info("pause created id=%s membership=%s actor=%s", row.id, membership_id, actor_id)
        return self._saved(org_id, membership, row, before, after, money=money, owner=owner, dry_run=False, today=today,
                           first=row.first_day, last=row.last_day, others=others)

    @staticmethod
    def _other_facts(moved: "_Moved") -> Dict[str, Any]:
        """The lines of the journal of a pause keep what the OTHER groups lost or gained (only when something moved there)."""
        return {k: v for k, v in (("new_debts", moved.new_debts), ("advance_drops", moved.advance_drops), ("other_gains", moved.gains)) if v}

    def _close_excuses(self, membership: StudentGroup, first: date, last: date, actor_id: str) -> int:
        from .attendance_excuses import AttendanceExcuseService  # lazy: the excuses module pulls in the service

        return AttendanceExcuseService(self.db).close_for_pause(membership.student_id, membership.group_id, first, last, actor_id, _utcnow())

    def _locked_pause(self, org_id: str, pause_id: UUID) -> MembershipPause:
        probe = self.db.query(MembershipPause).filter(MembershipPause.id == pause_id, MembershipPause.org_id == org_id).first()
        if probe is None:
            raise PaymentNotFound("Пауза не найдена")
        self.pay._lock_family(org_id, probe.student_id)
        row = self.db.query(MembershipPause).filter(MembershipPause.id == pause_id).populate_existing().with_for_update().first()
        if row.voided_at is not None:
            raise AlreadyVoided("Пауза уже отменена")
        return row

    def _check_unlocking(self, org_id: str, membership: StudentGroup, old: Set[date], new: Set[date], today: date) -> None:
        """Days that become payable again must not lie in a closed month (422 ``PAUSE_LOCKED``)."""
        self._assert_days_open(org_id, membership, old - new, today)

    @deadlock_guard
    def patch(
        self, org_id: str, pause_id: UUID, actor_id: str, body: PausePatch, *, dry_run: bool = False, money: bool,
        owner: bool = False, actor_role: str,
    ) -> PauseSaved:
        today = ps.business_today()
        try:
            row = self._locked_pause(org_id, pause_id)
            membership = self.pay._membership(org_id, row.membership_id, lock=True)
            first = body.first_day or row.first_day
            last = body.last_day or row.last_day
            if last < first:
                raise PauseRange("Пауза: «по» не раньше «с»")
            if (last - first).days + 1 > PAUSE_MAX_DAYS:
                raise PauseRange(f"Пауза не длиннее {PAUSE_MAX_DAYS} дней")
            self.pay._check_backdate_days(_days(first, last) ^ _days(row.first_day, row.last_day), today, actor_role, changing=True)      # R3b В4: the days that CHANGE decide (the reason alone touches no day)
            # M3: EVERY day that enters the pause (new - old) must lie in an open month, as a new pause's days must - not only a moved start
            self._assert_days_open(org_id, membership, _days(first, last) - _days(row.first_day, row.last_day), today)
            self._assert_no_lesson_in_progress(membership, first, last, only_days=_days(first, last) - _days(row.first_day, row.last_day))
            self._check_unlocking(org_id, membership, _days(row.first_day, row.last_day), _days(first, last), today)
            if any(p.id != row.id and p.first_day <= last and first <= p.last_day for p in self._live(row.membership_id)):
                raise PauseOverlap("Пауза пересекается с другой паузой этого ученика")
            _, attended = self._lessons(membership, first, last)
            newly = [(d, m) for d, m in attended if not row.first_day <= d <= row.last_day]
            if newly:
                dates = ", ".join(d.strftime("%d.%m") for d, _ in newly)
                raise PauseOverlapsAttended(f"Ученик был отмечен на уроках {dates} — на эти даты паузу поставить нельзя",
                                            dates=[d.isoformat() for d, _ in newly])
            family = self.pay._lock_family(org_id, membership.student_id)
            was = self.pay._accounts(org_id, family)
            before = was[membership.id].ledger
            old = (row.first_day, row.last_day, row.reason)
            row.first_day, row.last_day = first, last
            if body.reason is not None:
                row.reason = body.reason.strip() or None
            self.db.flush()
            self._close_excuses(membership, first, last, actor_id)       # newly covered days only change anything; closed ones stay closed
            others = self._others(org_id, family, was, membership.id, done=not dry_run)
            after = self.pay._entry_of(org_id, row.membership_id, today).ledger
            self.pay._family_check(org_id, [row.membership_id])
            if after.charged > before.charged and bl.F_OUTFLOW_EXCEEDS in after.flags:
                raise PauseBreaksRefund("Сначала отмените возврат: без паузы вернуть нечего")
            change_reason = None
            if after.charged != before.charged and not dry_run:      # M3: a change that moves money says why (the dry run only shows the impact)
                change_reason = self.pay._check_reason(body.reason)
            if not dry_run:
                self._audit(org_id, membership, actor_id, actor_role, AUDIT_PAUSE_SET, {
                    "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                    "pause_id": str(row.id), "first_day": first.isoformat(), "last_day": last.isoformat(), "reason": change_reason or row.reason,
                    "was": [old[0].isoformat(), old[1].isoformat()], "charged_before": before.charged, "charged_after": after.charged, **self._other_facts(others[0])})
            out = self._saved(org_id, membership, row, before, after, money=money, owner=owner, dry_run=dry_run, today=today,
                              first=min(first, old[0]), last=max(last, old[1]), others=others)
            if dry_run:
                self.db.rollback()
                return out
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        return self._saved(org_id, membership, row, before, after, money=money, owner=owner, dry_run=False, today=today,
                           first=min(first, old[0]), last=max(last, old[1]), others=others)

    @deadlock_guard
    def void(self, org_id: str, pause_id: UUID, actor_id: str, reason: str, *, money: bool, actor_role: str, dry_run: bool = False) -> PauseVoided:
        today = ps.business_today()
        try:
            reason = self.pay._check_reason(reason)
            row = self._locked_pause(org_id, pause_id)
            self.pay._check_backdate_days([row.first_day], today, actor_role, changing=True)      # R3b В4: cancelling a pause gives back ALL its days: the oldest decides
            membership = self.pay._membership(org_id, row.membership_id, lock=True)
            self._check_unlocking(org_id, membership, _days(row.first_day, row.last_day), set(), today)
            family = self.pay._lock_family(org_id, membership.student_id)
            was = self.pay._accounts(org_id, family)
            before = was[membership.id].ledger
            row.voided_at, row.voided_by, row.void_reason = _utcnow(), UUID(str(actor_id)), reason
            self.db.flush()
            moved, text = self._others(org_id, family, was, membership.id, done=not dry_run)
            after = self.pay._entry_of(org_id, row.membership_id, today).ledger
            self.pay._family_check(org_id, [row.membership_id])
            if dry_run:
                out = PauseVoided(pause=self._out(row), impact=impact_of(before, after) if money else None, dry_run=True,
                                  new_debts=moved.new_debts if money else [], advance_drops=moved.advance_drops if money else [], other_gains=moved.gains if money else [], other_groups_text=(text or None) if money else None)
                self.db.rollback()
                return out
            self._audit(org_id, membership, actor_id, actor_role, AUDIT_PAUSE_VOID, {
                "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                "pause_id": str(row.id), "first_day": row.first_day.isoformat(), "last_day": row.last_day.isoformat(),
                "reason": reason, "charged_before": before.charged, "charged_after": after.charged, **self._other_facts(moved)})
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        return PauseVoided(pause=self._out(row), impact=impact_of(before, after) if money else None, row=self._row(org_id, row.membership_id, today) if money else None,
                           new_debts=moved.new_debts if money else [], advance_drops=moved.advance_drops if money else [], other_gains=moved.gains if money else [], other_groups_text=(text or None) if money else None)

    # ---- used by leave / transfer / archive (inside the caller's transaction)

    def close_at_leave(self, membership_id: UUID, left_day: date, actor_id: Optional[str], via: str) -> int:
        """Pauses after the day of leaving are voided, the one that holds the day is cut to it (spec §2.8).  Returns the number touched.  ``via``: what ended the stay
        (``leave`` / ``transfer``), written to the line of the journal."""
        touched = 0
        membership = None
        for p in self._live(membership_id):
            was = [p.first_day.isoformat(), p.last_day.isoformat()]
            if p.first_day > left_day:
                p.voided_at, p.voided_by, p.void_reason = _utcnow(), UUID(str(actor_id)) if actor_id else None, "ученик ушёл из группы"
                if p.voided_by is None:  # the all-or-none check wants a person: the creator stands for the system
                    p.voided_by = p.created_by
                action = AUDIT_PAUSE_VOID
            elif p.last_day > left_day:
                p.last_day = left_day          # cut IN PLACE: the one change of a pause nobody asked for by hand - the line of the journal keeps what it was
                action = AUDIT_PAUSE_SET
            else:
                continue
            touched += 1
            author = actor_id or str(p.created_by)
            membership = membership or self.db.get(StudentGroup, membership_id)
            self._audit(str(p.org_id), membership, author, self.pay.role_of(str(p.org_id), author), action, {
                "actor_id": str(author), "membership_id": str(membership_id), "student_id": str(p.student_id), "pause_id": str(p.id), "first_day": p.first_day.isoformat(),
                "last_day": p.last_day.isoformat(), "was": was, "reason": p.void_reason if action == AUDIT_PAUSE_VOID else "ученик ушёл из группы", "via": via})
        if touched:
            self.db.flush()
        return touched

    # ---- Money-2 T2: a transfer takes the pauses of the student with him

    def plan_carry(self, membership_id: UUID, left_day: date) -> List[Tuple[date, date, Optional[str], UUID]]:
        """The pauses that go with the student when his stay ends by a TRANSFER on ``left_day``: ``[(first_day, last_day, reason, old pause id)]`` - the rest of the pause that holds the
        day (from the day after it) and every later one whole.  Read BEFORE ``close_at_leave`` cuts / voids them in the old group."""
        after = left_day + timedelta(days=1)
        return [(max(p.first_day, after), p.last_day, p.reason, p.id) for p in self._live(membership_id) if p.last_day > left_day]

    def carry_to(self, org_id: str, target: StudentGroup, plan: List[Tuple[date, date, Optional[str], UUID]], actor_id: str) -> List[MembershipPause]:
        """Create the planned pauses in the new group of the student (same days, same reason; the days the new group has under a pause of its own are not doubled) - one line of the
        journal each, naming the old pause and the new one.  Inside the caller's transaction; the target is flushed (it has its id)."""
        taken = [(p.first_day, p.last_day) for p in self._live(target.id)]
        made: List[MembershipPause] = []
        for first, last, reason, old_id in plan:
            for a, b in _uncovered(first, last, taken):
                row = MembershipPause(org_id=UUID(str(org_id)), membership_id=target.id,
                                      student_id=target.student_id, group_id=target.group_id, first_day=a, last_day=b, reason=reason,
                                      created_by=UUID(str(actor_id)), created_at=_utcnow())
                self.db.add(row)
                self.db.flush()
                self._close_excuses(target, a, b, actor_id)
                taken.append((a, b))
                made.append(row)
                self._audit(str(org_id), target, actor_id, self.pay.role_of(str(org_id), actor_id), AUDIT_PAUSE_SET, {
                    "actor_id": str(actor_id), "membership_id": str(target.id), "student_id": str(target.student_id), "pause_id": str(row.id), "first_day": a.isoformat(),
                    "last_day": b.isoformat(), "reason": reason, "moved_from_pause_id": str(old_id), "via": "transfer"})
        return made

    # ---- Money-2 T6: a return on the day of leaving takes the leaving back

    def restore_after_leave(self, org_id: str, membership: StudentGroup, since: datetime, actor_id: str) -> List[Tuple[MembershipPause, date, date]]:
        """Give the membership its pauses back as the journal of THE leaving says they were (the lines ``via`` leave / transfer / archive written since ``since``): a pause that was cut gets its last day
        back, a cancelled one lives again - each with a line of the journal (``via = leave_undone``).  A pause that somebody changed after the leaving (another last day, cancelled for another reason) is left.
        Returns ``[(pause, first_day, last_day it has now)]``."""
        lines = (self.db.query(GroupAudit).filter(GroupAudit.org_id == org_id, GroupAudit.group_id == membership.group_id, GroupAudit.created_at >= since,
                                                  GroupAudit.action.in_((AUDIT_PAUSE_SET, AUDIT_PAUSE_VOID)))
                 .order_by(GroupAudit.created_at, GroupAudit.id).all())
        done: List[Tuple[MembershipPause, date, date]] = []
        seen: Set[str] = set()
        for line in lines:
            snap = line.snapshot or {}
            if snap.get("membership_id") != str(membership.id) or snap.get("via") not in ("leave", "transfer", "archive") or not snap.get("was"):
                continue
            pause = self.db.get(MembershipPause, UUID(snap["pause_id"]))
            if pause is None or str(pause.id) in seen:
                continue
            first, last = (date.fromisoformat(d) for d in snap["was"])
            if line.action == AUDIT_PAUSE_VOID:
                if pause.voided_at is None or pause.void_reason != "ученик ушёл из группы":
                    continue
                pause.voided_at = pause.voided_by = pause.void_reason = None
            else:
                if pause.voided_at is not None or pause.last_day.isoformat() != snap["last_day"]:
                    continue
                pause.last_day = last
            seen.add(str(pause.id))
            self.db.flush()
            self._close_excuses(membership, first, last, actor_id)
            self._audit(str(pause.org_id), membership, actor_id, self.pay.role_of(str(pause.org_id), actor_id), AUDIT_PAUSE_SET, {
                "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(pause.student_id), "pause_id": str(pause.id), "first_day": first.isoformat(),
                "last_day": last.isoformat(), "was": snap["was"] if line.action == AUDIT_PAUSE_SET else None, "reason": "уход отменён: ученик вернулся в тот же день", "via": "leave_undone"})
            done.append((pause, first, last))
        return done


def _uncovered(first: date, last: date, taken: List[Tuple[date, date]]) -> List[Tuple[date, date]]:
    """``[first, last]`` without the days of ``taken`` ranges: the pieces that are still free (a pause of the new group is never doubled)."""
    pieces, cursor = [], first
    for a, b in sorted(t for t in taken if t[0] <= last and first <= t[1]):
        if a > cursor:
            pieces.append((cursor, a - timedelta(days=1)))
        cursor = max(cursor, b + timedelta(days=1))
    if cursor <= last:
        pieces.append((cursor, last))
    return pieces
