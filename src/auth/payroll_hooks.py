"""Interface between the operations package (B) and the payroll package (A) - SPEC 4.11.

The owner of this FILE is B; the implementation of the three functions is package A's (the signatures are the contract) - merged in n13-int.

Guarantee of B for A: the only field that says "who taught a lesson" is ``group_attendance.teacher_id`` (never ``marked_by``).  It is set
when the row is created from the lesson-teacher resolver (``lesson_teachers.py``) and changes afterwards only through
``PUT /org/groups/{id}/lesson-teacher`` (audit ``group_attendance_audit.teacher_changed`` + ``lesson_teacher_assignments``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .ops_rules import OpsError


class PayrollLocked(OpsError):
    """409 ``PAYROLL_LOCKED``: the month's sheet of one of the two teachers is already approved / paid."""

    def __init__(self, sheets: List[Dict[str, Any]]) -> None:
        super().__init__(
            409, "PAYROLL_LOCKED", "Ведомость за этот месяц уже утверждена: смена преподавателя недоступна", sheets=sheets
        )
        self.sheets = sheets


def assert_lesson_teacher_change_allowed(
    db, org_id, lesson_date: str, old_teacher_id: Optional[Any], new_teacher_id: Optional[Any]
) -> None:
    """LOGIC-A decision 6 (S-8): money that was paid out is never rewritten and a sheet has no "approved" state, so a change of "who taught"
    is never forbidden by payroll: both teachers' saved sheets become "data changed" through the fingerprint and a paid-out month shows
    "to pay N more" / "overpaid N".  ``PayrollLocked`` stays for a future lock; nothing raises it today."""
    return None


def sheets_touched_by_lesson_teacher_change(
    db, org_id, lesson_date: str, old_teacher_id: Optional[Any], new_teacher_id: Optional[Any]
) -> List[Dict[str, Any]]:
    """``[{"sheet_id", "teacher_id", "month", "status"}]`` of the SAVED sheets (of the lesson's month) of the two teachers - the sheets the
    change makes "data changed".  ``status`` = the payout state of the sheet (``unpaid | due | settled | overpaid``: live payouts against the
    saved total), so that the owner sees which of them were already paid out."""
    from datetime import date

    from sqlalchemy import func

    from .models import PayrollPayout, PayrollSheet

    teachers = [t for t in {old_teacher_id, new_teacher_id} if t]
    if not teachers:
        return []
    first_of_month = date.fromisoformat(str(lesson_date)[:7] + "-01")
    sheets = (
        db.query(PayrollSheet)
        .filter(
            PayrollSheet.org_id == org_id,
            PayrollSheet.teacher_id.in_(teachers),
            PayrollSheet.period_month == first_of_month,
        )
        .all()
    )
    out: List[Dict[str, Any]] = []
    for sheet in sheets:
        paid = (
            db.query(func.coalesce(func.sum(PayrollPayout.amount), 0))
            .filter(PayrollPayout.teacher_id == sheet.teacher_id, PayrollPayout.period_month == first_of_month,
                    PayrollPayout.org_id == org_id, PayrollPayout.voided_at.is_(None))
            .scalar()
        )
        total = int(sheet.total_amount or 0)
        status = "unpaid" if not paid else "settled" if paid == total else "due" if paid < total else "overpaid"
        out.append({"sheet_id": str(sheet.id), "teacher_id": str(sheet.teacher_id), "month": str(lesson_date)[:7], "status": status})
    return out


def after_enrol(db, student, membership, row) -> None:
    """Called after a student of an Excel row is enrolled (inside the row's transaction).  LOGIC-A (owner decision 1): a NEW student's
    opening balance (``row.opening`` = ``(paid_until, debt_amount, debt_on)`` of columns G-I, validated by the plan) is written through
    ``OpeningService`` with ``source="import"``; the row fails as a whole if the opening is refused."""
    opening = getattr(row, "opening", None)
    if not opening:
        return
    from . import payment_status as ps
    from .logica_service import OpeningService
    from .payments_schemas import OpeningIn

    paid_until, debt_amount, debt_on = opening
    db.flush()
    OpeningService(db).apply(
        str(row.org_id), membership, str(row.actor_id), row.actor_role,
        OpeningIn(paid_until=paid_until, debt_amount=debt_amount, debt_on=debt_on), source="import",
        today=ps.business_today(), require_reason_when_money=False)


# ---------------------------------------------------------------------------------------------- LOGIC-B2 <-> package A (SPEC 0 item 8 / 3.5)
def moved_one_off_lesson_keys(db, group_ids) -> set:
    """PACK A MERGE STUB.  ``{(group_id, lesson_id)}`` of the one-off lessons that a decision "reschedule" of LOGIC-B2 created.

    Package A's ``ledger_service._lesson_days`` counts the lesson days of the incomplete first period; the cancelled original is counted (A decision 3) and its
    moved copy must NOT be counted a second time, so that a move is neutral for the proration.  At the merge, ``_lesson_days`` skips the keys returned here
    (nothing else in package A changes).  Until then nothing calls this function - the payroll side is neutral by itself: a cancelled / moved row is never
    ``completed``, so nobody is paid for it, and the one-off lesson pays only when it is held."""
    from .lesson_substitutions import moved_one_off_keys

    return moved_one_off_keys(db, group_ids)
