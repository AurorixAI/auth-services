"""PAY-F, part 2: «Выходной день центра» - a date or a range of up to 14 days with a reason.

The owner / admin gives the day off; every lesson of the range that can still be cancelled is cancelled by THE usual cancellation
(``AuthService.save_group_attendance``, kind ``holiday``): one road, one set of rules (rights, the exam-service check, the audit row).  Lessons that cannot be
cancelled (started, held, an exam of it is running or has attempts) are listed apart and stay as they are.  A manual cancellation of the centre's fault that is
already there on a date of the day off is turned into a holiday (audited): the day is a holiday whoever cancelled the lesson.  «Отменить выходной» gives back exactly
the lessons that THIS day off cancelled and that nobody has touched since, unless another live day off covers the date; the others are listed.  What was given back
stays on record (``org_days_off.restored`` and one ``group_audit`` row per group): the cancellation rows themselves are deleted.

``org_days_off`` is the journal; the lessons point to their day off by ``group_attendance.day_off_id``.  The apply is idempotent: the same range and reason among the live
days off continues that day off, and ``apply`` finishes a day off whose exam-service was unavailable.  Nothing here touches money: a ``holiday`` cancellation is never
recalculated (part 3).

A day off cancels what EXISTS when it is given.  A WEEKLY lesson that appears later (a new slot, a revision that moves a series onto the date) goes through the same step
(``apply_to_lesson``); a one-off lesson or a move onto the date is deliberate (a make-up) and only gets the warning ``DAY_OFF`` (``warnings_for_date``).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from sqlalchemy.exc import IntegrityError

from src.models.auth import Group, GroupAttendance, GroupAttendanceAudit, GroupAudit, LessonTeacherAssignment, Organization, OrgDayOff, User

from . import business_clock as bc
from . import payments_texts as tx
from .ops_rules import conflict, hhmm_to_minutes, not_found, short_name, unprocessable
from .schemas import LessonAttendanceStatus

logger = logging.getLogger(__name__)

MAX_DAYS = 14
MAX_AHEAD_DAYS = 366
HOLIDAY = "holiday"
CENTRE = "centre"
DAY_OFF_KIND = "day_off"                 # ``trigger`` of the record of the journal «Замены» a day off made when it turned a hand cancellation «по вине центра» into a holiday (#157 A3) ...
DAY_OFF_VOID_KIND = "day_off_void"       # ... and when its cancellation gave the kind back
EXAM_DOWN = "сервис экзаменов недоступен, повторите"
EXAMS_NOTE = "Экзамены, снятые с уроков, при отмене выходного не вернутся — назначьте заново"
EXAMS_NOTE_VOID = "Если с этих уроков снимались экзамены — назначьте их заново"          # «Отменить выходной»: the server does not know whether the day off took exams off, so the sentence is conditional


# ================================================================================================================
# Models
# ================================================================================================================


class DayOffDates(BaseModel):
    dateFrom: date
    dateTo: date

    @model_validator(mode="after")
    def _range(self):
        if self.dateTo < self.dateFrom:
            raise ValueError("Конец выходного раньше его начала")
        if (self.dateTo - self.dateFrom).days + 1 > MAX_DAYS:
            raise ValueError(f"Выходной — не больше {MAX_DAYS} дней подряд")
        return self


class DayOffRequest(DayOffDates):
    reason: str = Field(..., max_length=200)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("Укажите причину: не короче 3 символов")
        return value


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
    cancellable: List[DayOffLesson] = Field(default_factory=list)
    blocked: List[DayOffLesson] = Field(default_factory=list)
    convertible: List[DayOffLesson] = Field(default_factory=list)       # cancelled by hand «по вине центра»: would be turned into a holiday
    alreadyCancelled: int = 0
    groups: int = 0
    examServiceDown: bool = False
    text: str = ""
    examsNote: str = EXAMS_NOTE


class DayOffOut(BaseModel):
    id: str
    dateFrom: date
    dateTo: date
    reason: str
    createdAt: Optional[str] = None
    createdByName: Optional[str] = None
    voidedAt: Optional[str] = None
    active: bool = True
    incomplete: bool = False          # W1 S10: the exam-service stopped the apply - some lessons wait for ``apply`` («Продолжить»); kept on the server, not in the tab


class DayOffApplied(BaseModel):
    dayOff: DayOffOut
    cancelled: List[DayOffLesson] = Field(default_factory=list)
    blocked: List[DayOffLesson] = Field(default_factory=list)
    converted: List[DayOffLesson] = Field(default_factory=list)       # «отмена переведена в праздник»
    alreadyCancelled: int = 0
    complete: bool = True                                             # False: the exam-service stopped the loop - ``apply`` finishes it


_Date = date


class DayOffWarning(BaseModel):
    code: str
    message: str                               # the line for the journal / the API; the screen builds its own line from the fields below
    groupId: Optional[str] = None
    groupName: Optional[str] = None
    lessonId: Optional[str] = None
    date: Optional[_Date] = None
    startTime: Optional[str] = None            # of the lesson (the same line as in the lists of the lessons)
    endTime: Optional[str] = None
    substituteName: Optional[str] = None


class DayOffVoided(BaseModel):
    dayOff: DayOffOut
    restored: List[DayOffLesson] = Field(default_factory=list)
    kept: List[DayOffLesson] = Field(default_factory=list)
    dryRun: bool = False
    examsNote: str = EXAMS_NOTE_VOID
    warnings: List[DayOffWarning] = Field(default_factory=list)      # W1: ``SUBSTITUTE_NOT_RESTORED`` - a lesson that came back without the substitute the day off took off it


class DayOffForDate(BaseModel):
    dayOff: Optional[DayOffOut] = None


# ================================================================================================================
# Texts (the words of the screen are written here, like the other payment texts)
# ================================================================================================================


def lesson_reason_text(reason: str) -> str:
    """The reason the lessons carry (the students read it): ``Выходной (Хайит)``."""
    return f"Выходной ({reason})"


def _dm(day: date) -> str:
    return day.strftime("%d.%m")


def cancelled_text(day: date, reason: str) -> str:
    return f"{_dm(day)} — выходной день центра ({reason}): урок в этот день отменён автоматически"


def not_cancelled_text(day: date, reason: str, why: Optional[str]) -> str:
    return f"{_dm(day)} — выходной день центра ({reason}): урок в этот день не удалось отменить автоматически ({why}) — отмените его вручную"


def converted_text(day: date, reason: str) -> str:
    return f"{_dm(day)} — выходной день центра ({reason}): отмена урока «по вине центра» переведена в «выходной», вычета у учеников не будет"


def on_day_off_text(day: date, reason: str) -> str:
    return f"{_dm(day)} — выходной день центра ({reason}): урок в этот день будет проведён, это не отменяется автоматически"


def _one(count: int) -> bool:
    return count % 10 == 1 and count % 100 != 11


def plan_text(cancellable: List[DayOffLesson]) -> str:
    """«Будет отменён 1 урок в 1 группе: …» / «Будут отменены 5 уроков в 2 группах: …» (one place for the plurals)."""
    if not cancellable:
        return "Уроков для отмены нет"
    names = list(dict.fromkeys(l.groupName for l in cancellable))
    n, m = len(cancellable), len(names)
    verb = "Будет отменён" if _one(n) else "Будут отменены"
    return f"{verb} {n} {tx.lessons_word(n)} в {m} {tx.plural(m, 'группе', 'группах', 'группах')}: " + ", ".join(names)


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
                         active=row.voided_at is None, incomplete=bool(row.incomplete) and row.voided_at is None)

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

    def live_for_date(self, org_id: Any, day: date) -> Optional[OrgDayOff]:
        """The live day off that holds ``day`` (the newest one when several do)."""
        return (self.db.query(OrgDayOff).filter(OrgDayOff.org_id == org_id, OrgDayOff.voided_at.is_(None), OrgDayOff.date_from <= day, OrgDayOff.date_to >= day)
                .order_by(OrgDayOff.created_at.desc(), OrgDayOff.id).first())

    def for_date(self, org_id: str, day: date) -> DayOffForDate:
        """The live day off that holds ``day`` (the hint when a lesson is created on that date: a warning, never a block)."""
        row = self.live_for_date(org_id, day)
        return DayOffForDate(dayOff=self._out(row) if row else None)

    # ---------------------------------------------------------------------------------------------- the lessons of a range

    def _lessons(self, org_id: str, date_from: date, date_to: date, group_id: Optional[Any] = None):
        """``[(group, occurrence)]`` of the lessons of the active groups (or of the one ``group_id``) in the range, with the journal rows that already exist."""
        query = self.db.query(Group).filter(Group.org_id == org_id, Group.status == "active")
        if group_id is not None:
            query = query.filter(Group.id == group_id)
        groups = query.order_by(Group.name, Group.id).all()
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

    def _occurrence_of(self, group: Optional[Group], att: GroupAttendance, cache: Dict[Any, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The occurrence of the schedule that the journal row stands for (``None`` when the lesson is gone); ``cache`` = ``{(group_id, date): {lesson_id: occurrence}}``, filled by
        ``_preload_lines`` in one pass, a single day is read on demand for a row it does not know."""
        if group is None:
            return None
        key = (group.id, att.lesson_date)
        if key not in cache:
            day = date.fromisoformat(att.lesson_date)
            cache[key] = {o["id"]: o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day)}
        return cache[key].get(att.lesson_id)

    def _preload_lines(self, atts: List[GroupAttendance], groups: Dict[Any, Group]) -> Dict[Any, Dict[str, Any]]:
        """The schedule of ALL the groups of ``atts`` over their days in one preload (as the preview does), not one resolution per row."""
        rows = [a for a in atts if a.group_id in groups]
        cache: Dict[Any, Dict[str, Any]] = {}
        if not rows:
            return cache
        days = sorted({a.lesson_date for a in rows})
        gids = sorted({a.group_id for a in rows}, key=str)
        d_from, d_to = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
        preload = self.svc._preload_schedule_data(gids, date_from=d_from, date_to=d_to)
        wanted = {(a.group_id, a.lesson_date) for a in rows}
        for gid in gids:
            for occ in self.svc._resolved_occurrences_for_group(groups[gid], date_from=d_from, date_to=d_to, preloaded=preload):
                if (gid, occ["occurrenceDate"]) in wanted:
                    cache.setdefault((gid, occ["occurrenceDate"]), {})[occ["id"]] = occ
        for key in wanted:
            cache.setdefault(key, {})
        return cache

    def _att_item(self, att: GroupAttendance, group: Optional[Group], cache: Dict[Any, Dict[str, Any]]) -> DayOffLesson:
        """The line of a lesson that is known by its journal row (the plan of «Отменить выходной», its result and its warnings): the SAME line as the preview builds
        (``_item``), with the time of the lesson in the schedule of the group (a lesson that is no longer there has no time)."""
        occ = self._occurrence_of(group, att, cache)
        shown = SimpleNamespace(id=att.group_id, name=group.name if group is not None else "")
        return self._item(shown, occ or {"id": att.lesson_id or "", "occurrenceDate": att.lesson_date})

    OLD_CONVERT = "Отмена «по вине центра» старше окна незакрытых уроков: её вид не меняется (закрытый период не пересчитывается)"

    @staticmethod
    def _in_window(lesson_date: str) -> bool:
        """W1 R3 (2): the same window as the marks of a past lesson - a cancellation older than it is never turned into a holiday (a closed or paid month does not change)."""
        return date.fromisoformat(lesson_date) >= bc.month_window_start(bc.business_today())

    @staticmethod
    def _convertible(row: Optional[GroupAttendance]) -> bool:
        """A lesson cancelled by hand «по вине центра» (not by a day off): the day off turns it into a holiday.  #154: not one that has a make-up - it stays «по вине центра» (the make-up lifts
        the deduction by the facts; a holiday would have no deduction to lift and the make-up would make up for nothing)."""
        return row is not None and row.status == LessonAttendanceStatus.CANCELLED.value and row.cancellation_kind == CENTRE and row.day_off_id is None and not row.made_up_by

    @staticmethod
    def _makeup_kept(row: Optional[GroupAttendance]) -> bool:
        """A hand cancellation «по вине центра» that has a make-up: a day off on its date does not touch it."""
        return (row is not None and row.status == LessonAttendanceStatus.CANCELLED.value and row.cancellation_kind == CENTRE and row.day_off_id is None and bool(row.made_up_by))

    MAKEUP_KEPT = "По уроку назначена отработка: отмена остаётся «по вине центра»"

    def _blocker(self, org_id: str, group: Group, occurrence: Dict[str, Any], row: Optional[GroupAttendance], *, ask_exams: bool = True) -> Optional[str]:
        """Why the lesson cannot be cancelled by a day off (``None``: it can; ``EXAM_DOWN``: exam-service did not answer).  The same checks the usual cancellation makes,
        asked before anything is written.  ``ask_exams=False``: leave the exam-service to the cancellation itself (it asks it anyway; the apply must not ask twice)."""
        day = date.fromisoformat(occurrence["occurrenceDate"])
        if row is not None:
            return "Урок уже начат" if row.status == LessonAttendanceStatus.IN_PROGRESS.value else "Урок уже проведён"
        if day < bc.month_window_start(bc.business_today()):
            return "Урок старше окна незакрытых уроков"
        try:
            started = bc.lesson_instant_utc(day, hhmm_to_minutes(occurrence["startTime"])) <= bc.utc_now()
        except (KeyError, ValueError, AttributeError):
            started = False
        if started:                       # W1 S10 (Д23 / Д11): a lesson that is past or going on may have been held - the day off only warns, the lesson is closed in the journal
            if day == bc.business_today():
                return "Урок уже начался — отмените вручную, если не состоялся"
            return "Урок уже прошёл или идёт: закройте его в журнале (отметьте посещаемость или «Урока не было»)"
        if occurrence.get("isOneOff"):       # W1 S10+ (Д5): a one-off lesson / a copy of a move is deliberate (a make-up) - in EVERY order of the actions it stays and only warns
            return "Разовый урок или перенос на эту дату назначен осознанно — отмените его вручную, если не нужен"
        if not ask_exams:
            return None
        try:
            self.svc._sync_exam_lesson_cancellation(phase="cancellation-check", org_id=org_id, group_id=str(group.id), lesson_id=occurrence["id"],
                                                    lesson_date=occurrence["occurrenceDate"], reason=None)
        except ValueError as exc:
            return str(exc)
        except RuntimeError:
            return EXAM_DOWN
        return None

    # ---------------------------------------------------------------------------------------------- the plan

    def plan(self, org_id: str, req: DayOffDates) -> DayOffPlan:
        """Dry run.  The first time exam-service does not answer the loop stops asking: the rest is listed as «сервис экзаменов недоступен» at once (not N waits)."""
        self._check_range(req)
        found, rows = self._lessons(org_id, req.dateFrom, req.dateTo)
        cancellable: List[DayOffLesson] = []
        blocked: List[DayOffLesson] = []
        convertible: List[DayOffLesson] = []
        already = 0
        down = False
        for group, occurrence in found:
            row = rows.get((group.id, occurrence["id"], occurrence["occurrenceDate"]))
            if row is not None and row.status == LessonAttendanceStatus.CANCELLED.value:
                if self._makeup_kept(row):                           # #154: said in a line of the preview: the cancellation stays «по вине центра», the make-up lifts the deduction
                    blocked.append(self._item(group, occurrence, self.MAKEUP_KEPT))
                elif self._convertible(row) and self._in_window(occurrence["occurrenceDate"]):
                    convertible.append(self._item(group, occurrence))
                elif self._convertible(row):
                    blocked.append(self._item(group, occurrence, self.OLD_CONVERT))
                else:
                    already += 1
                continue
            why = EXAM_DOWN if (down and row is None) else self._blocker(org_id, group, occurrence, row)
            down = down or why == EXAM_DOWN
            (blocked if why else cancellable).append(self._item(group, occurrence, why))
        return DayOffPlan(dateFrom=req.dateFrom, dateTo=req.dateTo, cancellable=cancellable, blocked=blocked, convertible=convertible,
                          alreadyCancelled=already, groups=len({l.groupId for l in cancellable}), examServiceDown=down, text=plan_text(cancellable))

    @staticmethod
    def _check_range(req: DayOffDates) -> None:
        if req.dateTo > bc.business_today() + timedelta(days=MAX_AHEAD_DAYS):
            raise unprocessable("DAY_OFF_TOO_FAR", "Выходной можно назначить не дальше чем на год вперёд")

    # ---------------------------------------------------------------------------------------------- create / apply

    def create(self, org_id: str, actor_id: str, req: DayOffRequest) -> DayOffApplied:
        """The day off + the cancellation of every lesson that can be cancelled.  The same range and reason among the LIVE days off is that day off (it is continued, not
        duplicated).  Each lesson is its own (usual) cancellation and commit: a lesson that was started a second ago, or one an exam is attached to, lands in ``blocked``
        and the others go on; when exam-service does not answer the loop stops and the rest waits in ``blocked`` for ``apply``."""
        self._check_range(req)
        row = self._live_row(org_id, actor_id, req)
        return self._apply(org_id, actor_id, row.id)

    def _live_row(self, org_id: str, actor_id: str, req: DayOffRequest) -> OrgDayOff:
        """W1 S10 (Д26): the live day off of this range - found or given, under the lock of the centre (two windows / a retry after a dropped connection do not give it twice; the
        partial unique index is the backstop).  The same range with ANOTHER reason is refused: one range, one live day off."""
        def live() -> Optional[OrgDayOff]:
            return (self.db.query(OrgDayOff).filter(OrgDayOff.org_id == org_id, OrgDayOff.voided_at.is_(None), OrgDayOff.date_from == req.dateFrom,
                                                    OrgDayOff.date_to == req.dateTo).order_by(OrgDayOff.created_at, OrgDayOff.id).first())

        def same(found: OrgDayOff) -> OrgDayOff:
            if found.reason != req.reason:
                raise conflict("DAY_OFF_EXISTS", f"На эти даты уже назначен выходной «{found.reason}»: отмените его или назначьте на другие даты")
            return found

        self.db.query(Organization.id).filter(Organization.id == org_id).with_for_update(key_share=True).first()       # FOR NO KEY UPDATE: the foreign key of the new row does not wait for it
        found = live()
        if found is not None:
            self.db.rollback()
            return same(found)
        row = OrgDayOff(org_id=UUID(str(org_id)), date_from=req.dateFrom, date_to=req.dateTo, reason=req.reason, created_by=UUID(str(actor_id)))
        self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            found = live()
            if found is None:
                raise
            return same(found)
        return row

    def apply(self, org_id: str, actor_id: str, day_off_id: UUID) -> DayOffApplied:
        """Finish a day off (idempotent): what is cancelled stays, the rest is tried again."""
        row = self.db.query(OrgDayOff).filter(OrgDayOff.id == day_off_id, OrgDayOff.org_id == org_id).first()
        if row is None:
            raise not_found("DAY_OFF_NOT_FOUND", "Выходной не найден")
        if row.voided_at is not None:
            raise conflict("DAY_OFF_VOIDED", "Этот выходной отменён: назначьте его заново")
        return self._apply(org_id, actor_id, row.id)

    def _apply(self, org_id: str, actor_id: str, day_off_id: UUID) -> DayOffApplied:
        row = self.db.query(OrgDayOff).filter(OrgDayOff.id == day_off_id).one()
        date_from, date_to, reason = row.date_from, row.date_to, row.reason
        found, rows = self._lessons(org_id, date_from, date_to)
        cancelled, blocked, converted, already, down = self._run(org_id, actor_id, day_off_id, reason, found, rows)
        logger.info("day off applied org=%s id=%s %s..%s cancelled=%d blocked=%d converted=%d", org_id, day_off_id, date_from, date_to, len(cancelled), len(blocked), len(converted))
        row = self.db.query(OrgDayOff).filter(OrgDayOff.id == day_off_id).one()
        if bool(row.incomplete) != down:                                  # W1 S10 (Д6): «не завершён» lives on the server, a reloaded panel still shows «Продолжить»
            row.incomplete = down
            self.db.commit()
        return DayOffApplied(dayOff=self._out(row), cancelled=cancelled, blocked=blocked, converted=converted, alreadyCancelled=already, complete=not down)

    def _run(self, org_id: str, actor_id: str, day_off_id: UUID, reason: str, found, rows):
        """THE step of a day off over ``found`` lessons: a hand-cancelled «по вине центра» one is turned into a holiday, a cancelled one is skipped, the others go through the usual
        cancellation.  The day off itself and ``apply_to_lesson`` (a lesson that appears after the day off) both walk through it: one road, no copy.
        Returns ``(cancelled, blocked, converted, already, exam_service_down)``."""
        cancelled: List[DayOffLesson] = []
        blocked: List[DayOffLesson] = []
        converted: List[DayOffLesson] = []
        already = 0
        down = False
        for group, occurrence in found:
            existing = rows.get((group.id, occurrence["id"], occurrence["occurrenceDate"]))
            if self._makeup_kept(existing):                         # #154: left as it is (the preview said so)
                already += 1
                continue
            if self._convertible(existing):
                if not self._in_window(occurrence["occurrenceDate"]):
                    blocked.append(self._item(group, occurrence, self.OLD_CONVERT))
                elif self._convert(existing, group, day_off_id, reason, actor_id):
                    converted.append(self._item(group, occurrence))
                continue
            if existing is not None and existing.status == LessonAttendanceStatus.CANCELLED.value:
                already += 1
                continue
            why = self._blocker(org_id, group, occurrence, existing, ask_exams=False)
            if why is None and down:
                why = EXAM_DOWN
            if why is None:
                why = self._cancel(org_id, actor_id, group, occurrence, reason, day_off_id)
                down = down or why == EXAM_DOWN
            (blocked if why else cancelled).append(self._item(group, occurrence, why))
        return cancelled, blocked, converted, already, down

    def apply_to_lesson(self, org_id: str, actor_id: str, group_id: Any, lesson_id: str, date_from: date, date_to: Optional[date] = None) -> List[str]:
        """A WEEKLY lesson that appears after a day off was given (a new slot, or a revision that moves a series onto a new day / time): its occurrences from ``date_from`` that fall into a
        LIVE day off of the centre are cancelled by the same step as the day off's own lessons (``_run``: the usual cancellation, kind ``holiday``, ``day_off_id``, «Выходной (…)»);
        what is already cancelled stays.  Returns the warnings of the answer (``DAY_OFF_CANCELLED`` / ``DAY_OFF_NOT_CANCELLED``: «не удалось … отмените вручную»).  A one-off lesson
        never comes here: it is a deliberate lesson (a make-up), only ``warnings_for_date`` tells the author about the day off."""
        live = (self.db.query(OrgDayOff).filter(OrgDayOff.org_id == org_id, OrgDayOff.voided_at.is_(None), OrgDayOff.date_to >= date_from)
                .order_by(OrgDayOff.created_at, OrgDayOff.id).all())
        warnings: List[Dict[str, str]] = []
        for day_off in live:
            first, last = max(day_off.date_from, date_from), day_off.date_to if date_to is None else min(day_off.date_to, date_to)
            if last < first:
                continue
            found, rows = self._lessons(org_id, first, last, group_id)
            found = [(g, o) for g, o in found if o["id"] == lesson_id]
            cancelled, blocked, converted, _, _ = self._run(org_id, actor_id, day_off.id, day_off.reason, found, rows)
            warnings += [{"code": "DAY_OFF_CANCELLED", "message": cancelled_text(item.date, day_off.reason)} for item in cancelled]
            warnings += [{"code": "DAY_OFF_CONVERTED", "message": converted_text(item.date, day_off.reason)} for item in converted]
            warnings += [{"code": "DAY_OFF_NOT_CANCELLED", "message": not_cancelled_text(item.date, day_off.reason, item.reason)} for item in blocked]
        return warnings

    def apply_to_group(self, org_id: str, actor_id: str, group_id: Any) -> List[Dict[str, str]]:
        """W1 S10 (Д7): a group that comes back from the archive meets the live days off of the centre from today on: its lessons that fall into them are cancelled by the same
        step (``_run``).  R3 11: nothing fails silently - a day off that could not be applied (an error, the exam-service down) is marked ``incomplete`` («Продолжить» finishes it) and the
        answer carries ``DAY_OFF_NOT_APPLIED``; a lesson that stays comes back as ``DAY_OFF_NOT_CANCELLED``.  Returns the warnings."""
        today = bc.business_today()
        live = (self.db.query(OrgDayOff).filter(OrgDayOff.org_id == org_id, OrgDayOff.voided_at.is_(None), OrgDayOff.date_to >= today)
                .order_by(OrgDayOff.created_at, OrgDayOff.id).all())
        warnings: List[Dict[str, str]] = []
        for day_off in [(d.id, d.reason, d.date_from, d.date_to) for d in live]:
            day_off_id, reason, first_day, last_day = day_off
            unfinished = False
            try:
                found, rows = self._lessons(org_id, max(first_day, today), last_day, group_id)
                _, blocked, _, _, down = self._run(org_id, actor_id, day_off_id, reason, found, rows)
                warnings += [{"code": "DAY_OFF_NOT_CANCELLED", "message": not_cancelled_text(item.date, reason, item.reason)} for item in blocked if item.reason != EXAM_DOWN]
                unfinished = down
            except Exception:
                self.db.rollback()
                logger.exception("restore: the day off %s was not applied to group %s", day_off_id, group_id)
                unfinished = True
            if unfinished:
                self.db.query(OrgDayOff).filter(OrgDayOff.id == day_off_id).update({"incomplete": True})
                self.db.commit()
                warnings.append({"code": "DAY_OFF_NOT_APPLIED", "message": f"Выходной «{reason}» ({_dm(first_day)}–{_dm(last_day)}) не применён к урокам вернувшейся группы полностью: откройте «Выходные дни» и нажмите «Продолжить»"})
        return warnings

    def warnings_for_date(self, org_id: str, day: date) -> List[Dict[str, str]]:
        """A one-off lesson or a MOVE onto a date of a live day off is allowed (a make-up, a deliberate lesson): the author only gets the warning ``DAY_OFF``."""
        found = self.for_date(org_id, day).dayOff
        return [{"code": "DAY_OFF", "message": on_day_off_text(day, found.reason)}] if found else []

    def _convert(self, row: GroupAttendance, group: Group, day_off_id: UUID, reason: str, actor_id: str) -> bool:
        """A lesson cancelled by hand «по вине центра» on a date of the day off: the day is a holiday whoever cancelled it (audited, no recalculation from now on).  The row is locked and
        read again (a second writer may have changed it), ``day_off_id`` is set (the day off owns it now: «Отменить выходной» and the day-off checks see it).  ``False``: it was not
        convertible any more."""
        row = self.db.query(GroupAttendance).filter(GroupAttendance.id == row.id).with_for_update().populate_existing().first()
        if not self._convertible(row):
            self.db.rollback()
            return False
        row.converted_from_kind = row.cancellation_kind          # the marker: what it WAS (``centre``) - «Отменить выходной» gives it back, the deduction with it
        row.cancellation_kind = HOLIDAY
        row.day_off_id = day_off_id
        row.version = int(row.version or 1) + 1
        self.db.add(GroupAttendanceAudit(
            attendance_id=row.id, org_id=row.org_id, group_id=row.group_id, actor_id=UUID(str(actor_id)), action="corrected", before_status="cancelled",
            after_status="cancelled", before_records={}, after_records={}, correction_reason=lesson_reason_text(reason)[:500],
            cancellation_reason=row.cancellation_reason, details={"cancellation_kind": {"from": CENTRE, "to": HOLIDAY}, "day_off_id": str(day_off_id)}))
        self._journal_kind(group, row, actor_id, CENTRE, HOLIDAY, f"Выходной «{reason}»: отмена урока стала «праздник», вычета у учеников нет", "day_off")      # #157 A3 (LESSONS 47): the money moved - the journal says so
        self.db.commit()
        return True

    def _journal_kind(self, group: Optional[Group], att: GroupAttendance, actor_id: Any, before: str, after: str, text: str, trigger: str) -> None:
        """#157 A3: a day off turned the kind of a hand cancellation (or gave it back): one record of the journal «Замены» (``KIND_CHANGED``, author - whoever made / cancelled the day off)."""
        if group is None:
            return
        from .lesson_substitutions import Substitutions  # lazy: it imports this module

        Substitutions(self.svc).record_kind_change(group, att.lesson_id, date.fromisoformat(att.lesson_date), UUID(str(actor_id)), before, after, text[:500], trigger=trigger)

    def _cancel(self, org_id: str, actor_id: str, group: Group, occurrence: Dict[str, Any], reason: str, day_off_id: UUID) -> Optional[str]:
        """The usual cancellation (``Substitutions.cancel_lesson``), kind ``holiday``, of a lesson that has no journal row (a started one is never touched: the row is read again
        under the lock).  ``None`` = done, else the reason it was refused (``EXAM_DOWN``: stop asking)."""
        from .lesson_substitutions import Substitutions  # lazy: it imports this module

        try:
            Substitutions(self.svc).cancel_lesson(org_id, actor_id, group.id, occurrence["id"], date.fromisoformat(occurrence["occurrenceDate"]),
                                                  reason=lesson_reason_text(reason), kind=HOLIDAY, day_off_id=day_off_id)
            self.db.commit()
        except RuntimeError:
            self.db.rollback()
            return EXAM_DOWN
        except (ValueError, PermissionError) as exc:
            self.db.rollback()
            return str(exc)
        except HTTPException as exc:
            self.db.rollback()
            detail = exc.detail
            if isinstance(detail, dict) and detail.get("code") == "EXAM_SERVICE_UNAVAILABLE":
                return EXAM_DOWN
            if isinstance(detail, dict) and detail.get("code") == "ALREADY_CANCELLED":     # another apply (a second window) cancelled it a moment ago: it is done
                return None
            return detail.get("message") if isinstance(detail, dict) else str(detail)
        except Exception:     # one lesson must not stop the others
            self.db.rollback()
            logger.exception("day off: lesson %s of group %s was not cancelled", occurrence.get("id"), group.id)
            return "Не удалось отменить урок"
        return None

    # ---------------------------------------------------------------------------------------------- void

    def void(self, org_id: str, actor_id: str, actor_role: str, day_off_id: UUID, *, dry_run: bool = False) -> DayOffVoided:
        """«Отменить выходной»: the lessons THIS day off cancelled come back when they are still in the future and unchanged since (the row is as the cancellation made
        it: version 1, kind ``holiday``) and no other live day off covers the date; every other one is listed with the reason and stays cancelled.  What comes back is
        recorded (``restored`` of the day off + a ``group_audit`` row per group) BEFORE the cancellation rows are deleted."""
        row = self.db.query(OrgDayOff).filter(OrgDayOff.id == day_off_id, OrgDayOff.org_id == org_id).with_for_update().first()
        if row is None:
            raise not_found("DAY_OFF_NOT_FOUND", "Выходной не найден")
        if row.voided_at is not None:
            raise conflict("DAY_OFF_VOIDED", "Этот выходной уже отменён")
        today = bc.business_today()
        others = (self.db.query(OrgDayOff).filter(OrgDayOff.org_id == org_id, OrgDayOff.voided_at.is_(None), OrgDayOff.id != row.id).all())
        groups = {g.id: g for g in self.db.query(Group).filter(Group.org_id == org_id)}
        atts = (self.db.query(GroupAttendance).filter(GroupAttendance.org_id == org_id, GroupAttendance.day_off_id == row.id)
                .order_by(GroupAttendance.lesson_date, GroupAttendance.group_id).with_for_update().all())
        names = self._names(a.marked_by for a in atts)
        restored: List[DayOffLesson] = []
        kept: List[DayOffLesson] = []
        trace: List[Dict[str, Any]] = []
        doomed: List[GroupAttendance] = []
        lines = self._preload_lines(atts, groups)          # the schedule of every (group, day) of the rows read ONCE: the times of the lines
        for att in atts:
            group = groups.get(att.group_id)
            day = date.fromisoformat(att.lesson_date)
            item = self._att_item(att, group, lines)
            if att.converted_from_kind is not None and att.status == LessonAttendanceStatus.CANCELLED.value:
                covering = next((o for o in others if o.date_from <= day <= o.date_to), None)
                if covering is not None:
                    item.reason = "Дата входит в другой выходной"
                    if not dry_run:
                        att.day_off_id = covering.id           # the covering day off wins: holiday under THAT one (the marker stays: voiding it gives the kind back); the reason is the author's own
                else:
                    item.reason = "Ручная отмена: вид «по вине центра» возвращён"
                    if not dry_run:
                        self._revert_conversion(att, row, actor_id, group)
                kept.append(item)
                continue
            why = self._not_restorable(att, today, group)
            if why is None and day == today and self._started_today(group, att, lines):
                why = "Урок сегодня уже начался: остаётся отменённым"          # W1 S10+ (Д17): a lesson of today whose time has passed does not come back as a «пропущенный»
            covering = next((o for o in others if o.date_from <= day <= o.date_to), None) if why is None else None
            if covering is not None:
                why = "Дата входит в другой выходной"
                if not dry_run:
                    att.cancellation_reason = lesson_reason_text(covering.reason)          # the reason the students read is the one of the day off that holds the date now (R3 17)
                    att.day_off_id = covering.id            # W1 S10 (Д4): the lesson stays cancelled and now belongs to the day off that still holds the date (cancelling THAT one gives it back)
            if why:
                item.reason = why
                kept.append(item)
                continue
            restored.append(item)
            doomed.append(att)
            trace.append({"groupId": item.groupId, "groupName": item.groupName, "lessonId": item.lessonId, "date": att.lesson_date,
                          "cancelledBy": names.get(att.marked_by), "cancelledAt": att.created_at.isoformat() if att.created_at else None,
                          "reason": att.cancellation_reason})
        if not dry_run:
            row.restored = trace
            row.voided_at = datetime.utcnow()
            row.voided_by = UUID(str(actor_id))
            by_group: Dict[Any, List[Dict[str, Any]]] = {}
            for entry, att in zip(trace, doomed):
                by_group.setdefault(att.group_id, []).append(entry)
            for group_id, entries in by_group.items():
                self.db.add(GroupAudit(
                    org_id=UUID(str(org_id)), group_id=group_id, actor_id=UUID(str(actor_id)),
                    actor_role=actor_role if actor_role in ("owner", "admin", "teacher", "superadmin") else "admin", action="day_off_voided",
                    snapshot={"day_off_id": str(row.id), "reason": row.reason, "dateFrom": row.date_from.isoformat(), "dateTo": row.date_to.isoformat(), "restored": entries}))
            self.db.flush()
            warnings = self._substitutes_not_restored(doomed, groups, lines)
            from .lesson_substitutions import mark_substitutions_not_restored, reopen_requests_closed_by_day_off

            reopen_requests_closed_by_day_off(self.svc, row, doomed, UUID(str(actor_id)))          # W1 S10+ (Д28); #157 A4: every request it closed learns the day off is gone
            mark_substitutions_not_restored(self.svc, row, doomed, UUID(str(actor_id)))            # #157 A4: so does every substitution it took off and did not give back
            for att in doomed:
                self.db.delete(att)
            self.db.commit()
            logger.info("day off voided org=%s id=%s restored=%d kept=%d", org_id, row.id, len(restored), len(kept))
            row = self.db.query(OrgDayOff).filter(OrgDayOff.id == day_off_id).one()
        else:
            warnings = self._substitutes_not_restored(doomed, groups, lines)
        return DayOffVoided(dayOff=self._out(row), restored=restored, kept=kept, dryRun=dry_run, warnings=warnings,
                            examsNote=EXAMS_NOTE_VOID if restored else "")

    def _revert_conversion(self, att: GroupAttendance, day_off: OrgDayOff, actor_id: str, group: Optional[Group] = None) -> None:
        """A hand-made cancellation that THIS day off turned into a holiday goes back to the kind it had (``centre``), without a day off, with its own reason; it stays cancelled (audited)."""
        before = att.cancellation_kind
        att.cancellation_kind, att.day_off_id, att.converted_from_kind = att.converted_from_kind, None, None
        att.version = int(att.version or 1) + 1
        self.db.add(GroupAttendanceAudit(
            attendance_id=att.id, org_id=att.org_id, group_id=att.group_id, actor_id=UUID(str(actor_id)), action="corrected", before_status="cancelled", after_status="cancelled",
            before_records={}, after_records={}, correction_reason=f"Выходной «{day_off.reason}» отменён: ручная отмена вернулась к прежнему виду"[:500], cancellation_reason=att.cancellation_reason,
            details={"cancellation_kind": {"from": before, "to": att.cancellation_kind}, "day_off_voided": str(day_off.id)}))
        self._journal_kind(group, att, actor_id, before, att.cancellation_kind, f"Выходной «{day_off.reason}» отменён: отмена урока снова «по вине центра»", DAY_OFF_VOID_KIND)      # #157 A3

    def _started_today(self, group: Optional[Group], att: GroupAttendance, lines: Dict[Any, Dict[str, Any]]) -> bool:
        """The lesson of the row is today and its start has passed (a lesson that is gone from the schedule is not «started»)."""
        if group is None:
            return False
        day = date.fromisoformat(att.lesson_date)
        occ = self._occurrence_of(group, att, lines)
        try:
            return bool(occ) and bc.lesson_instant_utc(day, hhmm_to_minutes(occ["startTime"])) <= bc.utc_now()
        except (KeyError, ValueError, AttributeError):
            return False

    def lost_substitution(self, att: GroupAttendance) -> Optional[LessonTeacherAssignment]:
        """THE condition of «the substitute did not come back» (the warning of the answer and the mark in the journal «Замены» both ask it): the assignment the author of THIS cancellation took off
        the lesson, and no live assignment on the lesson now (somebody was assigned again meanwhile = nothing was lost)."""
        asg = (self.db.query(LessonTeacherAssignment).filter(LessonTeacherAssignment.group_id == att.group_id, LessonTeacherAssignment.lesson_id == att.lesson_id,
                                                              LessonTeacherAssignment.lesson_date == att.lesson_date, LessonTeacherAssignment.cancelled_at.isnot(None),
                                                              LessonTeacherAssignment.cancelled_by == att.marked_by).order_by(LessonTeacherAssignment.cancelled_at.desc()).first())
        if asg is None or self.db.query(LessonTeacherAssignment.id).filter(LessonTeacherAssignment.group_id == att.group_id, LessonTeacherAssignment.lesson_id == att.lesson_id,
                                                                           LessonTeacherAssignment.lesson_date == att.lesson_date, LessonTeacherAssignment.cancelled_at.is_(None)).first():
            return None
        return asg

    def _substitutes_not_restored(self, doomed: List[GroupAttendance], groups: Dict[Any, Group], lines: Optional[Dict[Any, Dict[str, Any]]] = None) -> List[DayOffWarning]:
        """W1: the lessons that come back WITHOUT the substitute the day off took off them (the author of the cancellation took the assignment off in the same step): the administrator
        assigns them again."""
        out: List[DayOffWarning] = []
        lines = {} if lines is None else lines
        for att in doomed:
            asg = self.lost_substitution(att)
            if asg is None:
                continue
            who = short_name(self.db.query(User).filter(User.id == asg.teacher_id).first())          # “Рахимов Т.” as everywhere (LESSONS 48), not the full name
            group = groups.get(att.group_id)
            day = date.fromisoformat(att.lesson_date)
            line = self._att_item(att, group, lines)
            out.append(DayOffWarning(code="SUBSTITUTE_NOT_RESTORED", groupId=str(att.group_id), groupName=line.groupName or None, lessonId=att.lesson_id, date=day, startTime=line.startTime, endTime=line.endTime,
                                     substituteName=who,
                                     message=f"{_dm(day)}{(' — «' + group.name + '»') if group else ''}: замена ({who or 'преподаватель'}) была снята выходным и не вернулась — назначьте её заново"))
        return out

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
