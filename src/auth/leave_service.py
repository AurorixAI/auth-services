"""
ALGO - LOGIC-A (F9, F13): a student leaves a group / is archived WITH his money.

Before the leaving the server shows the account «after the leaving» (the ledger with every stay closed today) and the owner chooses per
membership: ``keep`` (default), ``writeoff`` (only a debt; the reason is mandatory) or ``refund`` (only a refundable advance; the method is
mandatory).  The choice is carried out in the SAME transaction as the leaving, after the stay is closed; the numbers the client saw
(``expected_unpaid`` / ``expected_refundable``) are checked again under the locks - a difference is 409 ``BALANCE_CHANGED`` with the fresh
numbers and nothing is done.  A debt that is kept can still be paid after the leaving (S-5: not more than the debt).

Closing a stay also (F13) ends the individual price of the membership the day after, and (decision 2) cuts / cancels its pauses - the same
hooks run for «Убрать из группы», for a transfer (``add_student_to_group``) and for the archiving.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import Group, GroupAudit, Student, StudentGroup, StudentRefund, StudentWriteoff

from . import billing_ledger as bl
from . import business_clock as bc
from . import leave_effects
from . import ledger_service as ls
from . import payment_status as ps
from . import pricing
from .logica_service import AUDIT_OPENING_SET, AUDIT_OPENING_VOID, PauseService
from .membership_periods import close_period
from .payments_schemas import (
    FreshBalance,
    LeaveMembership,
    LeaveMoney,
    LeavePreview,
    LeaveRequest,
    LeaveResult,
    SettledOut,
    SettleItem,
)
from .payments_service import (
    AUDIT_REFUND,
    AUDIT_WRITEOFF,
    AUDIT_WRITEOFF_VOID,
    MethodRequired,
    PaymentConflict,
    PaymentError,
    PaymentForbidden,
    PaymentInvalid,
    PaymentNotFound,
    PaymentService,
    ReasonRequired,
    _utcnow,
    deadlock_guard,
    invalidate_summary,
)

logger = logging.getLogger(__name__)

AUDIT_LEFT = "student_left_group"
BALANCE_NEEDS_MONEY_RIGHT = "Убрать ученика с оплатой вперёд или долгом может владелец или администратор с доступом к деньгам"      # Money-2 T11: ONE text for the leaving and the archive; a fact, no sum
LEFT_GROUP_REASON = "left_group"
RETURN_REASON = "return_group"


class BalanceChanged(PaymentConflict):
    code = "BALANCE_CHANGED"


class OpeningNotAllowedAdd(PaymentInvalid):
    code = "OPENING_NOT_ALLOWED"


# ---- hooks run whenever a stay is closed / reopened ------------------------------------------------------------


def on_membership_closed(db: Session, membership: StudentGroup, left_at: datetime, actor_id: str, via: str) -> None:
    """F13 + pauses.  The caller closed the period (``close_period``) and holds the locks; nothing is committed here.

    The individual price (with a sum) in force on the last day ends the day after - a return is then priced by the group; the scheduled
    individual prices dated after the last day are cancelled (Р3-1б: they belong to the stay that ended, a return finds a clean book); the pauses
    after the last day are cancelled and the one that holds it is cut to it (``via="transfer"``: the transfer then creates their rest in the new group, Money-2 T2).  The author is mandatory:
    the price audit has no row without one."""
    left_day = ps.business_date(left_at)
    books = pricing.load_books(db, [membership.group_id], [membership.id])
    price = books.price(membership.id, membership.group_id, left_day)
    if price.source == pricing.SOURCE_OVERRIDE and price.amount is not None:
        pricing.write_override(db, membership.id, actor_id, amount=None, valid_from=left_day.fromordinal(left_day.toordinal() + 1),
                               reason=LEFT_GROUP_REASON)
    pricing.cancel_rows_after(db, membership.id, actor_id, left_day, markers=False, why=LEFT_GROUP_REASON)
    PauseService(db).close_at_leave(membership.id, left_day, actor_id, via=via)


EMPTY_STAY_REASON = "Пребывание закрыто в день записи — долга нет"


def cancel_forgiveness_of_empty_stay(db: Session, membership: StudentGroup, period, actor_id: str) -> List[UUID]:
    """Money-2 У4: the stay that has just been closed is of NO day (opened and closed on one business day without a counted mark, ``ls.stay_of_period``): the debt it had is gone, so the forgiveness
    given to it while it was open would stand as an advance (and the night would name it, I-1).  The live forgiveness of THIS stay is cancelled by the closing itself, in the same transaction, with a line
    of the journal of decisions (the author is the one who closes; the same line as the cancel by hand - no check of the age or of the role here); a debt that comes back later with a mark is an ordinary
    debt.  A forgiveness dated before the day the stay opened belongs to an earlier stay of the membership and is left alone.  Returns the ids of the cancelled forgivenesses."""
    day = ps.business_date(period.joined_at)
    if period.left_at is None or ps.business_date(period.left_at) != day:
        return []
    rows = (db.query(StudentWriteoff).filter(StudentWriteoff.membership_id == membership.id, StudentWriteoff.voided_at.is_(None), StudentWriteoff.effective_on >= day)
            .with_for_update().all())
    if not rows:
        return []
    org_id = db.query(Group.org_id).filter(Group.id == membership.group_id).scalar()
    loader = ls.LedgerLoader(db, str(org_id), day)
    loader.load_facts([membership.id])
    if ls.stay_of_period(period, loader.marked_of(membership.id)) is not None:
        return []
    gone = loader.forgiveness_to_cancel(membership.id, day)          # Ф1: only the forgiveness that would stand as an advance - one function with the window
    rows = [r for r in rows if r.id in gone]
    if not rows:
        return []
    pay = PaymentService(db)
    role = pay.role_of(org_id, actor_id)
    for row in rows:
        row.voided_at, row.voided_by, row.void_reason = _utcnow(), UUID(str(actor_id)), EMPTY_STAY_REASON
        pay._audit(org_id, membership, actor_id, role, AUDIT_WRITEOFF_VOID, {
            "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id), "writeoff_id": str(row.id), "amount": int(row.amount),
            "reason": row.void_reason, "effective_on": row.effective_on.isoformat(), "auto": True, "via": "stay_closed_same_day"})
    db.flush()
    return [r.id for r in rows]


def close_stay(db: Session, membership: StudentGroup, at: datetime, actor_id: str, via: str) -> None:
    """THE one way a stay ends - a leaving, a transfer, an archiving (Money-2: one rule, one function): the open period ends at ``at`` and the hooks of ``on_membership_closed`` run
    (the individual price ends, the scheduled prices are cancelled, the pauses are cut).  ``via``: ``leave`` / ``transfer`` / ``archive`` (the line of the journal says it)."""
    period = close_period(membership, at)
    db.flush()
    on_membership_closed(db, membership, at.replace(tzinfo=None), actor_id, via=via)
    if period is not None:
        cancel_forgiveness_of_empty_stay(db, membership, period, actor_id)


def on_membership_returned(db: Session, membership: StudentGroup, now: datetime, actor_id: Optional[str]) -> bool:
    """A student comes back to a group he left: the marker of the earlier leave that lies AFTER today (he came back the day he left) is taken
    back, and an individual price that is STILL in force then ends today - he is priced by the group (a price given with the return replaces
    that marker of the same day).  Returns True when a marker was written."""
    if not actor_id:
        return False
    today = ps.business_date(now)
    pricing.cancel_rows_after(db, membership.id, actor_id, today, markers=True, why=RETURN_REASON)
    books = pricing.load_books(db, [membership.group_id], [membership.id])
    price = books.price(membership.id, membership.group_id, today)
    if price.source == pricing.SOURCE_OVERRIDE and price.amount is not None:
        pricing.write_override(db, membership.id, actor_id, amount=None, valid_from=today, reason=RETURN_REASON)
        return True
    return False


class LeaveService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.pay = PaymentService(db)

    # ---- the account "after the leaving"

    def _after(self, org_id: str, membership_ids: Sequence[UUID], today: date):
        """``{membership_id: (ledger after leaving today, refundable on today, loader)}`` - the stays are closed on today in memory."""
        loader = ls.LedgerLoader(self.db, str(org_id), today, closing=set(membership_ids))      # Money-2 У2: the stays are judged as closed today - by the same rule as the record (a stay opened today and closed today charges nothing)
        loader.load_facts(list(membership_ids))
        out: Dict[UUID, Tuple[bl.Ledger, int]] = {}
        for mid in membership_ids:
            inp = loader.build_input(mid)
            if inp is None:
                continue
            closed = bl.sliced_input(inp, today)
            ledger = bl.run_ledger(closed)
            out[mid] = (ledger, min(ledger.refundable, bl.refundable_at(closed, today)))
        return out, loader

    def require_money_right_for_balance(self, org_id: str, membership_ids: Sequence[UUID], money: bool) -> None:
        """Money-1 A4 / Money-2 T11: a student who has PAID AHEAD or OWES leaves (or is archived) with his advance / debt and his paid-until date - the owner / an administrator with the right decides that.
        The one rule of the leaving and of the archive of a student; the text is the same for both and names no sum (403)."""
        if money:
            return
        after, _ = self._after(org_id, list(membership_ids), ps.business_today())
        if any(ledger.advance > 0 or ledger.debt > 0 for ledger, _refundable in after.values()):
            raise PaymentForbidden(BALANCE_NEEDS_MONEY_RIGHT)

    def _money(self, ledger: bl.Ledger, refundable: int, loader: ls.LedgerLoader, membership: StudentGroup, today: date, snap: Optional[leave_effects.Snap] = None) -> LeaveMoney:
        price = loader.books.price(membership.id, membership.group_id, today)
        named = {"debt_amount": snap.named_debt, "due_amount": snap.named_due, "due_until": snap.due_until, "becomes_debt": snap.turns} if snap is not None else {}
        return LeaveMoney(
            **named, unpaid=ledger.debt, advance=ledger.advance, refundable=refundable, paid_until=ledger.covered_through,
            charged_after_leave=ledger.charged, individual_price_ends=price.source == pricing.SOURCE_OVERRIDE and price.amount is not None)

    def _preview(self, org_id: str, memberships: Sequence[StudentGroup], groups: Dict[UUID, Group], money: bool, kind: str) -> LeavePreview:
        today = ps.business_today()
        after, loader = self._after(org_id, [m.id for m in memberships], today)
        snaps = {s.membership_id: s for s in leave_effects.snapshot(self.db, org_id, memberships, today, after=after, loader=loader)}
        items: List[LeaveMembership] = []
        for m in memberships:
            ledger, refundable = after[m.id]
            allowed = ["keep"] + (["writeoff"] if ledger.debt > 0 else []) + (["refund"] if refundable > 0 else [])
            items.append(LeaveMembership(
                membership_id=str(m.id), group_id=str(m.group_id), group_name=groups[m.group_id].name,
                money=self._money(ledger, refundable, loader, m, today, snaps[m.id]) if money else None,
                has_open_balance=ledger.debt > 0 or ledger.advance > 0, allowed=allowed if money else ["keep"],
                effects=leave_effects.build([snaps[m.id]], kind=kind, money=money)))
        return LeavePreview(as_of=today, memberships=items)

    def preview_group(self, org_id: str, group_id: UUID, student_id: UUID, *, money: bool) -> LeavePreview:
        student = self.db.query(Student).filter(Student.id == student_id, Student.org_id == org_id).first()
        group = self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).first()
        if not student or not group:
            raise PaymentNotFound("Ученик или группа не найдены")
        m = self.db.query(StudentGroup).filter(StudentGroup.student_id == student.id, StudentGroup.group_id == group.id,
                                               StudentGroup.is_active.is_(True)).first()
        if m is None:
            raise PaymentConflict("Ученик не в этой группе")
        return self._preview(org_id, [m], {group.id: group}, money, "leave")

    def preview_student(self, org_id: str, student_id: UUID, *, money: bool) -> LeavePreview:
        student = self.db.query(Student).filter(Student.id == student_id, Student.org_id == org_id).first()
        if not student:
            raise PaymentNotFound("Ученик не найден")
        ms = (self.db.query(StudentGroup).filter(StudentGroup.student_id == student.id, StudentGroup.is_active.is_(True))
              .order_by(StudentGroup.joined_at, StudentGroup.id).all())
        groups = {g.id: g for g in self.db.query(Group).filter(Group.id.in_([m.group_id for m in ms])).all()} if ms else {}
        return self._preview(org_id, ms, groups, money, "archive")

    # ---- settling (inside the caller's transaction; the stays are already closed and flushed)

    def settle(
        self, org_id: str, memberships: Sequence[StudentGroup], items: Sequence[SettleItem], actor_id: str, actor_role: str, *, money: bool, at: Optional[datetime] = None,
    ) -> List[SettledOut]:
        """Carry out the choices.  Raises ``PaymentError`` (the caller rolls back): 403 without access to money, 422 for a bad choice,
        409 ``BALANCE_CHANGED`` when the numbers moved.  ``at``: the one moment of the whole action (leave / archive) - the rows of the settlement carry it too."""
        todo = [i for i in items if i.action != "keep"]
        if not todo:
            return []
        if not money:
            raise PaymentForbidden("Нет доступа к оплатам")
        by_id = {str(m.id): m for m in memberships}
        seen = set()
        for item in todo:
            if item.membership_id not in by_id:
                raise PaymentInvalid("Это членство не закрывается этим действием")
            if item.membership_id in seen:
                raise PaymentInvalid("Одно членство указано дважды")
            seen.add(item.membership_id)
        today = bc.utc_to_business_date(at) if at is not None else ps.business_today()      # TIME-TESTS: the day of the action's own moment, not a new reading of the clock
        ids = [by_id[i.membership_id].id for i in todo]
        after, loader = self._after_closed(org_id, ids, today)
        # 1. every choice is checked against the numbers of NOW; any difference answers with ALL the fresh numbers
        stale = []
        for item in todo:
            ledger, refundable = after[by_id[item.membership_id].id]
            if item.action == "writeoff" and item.expected_unpaid is not None and item.expected_unpaid != ledger.debt:
                stale.append(item)
            if item.action == "refund" and item.expected_refundable is not None and item.expected_refundable != refundable:
                stale.append(item)
        if stale:
            raise BalanceChanged("Данные оплат изменились — проверьте суммы", fresh=[
                FreshBalance(membership_id=i.membership_id, debt=after[by_id[i.membership_id].id][0].debt,
                             advance=after[by_id[i.membership_id].id][0].advance,
                             refundable=after[by_id[i.membership_id].id][1]).model_dump() for i in todo])
        # 2. validation of what the choice needs
        out: List[SettledOut] = []
        now = (at.astimezone(timezone.utc).replace(tzinfo=None) if at is not None and at.tzinfo is not None else at) or _utcnow()
        stamps = bc.action_moments(now)          # the journal lines of the settlement: now + 1 us, + 2 us ... in the order of writing (the leave line itself stays at ``now``, before them)
        for item in todo:
            membership = by_id[item.membership_id]
            ledger, refundable = after[membership.id]
            if item.action == "writeoff":
                reason = (item.reason or "").strip()
                if not 3 <= len(reason) <= 500:
                    raise ReasonRequired("Причина: от 3 до 500 символов")
                if ledger.debt < 1:
                    raise PaymentInvalid("Долга нет — прощать нечего")
                row = StudentWriteoff(org_id=UUID(str(org_id)), membership_id=membership.id, student_id=membership.student_id,
                                      group_id=membership.group_id, amount=ledger.debt, reason=reason, effective_on=today,
                                      created_by=UUID(str(actor_id)), created_at=now)
                self.db.add(row)
                self.db.flush()
                self.pay._audit(org_id, membership, actor_id, actor_role, AUDIT_WRITEOFF, {
                    "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                    "writeoff_id": str(row.id), "amount": int(row.amount), "reason": reason, "effective_on": today.isoformat(),
                    "debt_before": ledger.debt, "debt_after": 0, "at_leave": True,
                    "expected_unpaid": item.expected_unpaid}, at=stamps.next())        # the numbers the client saw (null: it sent none) and the ones used
                out.append(SettledOut(membership_id=item.membership_id, action="writeoff", amount=int(row.amount), writeoff_id=str(row.id)))
            else:
                if not item.method:
                    raise MethodRequired("Укажите способ возврата")
                if refundable < 1:
                    raise PaymentInvalid("Возвращать нечего: аванса нет")
                row = self.pay._add_refund(          # Money-1 R3 (К3 = Д5): ONE way to create a refund - the number «В-n», the receipt of a refund (LESSONS 104)
                    org_id, lambda number: StudentRefund(
                        org_id=UUID(str(org_id)), membership_id=membership.id, student_id=membership.student_id,
                        group_id=membership.group_id, amount=refundable, method=item.method,
                        reason=(item.reason or "").strip() or "уход из группы", note=None, effective_on=today,
                        created_by=UUID(str(actor_id)), created_at=now, receipt_no=number))
                self.pay._audit(org_id, membership, actor_id, actor_role, AUDIT_REFUND, {
                    "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                    "refund_id": str(row.id), "amount": int(refundable), "method": item.method, "effective_on": today.isoformat(),
                    "advance_before": ledger.advance, "advance_after": max(0, ledger.advance - int(refundable)), "debt_before": ledger.debt, "debt_after": ledger.debt, "at_leave": True,
                    "expected_refundable": item.expected_refundable, "refundable_used": refundable}, at=stamps.next())
                out.append(SettledOut(membership_id=item.membership_id, action="refund", amount=int(refundable), refund_id=str(row.id)))
        self.pay._family_check(org_id, [m.id for m in memberships])
        return out

    def _after_closed(self, org_id: str, ids: Sequence[UUID], today: date):
        """The ledgers of memberships whose stay is ALREADY closed in the database (flushed): ``(ledger, refundable today)``."""
        loader = ls.LedgerLoader(self.db, str(org_id), today)
        loader.load_facts(list(ids))
        out = {}
        for mid in ids:
            inp = loader.build_input(mid)
            ledger = bl.run_ledger(inp)
            out[mid] = (ledger, min(ledger.refundable, bl.refundable_at(inp, today)))
        return out, loader

    # ---- the leaving itself

    @deadlock_guard          # m3: a PostgreSQL deadlock is a 409 «повторите», never a 500
    def leave(
        self, org_id: str, group_id: str, student_id: str, actor_id: str, actor_role: str, body: Optional[LeaveRequest] = None,
        *, money: bool,
    ) -> LeaveResult:
        """«Убрать из группы»: closing of the stay + settlement + hooks + audit, one transaction."""
        from .service import AuthService  # lazy: service is heavy

        svc = AuthService(self.db)
        try:
            student = (self.db.query(Student).filter(Student.id == student_id, Student.org_id == org_id).with_for_update(key_share=True).first())   # DM-006: rows with an FK to the student insert meanwhile (LESSONS 23)
            group = (self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).with_for_update().first())
            if not student or not group:
                raise ValueError("Ученик или группа не найдены")
            sg = (self.db.query(StudentGroup).filter(StudentGroup.student_id == student.id, StudentGroup.group_id == group.id,
                                                     StudentGroup.is_active.is_(True)).with_for_update(key_share=True).first())
            if not sg:
                raise PaymentConflict("Ученик не в этой группе")
            self.pay._lock_family(org_id, student.id)
            self.require_money_right_for_balance(org_id, [sg.id], money)
            svc._lock_groups_for_membership_change(org_id, [group.id])
            svc._assert_no_open_attendance_for_groups(org_id, [group.id])
            moment = bc.utc_now()   # DM-005: the one clock; TIME-TESTS: this ONE moment stamps the closed stay, the settlement rows, the day of the snapshot and the journal line of the leave
            before_close = leave_effects.snapshot(self.db, org_id, [sg], bc.utc_to_business_date(moment))      # Money-2 T3: read before the stay is closed
            now = moment.replace(tzinfo=timezone.utc)
            close_stay(self.db, sg, now, actor_id, "leave")
            if str(student.group_id or "") == str(group.id):
                successor = (self.db.query(StudentGroup).join(Group, Group.id == StudentGroup.group_id)
                             .filter(StudentGroup.student_id == student.id, StudentGroup.is_active.is_(True), Group.org_id == org_id)
                             .order_by((Group.status != "active"), StudentGroup.joined_at, StudentGroup.id).first())
                student.group_id = successor.group_id if successor else None
            settled = self.settle(org_id, [sg], body.settle if body else [], actor_id, actor_role, money=money, at=moment)
            self.db.add(GroupAudit(
                org_id=UUID(str(org_id)), group_id=group.id, actor_id=UUID(str(actor_id)),
                actor_role=actor_role if actor_role in ("owner", "admin", "teacher") else "admin",
                action=AUDIT_LEFT, assigned_teacher_id=group.teacher_id,
                snapshot={"student_id": str(student.id), "membership_id": str(sg.id), "reason": (body.reason if body else None),
                          "settled": [s.model_dump() for s in settled]},
                created_at=moment))
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        except ValueError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        return LeaveResult(message="Ученик удалён из группы", settled=settled, effects=leave_effects.build(leave_effects.after_settle(before_close, settled), kind="leave", money=money))


def apply_add_opening(db: Session, org_id: str, membership: StudentGroup, actor_id: str, opening, *, transfer: bool, source: str = "add") -> Optional[Dict[str, str]]:
    """The opening balance given with «Добавить в группу» (needs access to money - the router checks).  A transfer that closes other
    memberships carries money along: no opening balance then (422 ``OPENING_NOT_ALLOWED``)."""
    from .logica_service import OpeningService

    if transfer:
        raise OpeningNotAllowedAdd("Остаток нельзя внести: ученик переводится из другой группы, его деньги переходят вместе с ним")
    if opening.paid_until is None and opening.debt_amount is None and opening.debt_on is None:
        return None
    service = OpeningService(db)
    today = ps.business_today()
    cancelled: List[UUID] = []
    actor_role = service.pay.role_of(org_id, actor_id)          # Money-2 T19: the real role of the author in the line of the journal (never a hand-written one)
    row, _ = service.apply(org_id, membership, actor_id, actor_role, opening, source=source, today=today, reason=getattr(opening, "reason", None), require_reason_when_money=False, voided=cancelled)
    if cancelled:          # a return into a group that kept an opening balance replaces it: the cancelled row must be named in the journal (the first opening of a stay leaves none); the author is mandatory
        service._audit(org_id, membership, actor_id, actor_role, AUDIT_OPENING_SET if row else AUDIT_OPENING_VOID, {
            "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id), "opening_id": str(row.id) if row else None,
            "voided_opening_id": str(cancelled[0]), "via": source})
    return service.over_payments(org_id, membership.id, today) if opening.paid_until is not None else None       # Money-1 R3 Д4: «оплачено до» on top of payments is named
