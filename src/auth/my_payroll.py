"""Money-1 V8: «Моя зарплата» - a teacher reads HIS month: lessons counted, the sum, paid, the rest (owner's decision 05.11).

No second formula (LESSONS 104; R3 Д1, Д2): the numbers are the owner's.  The sum of an open month is the FRESH calculation of the payroll itself with the ticks of the saved sheet
(``PayrollService.fresh_lines``), the sum of a closed month is the saved sheet, and «выплачено / остаток» of every month is ``PayrollPayoutService.state_in_month`` - the function of the list of the
owner, which knows an open month (a sheet saved inside it, a reopened one) from a closed one and takes the advance of the earlier months off.  Nothing about prices, students or debts leaves
this module (LESSONS 95): the answer is built from numbers and words only.  A finished month the owner has not saved at all is «не сохранена»."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import GroupAttendance, PayrollSheet

from . import payment_status as ps
from . import payments_texts as tx
from . import payroll_engine as engine
from .closed_period import ClosedPeriods, month_first
from .payroll_payouts import PayrollPayoutService
from .payroll_service import MonthInvalid, PayrollService, month_label, parse_month

PRELIMINARY_TEXT = "Предварительно: сумма может измениться до сохранения зарплаты"
NOT_SAVED_TEXT = "Зарплата за месяц ещё не сохранена"
NO_RATE_TEXT = "Сумма появится, когда владелец задаст ставку"
MONTHS_BACK = 12


def lessons_counted(lines: List[Dict[str, Any]]) -> int:
    """Distinct lessons that pay (counted, or unpaid with the tick «начислить») - the lessons of the teacher, not lesson x student."""
    seen = set()
    for line in lines:
        include = bool(line.get("include_unpaid"))
        for mark in engine.marks_of(line):
            if mark.state == engine.COUNTED or (include and mark.state == engine.UNPAID):
                seen.add((mark.day, mark.lesson_id))
    return len(seen)


def _balance_text(balance: int) -> str:
    if balance > 0:
        return f"К выплате {tx.money(balance)}"
    if balance < 0:
        return f"Выплачено вперёд {tx.money(-balance)}"
    return "Выплачено полностью"


class MyPayroll:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.payroll = PayrollService(db)
        self.payouts = PayrollPayoutService(db)

    def month(self, org_id: str, teacher_id: UUID, month: str) -> Dict[str, Any]:
        first, _last = parse_month(month)
        if first > month_first(ps.business_today()):
            raise MonthInvalid("Этот месяц ещё не наступил")
        sheet = self.db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == teacher_id, PayrollSheet.period_month == first).first()
        items = self.payouts.payouts_of(org_id, month=first, teacher_ids=[teacher_id]).get((teacher_id, first), [])
        carry = self.payouts.carry_ins(org_id, [teacher_id], first).get(teacher_id, 0)
        return self._build(org_id, teacher_id, first, sheet, items, carry)

    def _build(self, org_id: str, teacher_id: UUID, first: date, sheet: Optional[PayrollSheet], items: List[Any], carry: int) -> Dict[str, Any]:
        today_first = month_first(ps.business_today())
        closed = ClosedPeriods.of(self.db, org_id).closed_for([teacher_id], first) is not None
        out = {"month": first.strftime("%Y-%m"), "month_label": month_label(first), "state": "not_saved", "preliminary": False, "lessons_counted": 0, "total": None,
               "paid": 0, "balance": 0, "carry_in": 0, "text": NOT_SAVED_TEXT, "updated_at": None}
        if first < today_first and sheet is None:
            return out                                                    # the month is over and the owner has not saved a sheet at all
        if closed and sheet is not None:                                  # the saved sheet is the truth
            state = self.payouts.state_in_month(org_id, teacher_id, first, saved_total=int(sheet.total_amount), fresh_total=None, items=items, carry_in=carry)
            return {**out, "state": "saved", "lessons_counted": lessons_counted(sheet.lines or []), "total": int(sheet.total_amount), "paid": state.paid, "balance": state.balance,
                    "carry_in": state.carry_in, "text": _balance_text(state.balance), "updated_at": sheet.updated_at.isoformat() if sheet.updated_at else None}
        # an open month (the current one, a sheet saved inside it, a reopened one): the owner's fresh calculation with the ticks of the saved sheet, «предварительно»
        lines = self.payroll.fresh_lines(org_id, first, teacher_id, sheet)
        out.update(state="open_preliminary", preliminary=True, text=PRELIMINARY_TEXT, carry_in=carry)
        if lines is None:                                                 # a rate is missing / a manual price: the owner's list falls back on the saved sheet - so do we
            if sheet is None:
                out.update(text=NO_RATE_TEXT)
                return out
            lines, total = sheet.lines or [], int(sheet.total_amount)
            fresh = None
        else:
            total = engine.sheet_total(lines)
            fresh = total
        state = self.payouts.state_in_month(org_id, teacher_id, first, saved_total=int(sheet.total_amount) if sheet is not None else 0, fresh_total=fresh, items=items, carry_in=carry)
        out.update(lessons_counted=lessons_counted(lines), total=total, paid=state.paid, balance=state.balance, carry_in=state.carry_in,
                   updated_at=sheet.updated_at.isoformat() if sheet is not None and sheet.updated_at else None)
        return out

    def months(self, org_id: str, teacher_id: UUID) -> List[Dict[str, Any]]:
        """The current month and the last 12 in which the teacher has a sheet or held a lesson, newest first - one pass: the sheets and the payouts of the range in two queries, the advance
        chain once (R3 К11), the fresh calculation only for the months that are open."""
        current = month_first(ps.business_today())
        since = date(current.year - 1, current.month, 1)
        sheets = {s.period_month: s for s in self.db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == teacher_id, PayrollSheet.period_month >= since)}
        held = {date(int(r[0][:4]), int(r[0][5:7]), 1) for r in self.db.query(GroupAttendance.lesson_date).filter(
            GroupAttendance.org_id == org_id, GroupAttendance.teacher_id == teacher_id, GroupAttendance.status == "completed", GroupAttendance.lesson_date >= since.isoformat())}
        keys = sorted({current} | set(sheets) | held, reverse=True)
        payouts = self.payouts.payouts_of(org_id, teacher_ids=[teacher_id])
        carries = self.payouts.carry_chain(org_id, teacher_id, keys)
        return [self._build(org_id, teacher_id, first, sheets.get(first), payouts.get((teacher_id, first), []), carries.get(first, 0)) for first in keys]
