"""PAY-F: an owner / admin changes the KIND of a lesson that is already cancelled (``centre`` <-> ``holiday``), with a reason, audited.

The recalculation for the lessons the centre cancelled is a function of the facts, so nothing else is written: the next read of the ledger shows or drops the
line.  Refused: a lesson that is not cancelled, a lesson cancelled by a day off of the centre (change the day off), and an old cancellation / a move (no kind: they
are neutral for the money and never recalculated)."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.models.auth import GroupAttendance, GroupAttendanceAudit

from .ops_rules import conflict, not_found
from .schemas import CancellationKind


class ChangeCancellationKind(BaseModel):
    lessonDate: date
    lessonId: str = Field(..., min_length=1, max_length=100)
    cancellationKind: CancellationKind
    reason: str = Field(..., min_length=3, max_length=500)


def change_cancellation_kind(db: Session, org_id: str, group_id: str, req: ChangeCancellationKind, *, actor_id: str) -> Dict[str, Any]:
    row = (db.query(GroupAttendance)
           .filter(GroupAttendance.org_id == org_id, GroupAttendance.group_id == group_id, GroupAttendance.lesson_date == req.lessonDate.isoformat(),
                   GroupAttendance.lesson_id == req.lessonId)
           .with_for_update().first())
    if row is None or row.status != "cancelled":
        raise not_found("LESSON_NOT_CANCELLED", "Урок не отменён: менять нечего")
    if row.day_off_id is not None:
        raise conflict("CANCELLED_BY_DAY_OFF", "Урок отменён выходным днём центра: измените или отмените сам выходной")
    if row.cancellation_kind is None:
        raise conflict("CANCELLATION_HAS_NO_KIND", "Это отмена до обновления или перенос урока: вид отмены у неё не меняется")
    before = row.cancellation_kind
    if before == req.cancellationKind:
        return {"ok": True, "changed": False, "cancellationKind": before, "version": row.version}
    row.cancellation_kind = req.cancellationKind
    row.version = int(row.version or 1) + 1
    db.add(GroupAttendanceAudit(
        attendance_id=row.id, org_id=org_id, group_id=group_id, actor_id=actor_id, action="corrected", before_status="cancelled", after_status="cancelled",
        before_records={}, after_records={}, correction_reason=req.reason.strip(), cancellation_reason=row.cancellation_reason,
        details={"cancellation_kind": {"from": before, "to": req.cancellationKind}}))
    db.commit()
    return {"ok": True, "changed": True, "cancellationKind": req.cancellationKind, "version": row.version}
