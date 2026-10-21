"""
ALGO - LOGIC-A: the opening balance of a membership (decision 1) and the pause of a student (decision 2).

Both are FACTS read by ``ledger_service`` into ``billing_ledger`` (``Opening`` / ``Pause``) - nothing is "recalculated" when they are
created or cancelled; the next call of the ledger gives the new answer.  Every write: the family lock (student -> memberships by id, the order
of ``payments_service``), the ledger "before / after" for the impact, one ``group_audit`` row, one transaction.  ``dry_run`` does the whole
write inside the transaction, measures the impact and rolls back.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy import func

from src.models.auth import (
    GroupAttendance,
    GroupAudit,
    MembershipOpeningBalance,
    MembershipPause,
    MembershipTransfer,
    PayrollSheet,
    StudentGroup,
    StudentPayment,
    StudentRefund,
    StudentWriteoff,
    User,
)

from . import billing_ledger as bl
from . import ledger_service as ls
from . import membership_periods
from . import payment_status as ps
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
    PaymentConflict,
    PaymentError,
    PaymentInvalid,
    PaymentNotFound,
    PaymentService,
    ReasonRequired,
    VoidBreaksRefund,
    _iso,
    _person,
    _utcnow,
    deadlock_guard,
    invalidate_summary,
)

logger = logging.getLogger(__name__)

AUDIT_OPENING_SET = "opening_balance_set"
AUDIT_OPENING_VOID = "opening_balance_void"
AUDIT_PAUSE_SET = "membership_pause_set"
AUDIT_PAUSE_VOID = "membership_pause_void"
PAUSE_BACKDATE_DAYS = 30
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


class _Base:
    def __init__(self, db) -> None:
        self.db = db
        self.pay = PaymentService(db)

    def _first_stay_day(self, membership: StudentGroup) -> date:
        periods = membership_periods.periods_by_membership(self.db, [membership.id]).get(membership.id, [])
        if periods:
            return ps.business_date(periods[0].joined_at)
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

    def validate(self, body, *, first_day: date, accounting_start: date, today: date) -> None:
        """422 ``OPENING_RANGE`` / ``AMOUNT_RANGE`` (``body``: anything with paid_until / debt_amount / debt_on)."""
        if body.debt_amount is not None or body.debt_on is not None:
            if body.debt_amount is None or body.debt_on is None:
                raise OpeningRange("Долг на начало: укажите и сумму, и дату долга")
            if not isinstance(body.debt_amount, int) or body.debt_amount < 1 or body.debt_amount > MAX_MONEY:
                raise AmountRange(f"Сумма долга: от 1 до {MAX_MONEY:,}".replace(",", " "))
            if body.debt_on > first_day:
                raise OpeningRange("Долг на дату — не позже дня зачисления")
            if body.debt_on < OPENING_MIN_DATE:
                raise OpeningRange("Дата долга: не раньше 01.01.2020")
        if body.paid_until is not None:
            lowest = max(accounting_start, first_day)
            if body.paid_until < lowest:
                raise OpeningRange("Оплачено до — не раньше даты зачисления")
            if body.paid_until > today + timedelta(days=OPENING_AHEAD_DAYS):
                raise OpeningRange("Оплачено до — не позже чем через год")

    def _guard_transfer(self, membership_id: UUID) -> None:
        if self.db.query(MembershipTransfer.id).filter(
            MembershipTransfer.to_membership_id == membership_id, MembershipTransfer.voided_at.is_(None)
        ).first():
            raise OpeningNotAllowed("Остаток нельзя внести: ученик пришёл переводом")

    # ---- write

    def apply(
        self, org_id: str, membership: StudentGroup, actor_id: str, actor_role: str, body, *, source: str, today: date,
        reason: Optional[str] = None, note: Optional[str] = None, require_reason_when_money: bool = True,
    ) -> Tuple[Optional[MembershipOpeningBalance], bool]:
        """The write inside the caller's transaction (no commit).  Returns ``(live row or None, changed)``.

        Nothing but paid_until / debt given = the live one is voided; the same values again = noop."""
        accounting_start = self.pay._loader(org_id, today).settings.accounting_start
        first_day = self._first_stay_day(membership)
        empty = body.paid_until is None and body.debt_amount is None and body.debt_on is None
        if not empty:
            self.validate(body, first_day=first_day, accounting_start=accounting_start, today=today)
            self._guard_transfer(membership.id)
        existing = self.live(membership.id)
        if existing is not None and not empty and (existing.paid_until, int(existing.debt_amount or 0) or None, existing.debt_on) == (
            body.paid_until, body.debt_amount, body.debt_on
        ):
            return existing, False
        if existing is None and empty:
            return None, False
        # m13: replacing a live opening of a membership that has money always needs a reason (a return through «Добавить в группу» included)
        if (require_reason_when_money or existing is not None) and self._has_money_records(membership.id):
            reason = (reason or "").strip()
            if not 3 <= len(reason) <= 500:
                raise ReasonRequired("У ученика уже есть оплаты: укажите причину изменения остатка (от 3 до 500 символов)")
        actor = UUID(str(actor_id))
        if existing is not None:
            existing.voided_at, existing.voided_by = _utcnow(), actor
            existing.void_reason = (reason or "").strip() or "заменён новым остатком"
            self.db.flush()
        new = None
        if not empty:
            new = MembershipOpeningBalance(
                org_id=UUID(str(org_id)), membership_id=membership.id, student_id=membership.student_id, group_id=membership.group_id,
                paid_until=body.paid_until, debt_amount=body.debt_amount, debt_on=body.debt_on, note=(note or "").strip() or None,
                source=source, created_by=actor, created_at=_utcnow(),
            )
            self.db.add(new)
            self.db.flush()
        return new, True

    @deadlock_guard
    def put(
        self, org_id: str, membership_id: UUID, actor_id: str, body: OpeningPut, *, dry_run: bool = False, actor_role: str = "admin",
    ) -> OpeningSaved:
        today = ps.business_today()
        try:
            membership = self.pay._membership(org_id, membership_id, lock=True, writable=True)
            self.pay._lock_family(org_id, membership.student_id)
            before = self.pay._entry_of(org_id, membership_id, today).ledger
            row, changed = self.apply(org_id, membership, actor_id, actor_role, body, source="manual", today=today,
                                      reason=body.reason, note=body.note)
            after = self.pay._entry_of(org_id, membership_id, today).ledger
            if changed:
                self.pay._family_check(org_id, [membership_id])
                if not dry_run:
                    self._audit(org_id, membership, actor_id, actor_role, AUDIT_OPENING_SET if row else AUDIT_OPENING_VOID, {
                        "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                        "opening_id": str(row.id) if row else None,
                        "paid_until": row.paid_until.isoformat() if row and row.paid_until else None,
                        "debt_amount": int(row.debt_amount) if row and row.debt_amount else None,
                        "debt_on": row.debt_on.isoformat() if row and row.debt_on else None,
                        "reason": (body.reason or "").strip() or None, "debt_before": before.debt, "debt_after": after.debt,
                        "advance_before": before.advance, "advance_after": after.advance})
            out = OpeningSaved(opening=self._out(row) if row else None, impact=impact_of(before, after), dry_run=dry_run,
                               row=self._row(org_id, membership_id, today))
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
                            row=self._row(org_id, membership_id, today))

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
        if first < today - timedelta(days=PAUSE_BACKDATE_DAYS):
            raise PauseRange(f"Пауза начинается не раньше чем {PAUSE_BACKDATE_DAYS} дней назад")
        if (last - first).days + 1 > PAUSE_MAX_DAYS:
            raise PauseRange(f"Пауза не длиннее {PAUSE_MAX_DAYS} дней")

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

    def _saved(self, org_id, membership, row, before, after, *, money: bool, owner: bool, dry_run: bool, today: date,
               first: date, last: date) -> PauseSaved:
        excluded, attended = self._lessons(membership, first, last)
        return PauseSaved(
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
            membership = self._guard_open(org_id, membership_id)
            self.pay._lock_family(org_id, membership.student_id)
            live = self._live(membership_id)
            same = next((p for p in live if (p.first_day, p.last_day) == (body.first_day, body.last_day)), None)
            if same is not None:  # the same range again (a double click): nothing new
                before = after = self.pay._entry_of(org_id, membership_id, today).ledger
                out = self._saved(org_id, membership, same, before, after, money=money, owner=owner, dry_run=dry_run, today=today,
                                  first=same.first_day, last=same.last_day)
                self.db.rollback()
                return out
            if any(p.first_day <= body.last_day and body.first_day <= p.last_day for p in live):
                raise PauseOverlap("Пауза пересекается с другой паузой этого ученика")
            _, attended = self._lessons(membership, body.first_day, body.last_day)
            if attended:
                dates = ", ".join(d.strftime("%d.%m") for d, _ in attended)
                raise PauseOverlapsAttended(f"Ученик был отмечен на уроках {dates} — на эти даты паузу поставить нельзя",
                                            dates=[d.isoformat() for d, _ in attended])
            before = self.pay._entry_of(org_id, membership_id, today).ledger
            row = MembershipPause(
                org_id=UUID(str(org_id)), membership_id=membership.id, student_id=membership.student_id, group_id=membership.group_id,
                first_day=body.first_day, last_day=body.last_day, reason=(body.reason or "").strip() or None,
                created_by=UUID(str(actor_id)), created_at=_utcnow())
            self.db.add(row)
            self.db.flush()
            self._close_excuses(membership, row.first_day, row.last_day, actor_id)
            after = self.pay._entry_of(org_id, membership_id, today).ledger
            if not dry_run:
                self._audit(org_id, membership, actor_id, actor_role, AUDIT_PAUSE_SET, {
                    "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                    "pause_id": str(row.id), "first_day": row.first_day.isoformat(), "last_day": row.last_day.isoformat(),
                    "reason": row.reason, "charged_before": before.charged, "charged_after": after.charged,
                    "advance_before": before.advance, "advance_after": after.advance})
            out = self._saved(org_id, membership, row, before, after, money=money, owner=owner, dry_run=dry_run, today=today,
                              first=row.first_day, last=row.last_day)
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
                           first=row.first_day, last=row.last_day)

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

    @staticmethod
    def _check_unlocking(old: Set[date], new: Set[date], today: date) -> None:
        """Days that become payable again must not be older than 30 days (422 ``PAUSE_LOCKED``)."""
        edge = today - timedelta(days=PAUSE_BACKDATE_DAYS)
        if any(d < edge for d in old - new):
            raise PauseLocked("Паузу нельзя сократить или отменить: дни старше 30 дней уже не меняются")

    @deadlock_guard
    def patch(
        self, org_id: str, pause_id: UUID, actor_id: str, body: PausePatch, *, dry_run: bool = False, money: bool = True,
        owner: bool = False, actor_role: str = "admin",
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
            # M3: EVERY day that enters the pause (new - old) is as old as a new pause may be - not only a moved start (a pause that lies wholly
            # in the past cannot be lengthened over older days)
            if any(d < today - timedelta(days=PAUSE_BACKDATE_DAYS) for d in _days(first, last) - _days(row.first_day, row.last_day)):
                raise PauseRange(f"Пауза начинается не раньше чем {PAUSE_BACKDATE_DAYS} дней назад")
            self._check_unlocking(_days(row.first_day, row.last_day), _days(first, last), today)
            if any(p.id != row.id and p.first_day <= last and first <= p.last_day for p in self._live(row.membership_id)):
                raise PauseOverlap("Пауза пересекается с другой паузой этого ученика")
            _, attended = self._lessons(membership, first, last)
            newly = [(d, m) for d, m in attended if not row.first_day <= d <= row.last_day]
            if newly:
                dates = ", ".join(d.strftime("%d.%m") for d, _ in newly)
                raise PauseOverlapsAttended(f"Ученик был отмечен на уроках {dates} — на эти даты паузу поставить нельзя",
                                            dates=[d.isoformat() for d, _ in newly])
            before = self.pay._entry_of(org_id, row.membership_id, today).ledger
            old = (row.first_day, row.last_day, row.reason)
            row.first_day, row.last_day = first, last
            if body.reason is not None:
                row.reason = body.reason.strip() or None
            self.db.flush()
            self._close_excuses(membership, first, last, actor_id)       # newly covered days only change anything; closed ones stay closed
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
                    "was": [old[0].isoformat(), old[1].isoformat()], "charged_before": before.charged, "charged_after": after.charged})
            out = self._saved(org_id, membership, row, before, after, money=money, owner=owner, dry_run=dry_run, today=today,
                              first=min(first, old[0]), last=max(last, old[1]))
            if dry_run:
                self.db.rollback()
                return out
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        return self._saved(org_id, membership, row, before, after, money=money, owner=owner, dry_run=False, today=today,
                           first=min(first, old[0]), last=max(last, old[1]))

    @deadlock_guard
    def void(self, org_id: str, pause_id: UUID, actor_id: str, reason: str, *, money: bool = True, actor_role: str = "admin") -> PauseVoided:
        today = ps.business_today()
        try:
            reason = self.pay._check_reason(reason)
            row = self._locked_pause(org_id, pause_id)
            membership = self.pay._membership(org_id, row.membership_id, lock=True)
            self._check_unlocking(_days(row.first_day, row.last_day), set(), today)
            before = self.pay._entry_of(org_id, row.membership_id, today).ledger
            row.voided_at, row.voided_by, row.void_reason = _utcnow(), UUID(str(actor_id)), reason
            self.db.flush()
            after = self.pay._entry_of(org_id, row.membership_id, today).ledger
            self.pay._family_check(org_id, [row.membership_id])
            self._audit(org_id, membership, actor_id, actor_role, AUDIT_PAUSE_VOID, {
                "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                "pause_id": str(row.id), "first_day": row.first_day.isoformat(), "last_day": row.last_day.isoformat(),
                "reason": reason, "charged_before": before.charged, "charged_after": after.charged})
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        return PauseVoided(pause=self._out(row), impact=impact_of(before, after) if money else None,
                           row=self._row(org_id, row.membership_id, today) if money else None)

    # ---- used by leave / transfer / archive (inside the caller's transaction)

    def close_at_leave(self, membership_id: UUID, left_day: date, actor_id: Optional[str] = None) -> int:
        """Pauses after the day of leaving are voided, the one that holds the day is cut to it (spec §2.8).  Returns the number touched."""
        touched = 0
        for p in self._live(membership_id):
            if p.first_day > left_day:
                p.voided_at, p.voided_by, p.void_reason = _utcnow(), UUID(str(actor_id)) if actor_id else None, "ученик ушёл из группы"
                if p.voided_by is None:  # the all-or-none check wants a person: the creator stands for the system
                    p.voided_by = p.created_by
                touched += 1
            elif p.last_day > left_day:
                p.last_day = left_day
                touched += 1
        if touched:
            self.db.flush()
        return touched
