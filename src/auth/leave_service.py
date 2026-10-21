"""
ALGO - LOGIC-A (F9, F13): a student leaves a group / is archived WITH his money.

Before the leaving the server shows the account «after the leaving» (the ledger with every stay closed today) and the owner chooses per
membership: ``keep`` (default), ``writeoff`` (only a debt; the reason is mandatory) or ``refund`` (only a refundable advance; the method is
mandatory).  The choice is carried out in the SAME transaction as the leaving, after the stay is closed; the numbers the client saw
(``expected_debt`` / ``expected_refundable``) are checked again under the locks - a difference is 409 ``BALANCE_CHANGED`` with the fresh
numbers and nothing is done.  A debt that is kept can still be paid after the leaving (S-5: not more than the debt).

Closing a stay also (F13) ends the individual price of the membership the day after, and (decision 2) cuts / cancels its pauses - the same
hooks run for «Убрать из группы», for a transfer (``add_student_to_group``) and for the archiving.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import Group, GroupAudit, MembershipPause, Student, StudentGroup, StudentRefund, StudentWriteoff

from . import billing_ledger as bl
from . import ledger_service as ls
from . import membership_periods
from . import payment_status as ps
from . import pricing
from .logica_service import PauseService
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
    MAX_MONEY,
    AmountRange,
    MethodRequired,
    PaymentConflict,
    PaymentError,
    PaymentForbidden,
    PaymentInvalid,
    PaymentNotFound,
    PaymentService,
    ReasonRequired,
    _num,
    _utcnow,
    invalidate_summary,
)

logger = logging.getLogger(__name__)

AUDIT_LEFT = "student_left_group"
LEFT_GROUP_REASON = "left_group"
RETURN_REASON = "return_group"


class BalanceChanged(PaymentConflict):
    code = "BALANCE_CHANGED"


class OpeningNotAllowedAdd(PaymentInvalid):
    code = "OPENING_NOT_ALLOWED"


# ---- hooks run whenever a stay is closed / reopened ------------------------------------------------------------


def on_membership_closed(db: Session, membership: StudentGroup, left_at: datetime, actor_id: Optional[str]) -> None:
    """F13 + pauses.  The caller closed the period (``close_period``) and holds the locks; nothing is committed here.

    The individual price (with a sum) in force on the last day ends the day after - a return is then priced by the group; the pauses after
    the last day are cancelled and the one that holds it is cut to it.  Without an author (a legacy caller) the marker is not written: the
    price audit has no row without an actor - the return hook below then closes the same gap."""
    left_day = ps.business_date(left_at)
    if actor_id:
        books = pricing.load_books(db, [membership.group_id], [membership.id])
        price = books.price(membership.id, membership.group_id, left_day)
        if price.source == pricing.SOURCE_OVERRIDE and price.amount is not None:
            pricing.write_override(db, membership.id, actor_id, amount=None, valid_from=left_day.fromordinal(left_day.toordinal() + 1),
                                   reason=LEFT_GROUP_REASON)
    PauseService(db).close_at_leave(membership.id, left_day, actor_id)


def on_membership_returned(db: Session, membership: StudentGroup, now: datetime, actor_id: Optional[str]) -> bool:
    """A student comes back to a group he left: an individual price that is STILL in force (older data, or he came back the day he left)
    ends today - he is priced by the group.  Returns True when a marker was written."""
    if not actor_id:
        return False
    today = ps.business_date(now)
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
        loader = ls.LedgerLoader(self.db, str(org_id), today)
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

    def _money(self, ledger: bl.Ledger, refundable: int, loader: ls.LedgerLoader, membership: StudentGroup, today: date) -> LeaveMoney:
        price = loader.books.price(membership.id, membership.group_id, today)
        return LeaveMoney(
            debt=ledger.debt, advance=ledger.advance, refundable=refundable, paid_until=ledger.covered_through,
            charged_after_leave=ledger.charged, individual_price_ends=price.source == pricing.SOURCE_OVERRIDE and price.amount is not None)

    def _preview(self, org_id: str, memberships: Sequence[StudentGroup], groups: Dict[UUID, Group], money: bool) -> LeavePreview:
        today = ps.business_today()
        after, loader = self._after(org_id, [m.id for m in memberships], today)
        items: List[LeaveMembership] = []
        for m in memberships:
            ledger, refundable = after[m.id]
            allowed = ["keep"] + (["writeoff"] if ledger.debt > 0 else []) + (["refund"] if refundable > 0 else [])
            items.append(LeaveMembership(
                membership_id=str(m.id), group_id=str(m.group_id), group_name=groups[m.group_id].name,
                money=self._money(ledger, refundable, loader, m, today) if money else None,
                has_open_balance=ledger.debt > 0 or ledger.advance > 0, allowed=allowed if money else ["keep"]))
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
        return self._preview(org_id, [m], {group.id: group}, money)

    def preview_student(self, org_id: str, student_id: UUID, *, money: bool) -> LeavePreview:
        student = self.db.query(Student).filter(Student.id == student_id, Student.org_id == org_id).first()
        if not student:
            raise PaymentNotFound("Ученик не найден")
        ms = (self.db.query(StudentGroup).filter(StudentGroup.student_id == student.id, StudentGroup.is_active.is_(True))
              .order_by(StudentGroup.joined_at, StudentGroup.id).all())
        groups = {g.id: g for g in self.db.query(Group).filter(Group.id.in_([m.group_id for m in ms])).all()} if ms else {}
        return self._preview(org_id, ms, groups, money)

    # ---- settling (inside the caller's transaction; the stays are already closed and flushed)

    def settle(
        self, org_id: str, memberships: Sequence[StudentGroup], items: Sequence[SettleItem], actor_id: str, actor_role: str, *, money: bool,
    ) -> List[SettledOut]:
        """Carry out the choices.  Raises ``PaymentError`` (the caller rolls back): 403 without access to money, 422 for a bad choice,
        409 ``BALANCE_CHANGED`` when the numbers moved."""
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
        today = ps.business_today()
        ids = [by_id[i.membership_id].id for i in todo]
        after, loader = self._after_closed(org_id, ids, today)
        # 1. every choice is checked against the numbers of NOW; any difference answers with ALL the fresh numbers
        stale = []
        for item in todo:
            ledger, refundable = after[by_id[item.membership_id].id]
            if item.action == "writeoff" and item.expected_debt is not None and item.expected_debt != ledger.debt:
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
        now = _utcnow()
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
                    "debt_before": ledger.debt, "debt_after": 0, "at_leave": True})
                out.append(SettledOut(membership_id=item.membership_id, action="writeoff", amount=int(row.amount), writeoff_id=str(row.id)))
            else:
                if not item.method:
                    raise MethodRequired("Укажите способ возврата")
                if refundable < 1:
                    raise PaymentInvalid("Возвращать нечего: аванса нет")
                row = StudentRefund(org_id=UUID(str(org_id)), membership_id=membership.id, student_id=membership.student_id,
                                    group_id=membership.group_id, amount=refundable, method=item.method,
                                    reason=(item.reason or "").strip() or "уход из группы", note=None, effective_on=today,
                                    created_by=UUID(str(actor_id)), created_at=now)
                self.db.add(row)
                self.db.flush()
                self.pay._audit(org_id, membership, actor_id, actor_role, AUDIT_REFUND, {
                    "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                    "refund_id": str(row.id), "amount": int(refundable), "method": item.method, "effective_on": today.isoformat(),
                    "advance_before": ledger.advance, "at_leave": True})
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

    def leave(
        self, org_id: str, group_id: str, student_id: str, actor_id: str, actor_role: str, body: Optional[LeaveRequest] = None,
        *, money: bool = False,
    ) -> LeaveResult:
        """«Убрать из группы» (the same closing as ``AuthService.remove_student_from_group``) + settlement + hooks + audit, one transaction."""
        from .service import AuthService  # lazy: service is heavy

        svc = AuthService(self.db)
        try:
            student = (self.db.query(Student).filter(Student.id == student_id, Student.org_id == org_id).with_for_update().first())
            group = (self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).with_for_update().first())
            if not student or not group:
                raise ValueError("Ученик или группа не найдены")
            sg = (self.db.query(StudentGroup).filter(StudentGroup.student_id == student.id, StudentGroup.group_id == group.id,
                                                     StudentGroup.is_active.is_(True)).with_for_update(key_share=True).first())
            if not sg:
                raise PaymentConflict("Ученик не в этой группе")
            self.pay._lock_family(org_id, student.id)
            svc._lock_groups_for_membership_change(org_id, [group.id])
            svc._assert_no_open_attendance_for_groups(org_id, [group.id])
            now = datetime.now(timezone.utc)
            close_period(sg, now)
            self.db.flush()
            if str(student.group_id or "") == str(group.id):
                successor = (self.db.query(StudentGroup).join(Group, Group.id == StudentGroup.group_id)
                             .filter(StudentGroup.student_id == student.id, StudentGroup.is_active.is_(True), Group.org_id == org_id)
                             .order_by((Group.status != "active"), StudentGroup.joined_at, StudentGroup.id).first())
                student.group_id = successor.group_id if successor else None
            on_membership_closed(self.db, sg, now.replace(tzinfo=None), actor_id)
            settled = self.settle(org_id, [sg], body.settle if body else [], actor_id, actor_role, money=money)
            self.db.add(GroupAudit(
                org_id=UUID(str(org_id)), group_id=group.id, actor_id=UUID(str(actor_id)),
                actor_role=actor_role if actor_role in ("owner", "admin", "teacher", "superadmin") else "admin",
                action=AUDIT_LEFT, assigned_teacher_id=group.teacher_id,
                snapshot={"student_id": str(student.id), "membership_id": str(sg.id), "reason": (body.reason if body else None),
                          "settled": [s.model_dump() for s in settled]}))
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        except ValueError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        return LeaveResult(message="Ученик удалён из группы", settled=settled)


def apply_add_opening(db: Session, org_id: str, membership: StudentGroup, actor_id: Optional[str], opening, *, transfer: bool) -> None:
    """The opening balance given with «Добавить в группу» (needs access to money - the router checks).  A transfer that closes other
    memberships carries money along: no opening balance then (422 ``OPENING_NOT_ALLOWED``)."""
    from .logica_service import OpeningService

    if transfer:
        raise OpeningNotAllowedAdd("Остаток нельзя внести: ученик переводится из другой группы, его деньги переходят вместе с ним")
    if opening.paid_until is None and opening.debt_amount is None and opening.debt_on is None:
        return
    OpeningService(db).apply(org_id, membership, actor_id, "admin", opening, source="add", today=ps.business_today(),
                             require_reason_when_money=False)
