"""Money-1 (A1): the ONE rule of a closed period for the actions that move lesson money.

A month of a teacher is CLOSED when the last sheet of it was saved after the month ended (the business time of the centre, ``utc_to_business_wall``) - variant C of
``area-payroll.md`` 6.  A sheet saved in the middle of the month (the owner pays an advance on the 15th) and an advance do not close it; the next save after the month's end does.
An owner / an administrator with the payments right may REOPEN a closed month (a reason, the journal ``payroll_period_reopenings``); a reopening lasts until the sheet is saved
again - the save after the correction closes the month again by the same rule, nothing else is stored.

Who is asked: the teacher of the lesson (the teacher frozen in its row, else the resolver of ``lesson_teachers``) in the month of the lesson date; a change of the teacher of a lesson
asks both teachers.  A lesson with no teacher has no sheet to protect.  The old rules this one replaces: the window «from the 1st of the previous month» (marks, cancellations, kind of a
cancellation, change of the teacher, days off, substitutions), the 30 days of a pause and the missing limit of a correction and of a decision on an excuse.

Payments are NOT under this rule (ДП-07): cancelling / correcting a payment is always allowed to the owner / an administrator with the right; when a closed month is touched the answer
only says so (``touched_months``), see ``money_effects.py``.

Nothing here commits.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from uuid import UUID

from src.models.auth import PayrollPeriodReopening, PayrollSheet, User

from . import business_clock as bc
from .ops_rules import OpsError, conflict, short_name

PERIOD_CLOSED = "PERIOD_CLOSED"
MONTH_NAMES = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")


def _uuid(value: Any) -> Optional[UUID]:
    if value is None:
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def month_first(day: Any) -> date:
    """The first day of the month of a date, an ISO date string or a ``YYYY-MM`` string."""
    if isinstance(day, str):
        day = date.fromisoformat(day[:10] if len(day) >= 10 else day + "-01")
    return day.replace(day=1)


def month_last(first: date) -> date:
    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


def month_title(first: date) -> str:
    return f"{MONTH_NAMES[first.month - 1]} {first.year}"


@dataclass(frozen=True)
class ClosedMonth:
    teacher_id: UUID
    month: date            # the first day
    closed_on: date        # the business day the closing sheet was saved
    sheet_id: UUID

    @property
    def label(self) -> str:
        return self.month.strftime("%Y-%m")


def state_of_sheet(sheet: PayrollSheet, last_reopened_at: Optional[Any]) -> Tuple[str, date]:
    """``(state, saved_on)`` of a saved sheet: ``closed`` | ``reopened`` | ``open``.  Pure: the one place where the rule of the module docstring is written."""
    saved_on = bc.utc_to_business_wall(sheet.updated_at).date()
    if saved_on <= month_last(sheet.period_month):
        return "open", saved_on                      # saved inside the month: an advance, a look at the Excel
    if last_reopened_at is not None and last_reopened_at > sheet.updated_at:
        return "reopened", saved_on
    return "closed", saved_on


class ClosedPeriods:
    """The closed months of ONE centre, loaded once (a handful of rows: teachers x months).  Create one per operation - it is a snapshot, not a cache."""

    def __init__(self, db, org_id: Any) -> None:
        self.db = db
        self.org_id = org_id
        self._closed: Optional[Dict[date, Dict[UUID, ClosedMonth]]] = None

    # ------------------------------------------------------------------------------------------------ the snapshot
    def _load(self) -> Dict[date, Dict[UUID, ClosedMonth]]:
        if self._closed is not None:
            return self._closed
        sheets = self.db.query(PayrollSheet).filter(PayrollSheet.org_id == self.org_id).all()
        last_reopen: Dict[Tuple[UUID, date], Any] = {}
        if sheets:
            for teacher_id, month, latest in (
                self.db.query(PayrollPeriodReopening.teacher_id, PayrollPeriodReopening.period_month, PayrollPeriodReopening.created_at)
                .filter(PayrollPeriodReopening.org_id == self.org_id).all()
            ):
                key = (teacher_id, month)
                if key not in last_reopen or latest > last_reopen[key]:
                    last_reopen[key] = latest
        out: Dict[date, Dict[UUID, ClosedMonth]] = {}
        for sheet in sheets:
            state, saved_on = state_of_sheet(sheet, last_reopen.get((sheet.teacher_id, sheet.period_month)))
            if state == "closed":
                out.setdefault(sheet.period_month, {})[sheet.teacher_id] = ClosedMonth(sheet.teacher_id, sheet.period_month, saved_on, sheet.id)
        self._closed = out
        return out

    def any_closed(self, day: Any) -> bool:
        """Cheap pre-check for a loop over many lessons: is any teacher's month of this date closed at all (then the teacher of the lesson must be found)."""
        return month_first(day) in self._load()

    def closed_for(self, teacher_ids: Iterable[Any], day: Any) -> Optional[ClosedMonth]:
        """The first closed month among ``teacher_ids`` in the month of ``day``."""
        month = self._load().get(month_first(day))
        if not month:
            return None
        for teacher in teacher_ids:
            tid = _uuid(teacher)
            if tid is not None and tid in month:
                return month[tid]
        return None

    def closed_in_range(self, teacher_ids: Iterable[Any], first: date, last: date) -> Optional[ClosedMonth]:
        """The first closed month of ``teacher_ids`` that the days ``first..last`` touch (a pause over a range of days)."""
        ids = {t for t in (_uuid(x) for x in teacher_ids) if t is not None}
        cursor = month_first(first)
        while cursor <= last:
            month = self._load().get(cursor)
            if month:
                for tid in sorted(ids, key=str):
                    if tid in month:
                        return month[tid]
            cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        return None

    def months_with_closed(self) -> Set[date]:
        return set(self._load())

    # ------------------------------------------------------------------------------------------------ the refusal
    def refusal(self, closed: ClosedMonth) -> OpsError:
        user = self.db.query(User).filter(User.id == closed.teacher_id).first()
        name = short_name(user)
        text = (f"Месяц {month_title(closed.month)}" + (f" у преподавателя {name}" if name else "") + f" закрыт: ведомость сохранена {closed.closed_on.strftime('%d.%m.%Y')}. "
                "Чтобы исправить урок, владелец или администратор с правом оплат сначала переоткрывает месяц")
        return conflict(PERIOD_CLOSED, text, month=closed.label, teacherId=str(closed.teacher_id), teacherName=name, closedOn=closed.closed_on.isoformat())

    def assert_open(self, teacher_ids: Iterable[Any], day: Any) -> None:
        closed = self.closed_for(teacher_ids, day)
        if closed is not None:
            raise self.refusal(closed)

    def assert_range_open(self, teacher_ids: Iterable[Any], first: date, last: date) -> None:
        closed = self.closed_in_range(teacher_ids, first, last)
        if closed is not None:
            raise self.refusal(closed)


