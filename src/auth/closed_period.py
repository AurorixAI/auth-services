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
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session

from src.models.auth import Organization, PayrollPeriodReopening, PayrollSheet, User

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
    """The closed months of ONE centre, loaded once per REQUEST (``of``): a handful of rows - four columns, never the lines of a sheet - shared by every service of the request."""

    def __init__(self, db, org_id: Any) -> None:
        self.db = db
        self.org_id = org_id
        self._closed: Optional[Dict[date, Dict[UUID, ClosedMonth]]] = None

    @classmethod
    def of(cls, db, org_id: Any) -> "ClosedPeriods":
        """The snapshot of the session (Money-1 R3 #6): the attendance service, the day-off service and the substitutions used to read the sheets of the centre each on its own.  The cache lives
        in ``db.info`` and is dropped at the end of every transaction (a commit or a rollback), so a later save of a sheet or a reopening is seen."""
        cache = db.info.setdefault("closed_periods", {})
        key = str(org_id)
        if key not in cache:
            cache[key] = cls(db, org_id)
        return cache[key]

    # ------------------------------------------------------------------------------------------------ the snapshot
    def _load(self) -> Dict[date, Dict[UUID, ClosedMonth]]:
        if self._closed is not None:
            return self._closed
        sheets = self.db.query(PayrollSheet.id, PayrollSheet.teacher_id, PayrollSheet.period_month, PayrollSheet.updated_at).filter(PayrollSheet.org_id == self.org_id).all()
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
        for sheet_id, teacher_id, period_month, updated_at in sheets:
            state, saved_on = state_of_sheet(SimpleNamespace(updated_at=updated_at, period_month=period_month), last_reopen.get((teacher_id, period_month)))
            if state == "closed":
                out.setdefault(period_month, {})[teacher_id] = ClosedMonth(teacher_id, period_month, saved_on, sheet_id)
        self._closed = out
        return out

    def invalidate(self) -> None:
        self._closed = None

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
    def refusal(self, closed: ClosedMonth, what: str = "урок") -> OpsError:
        user = self.db.query(User).filter(User.id == closed.teacher_id).first()
        name = short_name(user)
        text = (f"Месяц {month_title(closed.month)}" + (f" у преподавателя {name}" if name else "") + f" закрыт: ведомость сохранена {closed.closed_on.strftime('%d.%m.%Y')}. "
                f"Чтобы изменить {what}, владелец или администратор с правом оплат сначала переоткрывает месяц")
        return conflict(PERIOD_CLOSED, text, month=closed.label, teacherId=str(closed.teacher_id), teacherName=name, closedOn=closed.closed_on.isoformat())

    def _hold_open(self, teacher_ids: Iterable[Any], months: Iterable[date]) -> None:
        """Money-1 R3 m9: a check that said «open» keeps the SAVED sheets of these teacher-months from being saved until this transaction ends (``FOR SHARE``): a correction that passed the check and
        a save of the sheet after the month ended are one after another, never at the same moment.  Readers do not wait for each other; only the save of the sheet waits."""
        ids = [t for t in (_uuid(x) for x in teacher_ids) if t is not None]
        wanted = sorted({month_first(m) for m in months})
        if not ids or not wanted:
            return
        self.db.query(PayrollSheet.id).filter(PayrollSheet.org_id == self.org_id, PayrollSheet.teacher_id.in_(ids), PayrollSheet.period_month.in_(wanted)).with_for_update(read=True).all()

    def assert_open(self, teacher_ids: Iterable[Any], day: Any, what: str = "урок") -> None:
        teacher_ids = list(teacher_ids)
        closed = self.closed_for(teacher_ids, day)
        if closed is not None:
            raise self.refusal(closed, what)
        self._hold_open(teacher_ids, [month_first(day)])

    def assert_range_open(self, teacher_ids: Iterable[Any], first: date, last: date, what: str = "урок") -> None:
        teacher_ids = list(teacher_ids)
        closed = self.closed_in_range(teacher_ids, first, last)
        if closed is not None:
            raise self.refusal(closed, what)
        months, cursor = [], month_first(first)
        while cursor <= last:
            months.append(cursor)
            cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        self._hold_open(teacher_ids, months)


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

    from .lesson_teachers import TeacherBook

    ids: Set[UUID] = TeacherBook(db).teachers_between(group, bc.lesson_instant_utc(first, 0), bc.lesson_instant_utc(last, 24 * 60 - 1))       # R3 m3: the teachers OF THOSE DAYS, not the current one
    ids |= {t for (t,) in db.query(GroupAttendance.teacher_id).filter(
        GroupAttendance.group_id == group.id, GroupAttendance.teacher_id.isnot(None),
        GroupAttendance.lesson_date >= first.isoformat(), GroupAttendance.lesson_date <= last.isoformat()).distinct().all()}
    ids |= {t for (t,) in db.query(LessonTeacherAssignment.teacher_id).filter(
        LessonTeacherAssignment.group_id == group.id, LessonTeacherAssignment.cancelled_at.is_(None), LessonTeacherAssignment.lesson_date >= first.isoformat(),
        LessonTeacherAssignment.lesson_date <= last.isoformat()).distinct().all()}
    return ids


# ---------------------------------------------------------------------------------------------- reopening (the journal of the closed period)
REASON_MIN, REASON_MAX = 3, 500


