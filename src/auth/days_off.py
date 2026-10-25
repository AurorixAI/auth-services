"""PAY-F, part 2: «Выходной день центра» - a date or a range of up to 14 days with a reason.

The owner / admin gives the day off; every lesson of the range that can still be cancelled is cancelled by THE usual cancellation
(``AuthService.save_group_attendance``, kind ``holiday``): one road, one set of rules (rights, the exam-service check, the audit row).  Lessons that cannot be
cancelled (started, held, an exam of it is running or has attempts) are listed apart and stay as they are.  «Отменить выходной» gives back exactly the lessons that
THIS day off cancelled and that nobody has touched since; the others are listed.

``org_days_off`` is the journal; the lessons point to their day off by ``group_attendance.day_off_id``.  Nothing here touches money: a ``holiday`` cancellation is
never recalculated (part 3).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from src.models.auth import Group, GroupAttendance, OrgDayOff, User

from . import business_clock as bc
from . import payments_texts as tx
from .ops_rules import conflict, not_found, unprocessable
from .schemas import LessonAttendanceStatus, SaveAttendanceRequest

logger = logging.getLogger(__name__)

MAX_DAYS = 14
MAX_AHEAD_DAYS = 366
HOLIDAY = "holiday"


# ================================================================================================================
# Models
# ================================================================================================================


class DayOffRequest(BaseModel):
    dateFrom: date
    dateTo: date
    reason: str = Field(..., max_length=200)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("Укажите причину: не короче 3 символов")
        return value

    @model_validator(mode="after")
    def _range(self) -> "DayOffRequest":
        if self.dateTo < self.dateFrom:
            raise ValueError("Конец выходного раньше его начала")
        if (self.dateTo - self.dateFrom).days + 1 > MAX_DAYS:
            raise ValueError(f"Выходной — не больше {MAX_DAYS} дней подряд")
        return self


class DayOffLesson(BaseModel):
    groupId: str
    groupName: str
    lessonId: str
    date: date
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    reason: Optional[str] = None      # why it is not cancelled / not given back (blocked and kept lessons only)


class DayOffPlan(BaseModel):
    """Dry run of «создать выходной»: what would be cancelled and what would stay."""

    dateFrom: date
    dateTo: date
    reason: str
    cancellable: List[DayOffLesson] = Field(default_factory=list)
    blocked: List[DayOffLesson] = Field(default_factory=list)
    alreadyCancelled: int = 0
    groups: int = 0
    text: str = ""


class DayOffOut(BaseModel):
    id: str
    dateFrom: date
    dateTo: date
    reason: str
    createdAt: Optional[str] = None
    createdByName: Optional[str] = None
    voidedAt: Optional[str] = None
    active: bool = True


class DayOffApplied(BaseModel):
    dayOff: DayOffOut
    cancelled: List[DayOffLesson] = Field(default_factory=list)
    blocked: List[DayOffLesson] = Field(default_factory=list)
    alreadyCancelled: int = 0


class DayOffVoided(BaseModel):
    dayOff: DayOffOut
    restored: List[DayOffLesson] = Field(default_factory=list)
    kept: List[DayOffLesson] = Field(default_factory=list)
    dryRun: bool = False


class DayOffForDate(BaseModel):
    dayOff: Optional[DayOffOut] = None


# ================================================================================================================
# Texts (the words of the screen are written here, like the other payment texts)
# ================================================================================================================


def lesson_reason_text(reason: str) -> str:
    """The reason the lessons carry (the students read it): ``Выходной (Хайит)``."""
    return f"Выходной ({reason})"


def plan_text(cancellable: List[DayOffLesson]) -> str:
    if not cancellable:
        return "Уроков для отмены нет"
    names = list(dict.fromkeys(l.groupName for l in cancellable))
    n, m = len(cancellable), len(names)
    return (f"Будут отменены {n} {tx.lessons_word(n)} в {m} {tx.plural(m, 'группе', 'группах', 'группах')}: "
            + ", ".join(names))


# ================================================================================================================
# Service
# ================================================================================================================


class DaysOffService:
    def __init__(self, svc) -> None:
        self.svc = svc
        self.db = svc.db

    # ---------------------------------------------------------------------------------------------- reading

    def _out(self, row: OrgDayOff, names: Optional[Dict[Any, str]] = None) -> DayOffOut:
        names = names if names is not None else self._names([row.created_by])
        return DayOffOut(id=str(row.id), dateFrom=row.date_from, dateTo=row.date_to, reason=row.reason,
                         createdAt=row.created_at.isoformat() if row.created_at else None,
                         createdByName=names.get(row.created_by), voidedAt=row.voided_at.isoformat() if row.voided_at else None,
                         active=row.voided_at is None)

    def _names(self, ids) -> Dict[Any, str]:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        return {u.id: " ".join(x for x in (u.last_name, u.first_name) if x) for u in self.db.query(User).filter(User.id.in_(wanted))}

    def list(self, org_id: str, limit: int = 100) -> List[DayOffOut]:
        rows = (self.db.query(OrgDayOff).filter(OrgDayOff.org_id == org_id)
                .order_by(OrgDayOff.date_from.desc(), OrgDayOff.created_at.desc()).limit(limit).all())
        names = self._names(r.created_by for r in rows)
        return [self._out(r, names) for r in rows]

    def for_date(self, org_id: str, day: date) -> DayOffForDate:
        """The live day off that holds ``day`` (the hint when a lesson is created on that date: a warning, never a block)."""
        row = (self.db.query(OrgDayOff).filter(OrgDayOff.org_id == org_id, OrgDayOff.voided_at.is_(None),
                                               OrgDayOff.date_from <= day, OrgDayOff.date_to >= day)
               .order_by(OrgDayOff.created_at.desc()).first())
        return DayOffForDate(dayOff=self._out(row) if row else None)

    # ---------------------------------------------------------------------------------------------- the plan

    def _lessons(self, org_id: str, date_from: date, date_to: date):
        """``[(group, occurrence)]`` of the lessons of the active groups in the range, with the journal rows that already exist."""
        groups = self.db.query(Group).filter(Group.org_id == org_id, Group.status == "active").order_by(Group.name, Group.id).all()
        preload = self.svc._preload_schedule_data([g.id for g in groups], date_from=date_from, date_to=date_to)
        found = [(g, o) for g in groups for o in self.svc._resolved_occurrences_for_group(g, date_from=date_from, date_to=date_to, preloaded=preload)]
        rows = {}
        if groups:
            for row in self.db.query(GroupAttendance).filter(
                GroupAttendance.org_id == org_id, GroupAttendance.group_id.in_([g.id for g in groups]),
                GroupAttendance.lesson_date >= date_from.isoformat(), GroupAttendance.lesson_date <= date_to.isoformat(),
            ):
                rows[(row.group_id, row.lesson_id, row.lesson_date)] = row
        return found, rows

    @staticmethod
    def _item(group: Group, occurrence: Dict[str, Any], reason: Optional[str] = None) -> DayOffLesson:
        return DayOffLesson(groupId=str(group.id), groupName=group.name, lessonId=occurrence["id"], date=date.fromisoformat(occurrence["occurrenceDate"]),
                            startTime=occurrence.get("startTime"), endTime=occurrence.get("endTime"), reason=reason)

    def _blocker(self, org_id: str, group: Group, occurrence: Dict[str, Any], row: Optional[GroupAttendance], *, ask_exams: bool = True) -> Optional[str]:
        """Why the lesson cannot be cancelled by a day off (``None``: it can).  The same checks the usual cancellation makes, asked before anything is written.
        ``ask_exams=False``: leave the exam-service to the cancellation itself (it asks it anyway; the apply must not ask twice)."""
        day = date.fromisoformat(occurrence["occurrenceDate"])
        if row is not None:
            return "Урок уже начат" if row.status == LessonAttendanceStatus.IN_PROGRESS.value else "Урок уже проведён"
        if day < bc.month_window_start(bc.business_today()):
            return "Урок старше окна незакрытых уроков"
        if not ask_exams:
            return None
        try:
            self.svc._sync_exam_lesson_cancellation(phase="cancellation-check", org_id=org_id, group_id=str(group.id), lesson_id=occurrence["id"],
                                                    lesson_date=occurrence["occurrenceDate"], reason=None)
        except (ValueError, RuntimeError) as exc:
            return str(exc)
        return None

    def plan(self, org_id: str, req: DayOffRequest) -> DayOffPlan:
        self._check_range(req)
        found, rows = self._lessons(org_id, req.dateFrom, req.dateTo)
        cancellable: List[DayOffLesson] = []
        blocked: List[DayOffLesson] = []
        already = 0
        for group, occurrence in found:
            row = rows.get((group.id, occurrence["id"], occurrence["occurrenceDate"]))
            if row is not None and row.status == LessonAttendanceStatus.CANCELLED.value:
                already += 1
                continue
            why = self._blocker(org_id, group, occurrence, row)
            (blocked if why else cancellable).append(self._item(group, occurrence, why))
        return DayOffPlan(dateFrom=req.dateFrom, dateTo=req.dateTo, reason=req.reason, cancellable=cancellable, blocked=blocked,
                          alreadyCancelled=already, groups=len({l.groupId for l in cancellable}), text=plan_text(cancellable))

    @staticmethod
    def _check_range(req: DayOffRequest) -> None:
        if req.dateTo > bc.business_today() + timedelta(days=MAX_AHEAD_DAYS):
            raise unprocessable("DAY_OFF_TOO_FAR", "Выходной можно назначить не дальше чем на год вперёд")

    # ---------------------------------------------------------------------------------------------- create

    def create(self, org_id: str, actor_id: str, req: DayOffRequest) -> DayOffApplied:
        """The day off + the cancellation of every lesson that can be cancelled.  Each lesson is its own (usual) cancellation and commit: an exam-service that
        refuses one lesson, or a lesson that was started a second ago, lands in ``blocked`` and the others go on."""
        self._check_range(req)
        row = OrgDayOff(org_id=UUID(str(org_id)), date_from=req.dateFrom, date_to=req.dateTo, reason=req.reason, created_by=UUID(str(actor_id)))
        self.db.add(row)
        self.db.commit()
        day_off_id = row.id
        found, rows = self._lessons(org_id, req.dateFrom, req.dateTo)
        cancelled: List[DayOffLesson] = []
        blocked: List[DayOffLesson] = []
        already = 0
        for group, occurrence in found:
            existing = rows.get((group.id, occurrence["id"], occurrence["occurrenceDate"]))
            if existing is not None and existing.status == LessonAttendanceStatus.CANCELLED.value:
                already += 1
                continue
            why = self._blocker(org_id, group, occurrence, existing, ask_exams=False)
            if why is None:
                why = self._cancel(org_id, actor_id, group, occurrence, req.reason, day_off_id)
            (blocked if why else cancelled).append(self._item(group, occurrence, why))
        logger.info("day off created org=%s id=%s %s..%s cancelled=%d blocked=%d", org_id, day_off_id, req.dateFrom, req.dateTo, len(cancelled), len(blocked))
        row = self.db.query(OrgDayOff).filter(OrgDayOff.id == day_off_id).one()
        return DayOffApplied(dayOff=self._out(row), cancelled=cancelled, blocked=blocked, alreadyCancelled=already)

    def _cancel(self, org_id: str, actor_id: str, group: Group, occurrence: Dict[str, Any], reason: str, day_off_id: UUID) -> Optional[str]:
        """The usual cancellation, kind ``holiday``.  ``None`` = done, else the reason it was refused."""
        request = SaveAttendanceRequest(lessonDate=date.fromisoformat(occurrence["occurrenceDate"]), lessonId=occurrence["id"],
                                        status=LessonAttendanceStatus.CANCELLED, cancellationReason=lesson_reason_text(reason),
                                        cancellationKind=HOLIDAY)
        try:
            self.svc.save_group_attendance(org_id, str(group.id), request, marked_by=actor_id, is_org_admin=True, is_org_manager=True, day_off_id=day_off_id)
        except (ValueError, RuntimeError, PermissionError) as exc:
            self.db.rollback()
            return str(exc)
        except HTTPException as exc:
            self.db.rollback()
            detail = exc.detail
            return detail.get("message") if isinstance(detail, dict) else str(detail)
        return None

    # ---------------------------------------------------------------------------------------------- void

    def void(self, org_id: str, actor_id: str, day_off_id: UUID, *, dry_run: bool = False) -> DayOffVoided:
        """«Отменить выходной»: the lessons THIS day off cancelled come back when they are still in the future and unchanged since (the row is as the cancellation made
        it: version 1, kind ``holiday``); every other one is listed with the reason and stays cancelled."""
        row = self.db.query(OrgDayOff).filter(OrgDayOff.id == day_off_id, OrgDayOff.org_id == org_id).with_for_update().first()
        if row is None:
            raise not_found("DAY_OFF_NOT_FOUND", "Выходной не найден")
        if row.voided_at is not None:
            raise conflict("DAY_OFF_VOIDED", "Этот выходной уже отменён")
        today = bc.business_today()
        restored: List[DayOffLesson] = []
        kept: List[DayOffLesson] = []
        groups = {g.id: g for g in self.db.query(Group).filter(Group.org_id == org_id)}
        for att in (self.db.query(GroupAttendance).filter(GroupAttendance.org_id == org_id, GroupAttendance.day_off_id == row.id)
                    .order_by(GroupAttendance.lesson_date, GroupAttendance.group_id).with_for_update().all()):
            group = groups.get(att.group_id)
            item = DayOffLesson(groupId=str(att.group_id), groupName=group.name if group else "", lessonId=att.lesson_id or "",
                                date=date.fromisoformat(att.lesson_date))
            why = self._not_restorable(att, today, group)
            if why:
                item.reason = why
                kept.append(item)
            else:
                restored.append(item)
                if not dry_run:
                    self.db.delete(att)
        if not dry_run:
            row.voided_at = datetime.utcnow()
            row.voided_by = UUID(str(actor_id))
            self.db.commit()
            logger.info("day off voided org=%s id=%s restored=%d kept=%d", org_id, row.id, len(restored), len(kept))
            row = self.db.query(OrgDayOff).filter(OrgDayOff.id == day_off_id).one()
        return DayOffVoided(dayOff=self._out(row), restored=restored, kept=kept, dryRun=dry_run)

    @staticmethod
    def _not_restorable(att: GroupAttendance, today: date, group: Optional[Group]) -> Optional[str]:
        if att.status != LessonAttendanceStatus.CANCELLED.value or att.cancellation_kind != HOLIDAY:
            return "Урок уже изменён после выходного"
        if int(att.version or 1) != 1:
            return "Урок изменён после выходного"
        if date.fromisoformat(att.lesson_date) < today:
            return "Урок уже в прошлом"
        if group is None or group.status != "active":
            return "Группа закрыта"
        return None