# ---------------------------------------------------------------------------------------------- the teacher of a lesson / of a group
def lesson_teacher_ids(db, group, lesson_id: str, lesson_date: str, row: Optional[Any] = None, end_minutes: Optional[int] = None) -> List[UUID]:
    """The teacher whose month the lesson belongs to (a list of 0 or 1): the one frozen in its row, else the resolver's."""
    from .lesson_teachers import TeacherBook, effective_teacher

    if row is not None and row.teacher_id is not None:
        return [row.teacher_id]
    resolved = TeacherBook(db).resolve(group, lesson_id, lesson_date, end_minutes)
    teacher = effective_teacher(row, resolved)
    return [teacher] if teacher is not None else []


def group_teacher_ids(db, group, first: date, last: date) -> Set[UUID]:
    """Everybody who taught or leads the lessons of the group between two days: the teachers frozen in its journal rows + the teacher of the group + the assigned substitutes."""
    from src.models.auth import GroupAttendance, LessonTeacherAssignment

    ids: Set[UUID] = set()
    if group.teacher_id is not None:
        ids.add(group.teacher_id)
    ids |= {t for (t,) in db.query(GroupAttendance.teacher_id).filter(
        GroupAttendance.group_id == group.id, GroupAttendance.teacher_id.isnot(None),
        GroupAttendance.lesson_date >= first.isoformat(), GroupAttendance.lesson_date <= last.isoformat()).distinct().all()}
    ids |= {t for (t,) in db.query(LessonTeacherAssignment.teacher_id).filter(
        LessonTeacherAssignment.group_id == group.id, LessonTeacherAssignment.cancelled_at.is_(None), LessonTeacherAssignment.lesson_date >= first.isoformat(),
        LessonTeacherAssignment.lesson_date <= last.isoformat()).distinct().all()}
    return ids


