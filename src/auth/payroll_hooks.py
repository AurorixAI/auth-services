"""Interface between the operations package (B) and the payroll package (A) - SPEC 4.11.

The owner of this FILE is B; the implementation of the three functions is package A's (the signatures are the contract) - merged in n13-int.

Guarantee of B for A: the only field that says "who taught a lesson" is ``group_attendance.teacher_id`` (never ``marked_by``).  It is set
when the row is created from the lesson-teacher resolver (``lesson_teachers.py``) and changes afterwards only through
``PUT /org/groups/{id}/lesson-teacher`` (audit ``group_attendance_audit.teacher_changed`` + ``lesson_teacher_assignments``).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def sheets_touched_by_lesson_teacher_change(
    db, org_id, lesson_date: str, old_teacher_id: Optional[Any], new_teacher_id: Optional[Any], *, owner: bool
) -> List[Dict[str, Any]]:
    """The SAVED sheets (of the lesson's month) of the two teachers - the sheets the change makes "data changed".

    A change of "who taught" is never forbidden by payroll: a sheet has no "approved" state, money that was paid out is not rewritten, both
    teachers' saved sheets become "data changed" through the fingerprint and the difference shows in the comparison.

    LESSONS 45: the state of a teacher's money belongs to the owner.  ``owner=True`` -> ``[{"sheet_id", "teacher_id", "month", "status"}]`` (``status`` = the payout state
    ``unpaid | due | settled | overpaid``: live payouts against the saved total); otherwise only ``[{"month"}]`` (one entry, no status, sheet or teacher), so that the
    administrator still sees «Ведомость за октябрь изменится»."""
    from datetime import date

    from sqlalchemy import func

    from src.models.auth import PayrollPayout, PayrollSheet

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
    if not owner:
        return [{"month": str(lesson_date)[:7]}] if sheets else []
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
    """Extension point called after a student of an Excel row is enrolled.  LOGIC-A (owner decision 1): the opening balance of a NEW student ("paid
    until" / "debt on a date", columns G-I) is NOT written here - ``import_ops._enrol`` hands it to ``AuthService.add_student_to_group(opening_balance=...,
    opening_source="import")``, so the enrolment and the balance are one transaction (a refused balance rolls the whole row back).  Nothing left to do."""
    return None


# ---------------------------------------------------------------------------------------------- LOGIC-B2 <-> package A (SPEC 0 item 8 / 3.5)
def moved_one_off_lesson_keys(db, group_ids) -> set:
    """PACK A MERGE STUB.  ``{(group_id, lesson_id)}`` of the one-off lessons that a decision "reschedule" of LOGIC-B2 created.

    Package A's ``ledger_service._lesson_days`` counts the lesson days of the incomplete first period; the cancelled original is counted (A decision 3) and its
    moved copy must NOT be counted a second time, so that a move is neutral for the proration.  At the merge, ``_lesson_days`` skips the keys returned here
    (nothing else in package A changes).  Until then nothing calls this function - the payroll side is neutral by itself: a cancelled / moved row is never
    ``completed``, so nobody is paid for it, and the one-off lesson pays only when it is held."""
    from .lesson_substitutions import moved_one_off_keys

    return moved_one_off_keys(db, group_ids)
