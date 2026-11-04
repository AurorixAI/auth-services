"""Money-1 A2: «Вернуть проведённый урок» (completed -> not held) and the impact of a correction of a held lesson (A3).

A held lesson was a one-way door (D2): a mistake of a teacher or an administrator ("Завершить" by a slip, a lesson that did not happen) could not be undone by anybody.  Now the owner / an
administrator with the payments right returns it to the state «не проведён»: the row goes back to ``in_progress`` (the marks, the excuses and the journal stay - the lesson is closed again
the usual way), it no longer spends the pack of any student and pays nobody.  Only in an OPEN period of the lesson's teacher (``closed_period.py``), with a reason, one audit row
``completed_reverted`` (who, when, why, the marks).  A repeat (the lesson is not held any more) changes nothing.

The return of «Урока не было» (a past cancelled lesson back to «Требует разбора») is the existing ``restore_lesson`` of ``Substitutions``, widened for the past (see ``lesson_cancel.py``).
Nothing here commits except ``return_completed_lesson``.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.orm.attributes import flag_modified

from src.models.auth import AttendanceExcuse, Group, GroupAttendance, GroupAttendanceAudit

from . import business_clock as bc
from . import money_effects as me
from .closed_period import ClosedPeriods, lesson_teacher_ids
from .ops_rules import OpsError, conflict, forbidden, not_found

COMPLETED, IN_PROGRESS = "completed", "in_progress"


class ReturnLessonRequest(BaseModel):
    lessonDate: date
    lessonId: str = Field(..., min_length=1, max_length=100)
    reason: str = Field("", max_length=2000)             # 3..500 after the trim is checked by the function (``REASON_REQUIRED``, a Russian sentence) - not by pydantic's English 422
    expectedVersion: Optional[int] = Field(None, ge=1)   # REQUIRED unless ``dryRun`` (R3 #11): a late duplicate of a request must not return the lesson a second time
    dryRun: bool = False


class AttendanceImpactRequest(BaseModel):
    """The body of a correction of a held lesson, without the reason: what would it do to money."""

    lessonDate: date
    lessonId: str = Field(..., min_length=1, max_length=100)
    records: Dict[str, str] = Field(default_factory=dict)


def excuse_statuses(db, attendance_id: Any) -> Dict[str, Optional[str]]:
    return {str(sid): status for sid, status in db.query(AttendanceExcuse.student_id, AttendanceExcuse.status).filter(AttendanceExcuse.attendance_id == attendance_id).all()}


def _row(db, org_id: Any, group_id: Any, lesson_id: str, day: date, *, lock: bool) -> Optional[GroupAttendance]:
    query = db.query(GroupAttendance).filter(GroupAttendance.org_id == org_id, GroupAttendance.group_id == group_id, GroupAttendance.lesson_id == lesson_id,
                                             GroupAttendance.lesson_date == day.isoformat())
    return (query.with_for_update().populate_existing() if lock else query).first()


def _assert_open(db, group: Group, row: GroupAttendance) -> list:
    teachers = lesson_teacher_ids(db, group, row.lesson_id, row.lesson_date, row)
    ClosedPeriods.of(db, group.org_id).assert_open(teachers, row.lesson_date)
    return teachers


def _reason(text: str) -> str:
    text = (text or "").strip()
    if not 3 <= len(text) <= 500:
        raise OpsError(422, "REASON_REQUIRED", "Причина: от 3 до 500 символов")
    return text


def return_completed_lesson(db, org_id: Any, group_id: Any, req: ReturnLessonRequest, *, actor_id: Any, owner: bool) -> Dict[str, Any]:
    reason = _reason(req.reason)
    if not req.dryRun and req.expectedVersion is None:
        raise OpsError(422, "VERSION_REQUIRED", "Обновите страницу и повторите: нужна текущая версия урока")
    group = db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).with_for_update().populate_existing().first()      # the order of every writer: the GROUP, then the journal row
    if group is None:
        raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
    row = _row(db, org_id, group.id, req.lessonId, req.lessonDate, lock=True)
    if row is None or row.status not in (COMPLETED, IN_PROGRESS):
        db.rollback()
        raise not_found("LESSON_NOT_HELD", "Проведённого урока нет: возвращать нечего")
    if row.status == IN_PROGRESS:                                   # a repeat (a double click, a lost answer): it is not held any more
        version = int(row.version or 1)
        db.rollback()
        return {"ok": True, "changed": False, "version": version, "status": IN_PROGRESS, "moneyEffects": {"students": [], "payroll": [], "text": "", "dryRun": False}}
    if req.expectedVersion is not None and int(req.expectedVersion) != int(row.version or 1):
        version = int(row.version or 1)
        db.rollback()
        raise conflict("ATTENDANCE_CONFLICT", "Отметки урока уже изменили. Обновите страницу", currentVersion=version)
    try:
        teachers = _assert_open(db, group, row)
    except OpsError:
        db.rollback()
        raise
    marks = {str(k): v for k, v in (row.records or {}).items()}
    statuses = excuse_statuses(db, row.id)
    students = me.student_deltas(marks, marks, statuses, statuses, held_before=True, held_after=False)
    probe = me.lesson_probe(db, org_id, teachers, row.lesson_date, owner)          # the saved sheets, measured BEFORE the lesson goes back
    before_status, before_records, current_version = row.status, dict(row.records or {}), int(row.version or 1)
    row.status, row.completed_at = IN_PROGRESS, None
    row.version = int(row.version or 1) + 1
    db.add(GroupAttendanceAudit(attendance_id=row.id, org_id=org_id, group_id=group.id, actor_id=actor_id, action="completed_reverted", before_status=before_status,
                                after_status=IN_PROGRESS, before_records=before_records, after_records=before_records, correction_reason=reason,
                                details={"students_released": len(students)}, created_at=bc.utc_now()))
    db.flush()
    effects = me.lesson_block(db, org_id, students=students, probe=probe, owner=owner, done=not req.dryRun, group_id=group.id)
    if req.dryRun:                                                   # the confirmation: everything above is thrown away
        db.rollback()
        return {"ok": True, "changed": False, "dryRun": True, "version": current_version, "status": COMPLETED, "moneyEffects": effects}
    db.commit()
    return {"ok": True, "changed": True, "version": int(row.version), "status": IN_PROGRESS, "moneyEffects": effects}


def correction_impact(db, org_id: Any, group_id: Any, req: AttendanceImpactRequest, *, owner: bool, money_right: bool = True, actor_id: Any = None) -> Dict[str, Any]:
    """What a correction of the marks of a HELD lesson would do to money - nothing is written (the correction is applied inside a transaction that is rolled back; the confirmation before «Сохранить»)."""
    group = db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).first()
    if group is None:
        raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
    row = _row(db, org_id, group.id, req.lessonId, req.lessonDate, lock=True)
    if row is None or row.status != COMPLETED:
        db.rollback()
        raise not_found("LESSON_NOT_HELD", "Проведённого урока нет")
    if not money_right:                                                # R3 #24: the teacher of THIS lesson may ask (the numbers of the pack, no sums of the sheet); nobody else without the right
        from .dependencies import MONEY_RIGHT_REQUIRED_TEXT

        if actor_id is None or str(actor_id) not in {str(t) for t in lesson_teacher_ids(db, group, req.lessonId, row.lesson_date, row)}:
            db.rollback()
            raise forbidden("MONEY_RIGHT_REQUIRED", MONEY_RIGHT_REQUIRED_TEXT)
    try:
        teachers = _assert_open(db, group, row)
        before = {str(k): v for k, v in (row.records or {}).items()}
        status_before = excuse_statuses(db, row.id)
        status_after = {sid: (status_before.get(sid, "pending") if mark == "excused" else None) for sid, mark in req.records.items()}   # a new «excused» waits for the decision: it still counts
        students = me.student_deltas(before, dict(req.records), status_before, status_after)
        probe = me.lesson_probe(db, org_id, teachers, row.lesson_date, owner)
        row.records = dict(req.records)
        flag_modified(row, "records")
        stale = [sid for sid, st in status_before.items() if st is not None and req.records.get(sid) != "excused"]
        if stale:                                                       # the marks that stop being «excused» lose their excuse (a confirmed one stops sparing the pay)
            db.query(AttendanceExcuse).filter(AttendanceExcuse.attendance_id == row.id, AttendanceExcuse.student_id.in_([UUID(x) for x in stale])).delete(synchronize_session=False)
        db.flush()
        block = me.lesson_block(db, org_id, students=students, probe=probe, owner=owner, done=False, group_id=group.id)
    finally:
        db.rollback()
    return {"ok": True, "moneyEffects": block}


def start_correction(db, group: Group, att: GroupAttendance, owner: bool):
    """BEFORE a correction of a held lesson is written: the saved sheets of its teacher, measured now (``finish_correction`` measures them after)."""
    teachers = lesson_teacher_ids(db, group, att.lesson_id, att.lesson_date, att)
    return me.lesson_probe(db, group.org_id, teachers, att.lesson_date, owner)


def finish_correction(db, group: Group, att: GroupAttendance, probe, before: Dict[str, Any], after: Dict[str, Any], excuses_before: Dict[str, Optional[str]], *, owner: bool) -> Dict[str, Any]:
    """The ``moneyEffects`` of a correction of a held lesson that has just been flushed: the packs of the students whose mark changed what it spends, and the saved sheets of the lesson's teacher
    whose pay THIS correction moved."""
    excuses_after = excuse_statuses(db, att.id)
    students = me.student_deltas({str(k): v for k, v in before.items()}, {str(k): v for k, v in after.items()}, excuses_before, excuses_after)
    return me.lesson_block(db, group.org_id, students=students, probe=probe, owner=owner, done=True, group_id=group.id)
