"""Money-1 V8: «Моя зарплата» - a teacher reads HIS month: lessons counted, the sum, paid, the rest (owner's decision 05.11).

No second formula (LESSONS 104): the saved sum is the saved sheet, the payout block is ``payroll_payouts.state_of`` (the function of the list of the owner), a current month is the
fresh calculation of the payroll itself (``PayrollService._collect`` + ``_render``).  Nothing about prices, students or debts leaves this module (LESSONS 95): the answer is built from numbers
and words only.  A closed month is the SAVED sheet (the truth; a later change of the data does not move it under his eyes); an open one is «предварительно»; a finished month the owner has
not saved yet is «не сохранена»."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import GroupAttendance, PayrollSheet

from . import payment_status as ps
from . import payments_texts as tx
from . import payroll_engine as engine
from . import payroll_rates
from .closed_period import ClosedPeriods, month_first
from .payroll_payouts import PayrollPayoutService, state_of
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
        first, last = parse_month(month)
        today_first = month_first(ps.business_today())
        if first > today_first:
            raise MonthInvalid("Этот месяц ещё не наступил")
        sheet = self.db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == teacher_id, PayrollSheet.period_month == first).first()
        closed = ClosedPeriods.of(self.db, org_id).closed_for([teacher_id], first) is not None
        base = {"month": first.strftime("%Y-%m"), "month_label": month_label(first), "state": "not_saved", "preliminary": False, "lessons_counted": 0, "total": None,
                "paid": 0, "balance": 0, "carry_in": 0, "text": NOT_SAVED_TEXT, "updated_at": None}
        if first < today_first and not closed:
            return base                                                   # the month is over and the owner has not saved it (or has reopened it)
        items = self.payouts.payouts_of(org_id, month=first, teacher_ids=[teacher_id]).get((teacher_id, first), [])
        carry = self.payouts.carry_ins(org_id, [teacher_id], first).get(teacher_id, 0)
        if closed and sheet is not None:
            state = state_of(saved_total=int(sheet.total_amount), fresh_total=None, items=items, carry_in=carry, month_open=False)
            return {**base, "state": "saved", "lessons_counted": lessons_counted(sheet.lines or []), "total": int(sheet.total_amount), "paid": state.paid, "balance": state.balance,
                    "carry_in": state.carry_in, "text": _balance_text(state.balance), "updated_at": sheet.updated_at.isoformat() if sheet.updated_at else None}
        # the current month (or a reopened one): the fresh calculation of the payroll, «предварительно»
        seed = self.payroll._collect(org_id, first, last, teacher_ids={teacher_id})
        teacher = seed.teachers.get(teacher_id)
        out = {**base, "state": "open_preliminary", "preliminary": True, "text": PRELIMINARY_TEXT, "carry_in": carry}
        if teacher is None:
            out.update(total=0, balance=0)
            return out
        percents = payroll_rates.load_book(self.db, org_id, [teacher_id]).percents_at(teacher_id, first)
        if self.payroll._missing_percents(teacher, percents):
            out.update(text=NO_RATE_TEXT)
            return out
        lines = self.payroll._render(teacher, percents, {}, seed.divisor)
        total = engine.sheet_total(lines)
        state = state_of(saved_total=int(sheet.total_amount) if sheet is not None else 0, fresh_total=total, items=items, carry_in=carry, month_open=True)
        out.update(lessons_counted=lessons_counted(lines), total=total, paid=state.paid, balance=state.balance, carry_in=state.carry_in)
        return out

    def months(self, org_id: str, teacher_id: UUID) -> List[Dict[str, Any]]:
        """The current month and the last 12 in which the teacher has a sheet or held a lesson, newest first."""
        current = month_first(ps.business_today())
        since = date(current.year - 1, current.month, 1)
        keys = {current.strftime("%Y-%m")}
        keys |= {r[0].strftime("%Y-%m") for r in self.db.query(PayrollSheet.period_month).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == teacher_id, PayrollSheet.period_month >= since)}
        held = {r[0][:7] for r in self.db.query(GroupAttendance.lesson_date).filter(GroupAttendance.org_id == org_id, GroupAttendance.teacher_id == teacher_id,
                                                                                  GroupAttendance.status == "completed", GroupAttendance.lesson_date >= since.isoformat()).all()}
        return [self.month(org_id, teacher_id, key) for key in sorted(keys | held, reverse=True)]
