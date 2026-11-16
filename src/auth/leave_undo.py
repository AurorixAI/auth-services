"""
ALGO - Money-2 / T6: a student who comes back to the SAME group on the business day he left it (or was transferred out of it) has not left: the leaving is taken back.

The stay goes on as if the leaving had not happened - the individual price (the marker that ended it is removed), the prices scheduled for later and the pauses that the leaving ended are
restored, each by the lines of the journal of THAT leaving (the price audit and the pause audit of ``close_stay``).  The periods of the stay touch (``merge_stays``): the day of leaving is charged once.
A transfer back (``mode=transfer`` from the group he went to) takes the same way; the money comes home by the ordinary transfer back (the new group has a stay of zero days: it charges nothing, rule G-8).

NOT undone (an ordinary return, the window says why): a refund of an advance or a forgiveness of a debt made after the leaving (the money moved by that decision stays moved), and a leaving by a transfer when the
student does not come back from the group he was moved to (he is in both groups then - two decisions of the owner, not one cancelled).  The next day and later it is an ordinary return (rules Р3-1, Р3-1б).

``try_undo`` writes inside the caller's transaction and never commits; the preview (dry run) runs the same code and is rolled back, so the window says what the server writes.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import Group, GroupAudit, MembershipPriceOverride, MembershipTransfer, StudentGroup, StudentRefund, StudentWriteoff

from . import payment_status as ps
from . import payments_texts as tx
from . import pricing
from .leave_effects import _dmy, _range
from .membership_periods import naive_utc

AUDIT_LEFT = "student_left_group"
LEFT_REASON = "left_group"
UNDONE_REASON = "leave_undone"


def _item(code: str, text: str, **fields: Any) -> Dict[str, Any]:
    return {"code": code, "text": text, **fields}


def _refusal(text: str) -> Dict[str, Any]:
    return {"undone": False, "restored": [], "text": text}


def left_today(left_at: Optional[datetime], today: date) -> bool:
    """The stay that ended at ``left_at`` ended on the business day ``today``."""
    return left_at is not None and ps.business_date(left_at) == today


def _blocker(db: Session, membership: StudentGroup, since: datetime, coming_from: Sequence[Any], money: bool) -> Optional[str]:
    """What forbids the cancel: ``None`` when nothing does, else the sentence for the window."""
    refund = db.query(StudentRefund).filter(StudentRefund.membership_id == membership.id, StudentRefund.voided_at.is_(None), StudentRefund.created_at >= since).first()
    if refund is not None:
        what = f"возвращён аванс {tx.money(int(refund.amount))}" if money else "возвращён аванс"
        return f"Возврат обычный, а не отмена ухода: после ухода ученику {what}. Цена и паузы не восстанавливаются"
    forgiven = db.query(StudentWriteoff).filter(StudentWriteoff.membership_id == membership.id, StudentWriteoff.voided_at.is_(None), StudentWriteoff.created_at >= since).first()
    if forgiven is not None:
        what = f"прощён долг {tx.money(int(forgiven.amount))}" if money else "прощён долг"
        return f"Возврат обычный, а не отмена ухода: после ухода ученику {what}. Цена и паузы не восстанавливаются"
    link = db.query(MembershipTransfer).filter(MembershipTransfer.from_membership_id == membership.id, MembershipTransfer.voided_at.is_(None), MembershipTransfer.at >= since).first()
    if link is not None and str(link.to_membership_id) not in {str(x) for x in coming_from}:
        name = db.query(Group.name).join(StudentGroup, StudentGroup.group_id == Group.id).filter(StudentGroup.id == link.to_membership_id).scalar()
        return f"Возврат обычный, а не отмена ухода: сегодня ученика перевели в группу «{name}». Отменить перевод можно обратным переводом из неё"
    return None


def try_undo(
    db: Session, org_id: str, membership: StudentGroup, left_at: Optional[datetime], now: datetime, actor_id: str,
    *, coming_from: Sequence[Any], money: bool,
) -> Optional[Dict[str, Any]]:
    """``None`` when the stay did not end today (an ordinary return, nothing to say).  Else ``{"undone": bool, "restored": [...], "text": str}``: the cancel is done, or refused with the reason.

    ``left_at``: when the stay that ended (the one being come back to) ended - the caller reads it BEFORE ``open_period`` reopens the membership.  ``coming_from``: the ids of the memberships the student is
    transferred out of by this very request (the way back from the group he was moved to).  ``money``: the caller may see sums."""
    today = ps.business_date(now)
    if not left_today(left_at, today):
        return None
    since = naive_utc(left_at)
    reason = _blocker(db, membership, since, coming_from, money)
    if reason is not None:
        return _refusal(reason)
    mid = membership.id
    restored: List[Dict[str, Any]] = []
    ids: List[str] = []
    books = pricing.load_books(db, [membership.group_id], [mid])
    price = books.price(mid, membership.group_id, today)
    with_sum = price.source == pricing.SOURCE_OVERRIDE and price.amount is not None
    marker_rows = [r for r in db.query(MembershipPriceOverride).filter(MembershipPriceOverride.membership_id == mid, MembershipPriceOverride.valid_from > today).all()
                   if r.amount is None and r.reason == pricing.LEFT_GROUP_MARKER]
    marker_ids = [str(r.id) for r in marker_rows]
    pricing.cancel_rows_after(db, mid, actor_id, today, markers=True, why=UNDONE_REASON)          # the individual price goes on: the marker that ended it is taken away
    if marker_ids:
        ids += marker_ids
        restored.append(_item("individual_price", (f"Индивидуальная цена {tx.money(int(price.amount))} продолжается" if with_sum and money else "Индивидуальная цена продолжается"),
                              **({"amount": int(price.amount)} if with_sum and money else {})))
    cancelled = [g for g in db.query(GroupAudit).filter(
        GroupAudit.org_id == org_id, GroupAudit.group_id == membership.group_id, GroupAudit.action == pricing.ACTION_MEMBERSHIP_PRICE_CANCELLED, GroupAudit.created_at >= since).order_by(GroupAudit.created_at).all()
        if (g.snapshot or {}).get("membership_id") == str(mid) and (g.snapshot or {}).get("cancelled_because") == LEFT_REASON]
    for g in cancelled:                                                                           # the prices scheduled for later come back as they were (same day, amount, reason)
        s = g.snapshot
        valid_from = date.fromisoformat(s["valid_from"])
        if db.query(MembershipPriceOverride).filter_by(membership_id=mid, valid_from=valid_from).first() is not None:
            continue
        row = pricing.write_override(db, mid, actor_id, amount=s["amount"], valid_from=valid_from, reason=s["reason"], extra={"restored_because": UNDONE_REASON})
        ids.append(str(row.id))
        text = (f"Запланированная цена {tx.money(int(s['amount']))} с {_dmy(valid_from)} восстановлена" if s["amount"] is not None and money
                else f"Запланированная цена с {_dmy(valid_from)} восстановлена")
        restored.append(_item("scheduled_price", text, valid_from=valid_from.isoformat(), **({"amount": int(s["amount"])} if s["amount"] is not None and money else {})))
    from .logica_service import PauseService  # lazy: pulls in the payments modules

    for pause, first, last in PauseService(db).restore_after_leave(org_id, membership, since, actor_id):
        ids.append(str(pause.id))
        restored.append(_item("pause", f"Пауза {_range(first, last)} восстановлена", first_day=first.isoformat(), last_day=last.isoformat()))
    text = "Ученик вернулся в день ухода — уход отменён: " + "; ".join(i["text"][0].lower() + i["text"][1:] for i in restored) if restored else "Ученик вернулся в день ухода — уход отменён"
    from .payments_service import PaymentService  # lazy

    pay = PaymentService(db)
    pay._audit(org_id, membership, actor_id, pay.role_of(org_id, actor_id), AUDIT_LEFT, {
        "undone": True, "event": "leave_undone", "actor_id": str(actor_id), "membership_id": str(mid), "student_id": str(membership.student_id), "left_at": since.isoformat(),
        "restored": ids, "coming_from": [str(x) for x in coming_from]})
    return {"undone": True, "restored": restored, "text": text}