# ---------------------------------------------------------------------------------------------- reopening (the journal of the closed period)
REASON_MIN, REASON_MAX = 3, 500


def _parse_month(text: str) -> date:
    try:
        first, _last = bc.month_bounds(text)
    except ValueError as exc:
        raise OpsError(422, "MONTH_INVALID", bc.MONTH_FORMAT_MESSAGE) from exc
    return first


def period_state(db, org_id: Any, teacher_id: Any, first: date) -> Dict[str, Any]:
    """``{"state": "none" | "open" | "closed" | "reopened", "closedOn", "sheetId", "reopened": {...} | None}`` of one teacher's month - what the screen of a lesson / of the month shows."""
    sheet = db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == teacher_id, PayrollSheet.period_month == first).first()
    if sheet is None:
        return {"state": "none", "closedOn": None, "sheetId": None, "reopened": None}
    last = (db.query(PayrollPeriodReopening).filter(PayrollPeriodReopening.org_id == org_id, PayrollPeriodReopening.teacher_id == teacher_id,
                                                    PayrollPeriodReopening.period_month == first).order_by(PayrollPeriodReopening.created_at.desc()).first())
    state, saved_on = state_of_sheet(sheet, last.created_at if last else None)
    reopened = None
    if last is not None:
        who = db.query(User).filter(User.id == last.reopened_by).first()
        reopened = {"at": last.created_at.isoformat(), "by": short_name(who), "reason": last.reason}
    return {"state": state, "closedOn": saved_on.isoformat() if state != "open" else None, "sheetId": str(sheet.id), "reopened": reopened}


def list_periods(db, org_id: Any, month: str) -> List[Dict[str, Any]]:
    """The saved sheets of a month with their period state, one line per teacher (no sums: this is the owner's / the administrator's view of WHO may still be corrected)."""
    first = _parse_month(month)
    sheets = db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.period_month == first).all()
    users = {u.id: u for u in db.query(User).filter(User.id.in_([s.teacher_id for s in sheets])).all()} if sheets else {}
    out = []
    for sheet in sheets:
        info = period_state(db, org_id, sheet.teacher_id, first)
        out.append({"teacherId": str(sheet.teacher_id), "teacherName": short_name(users.get(sheet.teacher_id)), "month": first.strftime("%Y-%m"), **info})
    return sorted(out, key=lambda x: x["teacherName"] or "")


def reopen_month(db, org_id: Any, teacher_id: Any, month: str, actor_id: Any, reason: str) -> Dict[str, Any]:
    """«Переоткрыть месяц» (owner / administrator with the payments right - checked by the route): the month of ``teacher_id`` is open for corrections until his sheet is saved again.
    Idempotent: a month that is open already (never closed, or reopened and not saved since) answers ``changed: false`` and writes nothing (I-7)."""
    first = _parse_month(month)
    text = (reason or "").strip()
    if not REASON_MIN <= len(text) <= REASON_MAX:
        raise OpsError(422, "REASON_REQUIRED", f"Причина: от {REASON_MIN} до {REASON_MAX} символов")
    tid = _uuid(teacher_id)
    sheet = (db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == tid, PayrollSheet.period_month == first)
             .with_for_update().populate_existing().first())      # the lock of the sheet: a save of it and a reopening are one after another
    if sheet is None:
        raise OpsError(404, "SHEET_NOT_FOUND", "Ведомости за этот месяц нет: переоткрывать нечего")
    last = (db.query(PayrollPeriodReopening.created_at).filter(PayrollPeriodReopening.org_id == org_id, PayrollPeriodReopening.teacher_id == tid,
                                                                PayrollPeriodReopening.period_month == first).order_by(PayrollPeriodReopening.created_at.desc()).first())
    state, _ = state_of_sheet(sheet, last[0] if last else None)
    if state != "closed":
        db.rollback()
        return {"ok": True, "changed": False, "teacherId": str(tid), "month": first.strftime("%Y-%m"), **period_state(db, org_id, tid, first)}
    db.add(PayrollPeriodReopening(org_id=org_id, teacher_id=tid, period_month=first, sheet_id=sheet.id, sheet_revision=sheet.revision, reopened_by=_uuid(actor_id),
                                  reason=text, created_at=bc.utc_now()))
    db.commit()
    return {"ok": True, "changed": True, "teacherId": str(tid), "month": first.strftime("%Y-%m"), **period_state(db, org_id, tid, first)}