def accounting_start(db, org_id: Any) -> date:
    """The day the centre starts to account (the setting, else the day the centre was created): the lower bound of «open for ever» months (R3 #2) and of the type of lessons (R3 #4)."""
    from .ledger_service import accounting_start_of

    row = db.query(Organization.settings, Organization.created_at).filter(Organization.id == org_id).first()
    block = (row[0] or {}).get("payments") if row and isinstance(row[0], dict) else None
    return accounting_start_of(dict(block) if isinstance(block, dict) else {}, row[1] if row else None)


def open_lessons_since(db, org_id: Any, today: date) -> date:
    """The oldest day a lesson that nobody resolved (never started, still running, cancelled by mistake) is offered for a decision (R3 #2): the accounting start of the centre - and never
    later than the start of last month (the window this list had before, so a young centre loses nothing)."""
    return min(accounting_start(db, org_id), bc.month_window_start(today))


def group_open_actions_by_month(db, org_id: Any, actions: List[Any]) -> Dict[str, Any]:
    """The list «Требует разбора» grouped by month of the lesson (oldest first) with a counter in each month and in total (R3 #2)."""
    from .ops_schemas import OpenActionsMonth

    months: Dict[date, List[Any]] = {}
    for action in sorted(actions, key=lambda a: (a.lessonDate, a.startTime)):
        months.setdefault(month_first(action.lessonDate), []).append(action)
    return {
        "total": len(actions),
        "since": open_lessons_since(db, org_id, bc.business_today()).isoformat(),
        "months": [OpenActionsMonth(month=first.strftime("%Y-%m"), title=month_title(first).capitalize(), count=len(items), missed=sum(i.actionType == "missed" for i in items),
                                    inProgress=sum(i.actionType == "in_progress" for i in items), items=items) for first, items in sorted(months.items())],
    }


def _parse_month(text: str) -> date:
    try:
        first, _last = bc.month_bounds(text)
    except ValueError as exc:
        raise OpsError(422, "MONTH_INVALID", "Месяц указывается в формате ГГГГ-ММ, например 2026-09") from exc
    return first


def period_state(db, org_id: Any, teacher_id: Any, first: date) -> Dict[str, Any]:
    """``{"state": "none" | "open" | "closed" | "reopened", "closedOn", "sheetId", "reopened": {...} | None}`` of one teacher's month - what the screen of a lesson / of the month shows."""
    sheet = db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == teacher_id, PayrollSheet.period_month == first).first()
    if sheet is None:
        return {"state": "none", "closedOn": None, "sheetId": None, "sheetRevision": None, "reopened": None}
    last = (db.query(PayrollPeriodReopening).filter(PayrollPeriodReopening.org_id == org_id, PayrollPeriodReopening.teacher_id == teacher_id,
                                                    PayrollPeriodReopening.period_month == first).order_by(PayrollPeriodReopening.created_at.desc()).first())
    state, saved_on = state_of_sheet(sheet, last.created_at if last else None)
    reopened = None
    if last is not None:
        who = db.query(User).filter(User.id == last.reopened_by).first()
        reopened = {"at": last.created_at.isoformat(), "by": short_name(who), "reason": last.reason}
    return {"state": state, "closedOn": saved_on.isoformat() if state != "open" else None, "sheetId": str(sheet.id), "sheetRevision": int(sheet.revision), "reopened": reopened}


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


def reopen_month(db, org_id: Any, teacher_id: Any, month: str, actor_id: Any, reason: str, sheet_revision: Optional[int] = None) -> Dict[str, Any]:
    """«Переоткрыть месяц» (owner / administrator with the payments right - checked by the route): the month of ``teacher_id`` is open for corrections until his sheet is saved again.
    Idempotent: a month that is open already (never closed, or reopened and not saved since) answers ``changed: false`` and writes nothing (I-7).  ``sheet_revision`` (R3 #11): the revision of the sheet
    the person saw; a late duplicate of the request, sent before the owner saved the sheet again, finds another revision and is refused (409 ``SHEET_CHANGED``) instead of reopening the month a second time."""
    first = _parse_month(month)
    text = (reason or "").strip()
    if not REASON_MIN <= len(text) <= REASON_MAX:
        raise OpsError(422, "REASON_REQUIRED", f"Причина: от {REASON_MIN} до {REASON_MAX} символов")
    tid = _uuid(teacher_id)
    sheet = (db.query(PayrollSheet).filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == tid, PayrollSheet.period_month == first)
             .with_for_update().populate_existing().first())      # the lock of the sheet: a save of it and a reopening are one after another
    if sheet is None:
        raise OpsError(404, "SHEET_NOT_FOUND", "Ведомости за этот месяц нет: переоткрывать нечего")
    if sheet_revision is None:
        db.rollback()
        raise OpsError(422, "REVISION_REQUIRED", "Обновите страницу и повторите: нужна текущая версия ведомости")
    if int(sheet_revision) != int(sheet.revision):
        current = int(sheet.revision)
        db.rollback()
        raise OpsError(409, "SHEET_CHANGED", "Ведомость уже изменили. Обновите страницу и проверьте, нужно ли переоткрывать месяц", currentRevision=current)
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


@event.listens_for(Session, "after_transaction_end")
def _forget_closed_periods(session, _transaction) -> None:
    """The snapshot of the closed months lives for one transaction: a commit or a rollback (also of a savepoint) drops it."""
    session.info.pop("closed_periods", None)
