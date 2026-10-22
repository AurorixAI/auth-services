"""Rules of the recurring schedule that the operations package adds (LOGIC-B O1 "valid from", O4 inactive intervals, O11 no invented data).

Pure helpers used by ``AuthService._resolved_occurrences_for_group`` plus the creation of a slot / one-off lesson.  The rules:

* a slot has ``validFrom`` (``YYYY-MM-DD``, business date); a day earlier than it has no lesson (INV-O1-1).  A legacy slot without it follows the
  business day of the group's creation and is logged (``schedule_slot_without_valid_from``);
* a lesson whose planned start lies inside an inactive interval of the group (``groups.inactive_intervals``: archived) does not exist;
* a slot without a time is not a lesson (``schedule_slot_without_time``), a room nobody typed stays ``null`` (no "Кабинет 204").
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from sqlalchemy.orm.attributes import flag_modified

from . import business_clock as bc
from .lesson_teachers import BusyItem, LessonTeachers, TeacherBook
from .models import Group
from .ops_rules import (
    conflict,
    group_archived,
    hhmm_to_minutes,
    minutes_overlap,
    normalize_room,
    room_key,
    unprocessable,
)

logger = logging.getLogger(__name__)

VALID_FROM_MAX_AHEAD_DAYS = 365
CHECK_HORIZON_DAYS = 56
ROOM_WARNING_LIMIT = 3
ISO = "%Y-%m-%dT%H:%M:%S"


# ----------------------------------------------------------------------------------------------- validFrom


def legacy_valid_from(group: Group) -> str:
    """A slot without ``validFrom`` (an old writer / a rolled-back image): the business day the group was created."""
    created = getattr(group, "created_at", None)
    if created is None:
        return "0001-01-01"
    return bc.utc_to_business_date(created).isoformat()


_PATTERN_ONLY: contextvars.ContextVar[bool] = contextvars.ContextVar("schedule_pattern_only", default=False)


@contextlib.contextmanager
def ignoring_valid_from():
    """Inside the block every slot is valid from the beginning of time: the group's FULL weekly pattern, as the old code generated it.  Used for the
    denominator of the proration of a partial period («lessons of a full month by the schedule»): ``validFrom`` only says from when a slot
    produces REAL lessons (the numerator), it never shrinks the month."""
    token = _PATTERN_ONLY.set(True)
    try:
        yield
    finally:
        _PATTERN_ONLY.reset(token)


def slot_valid_from(group: Group, slot: Dict[str, Any]) -> str:
    if _PATTERN_ONLY.get():
        return "0001-01-01"
    value = slot.get("validFrom")
    if isinstance(value, str) and len(value) == 10:
        return value
    logger.warning("schedule_slot_without_valid_from group=%s slot=%s", group.id, slot.get("id"))
    return legacy_valid_from(group)


def slot_has_time(payload: Dict[str, Any]) -> bool:
    ok = bool(payload.get("startTime")) and bool(payload.get("endTime"))
    if not ok:
        logger.info("schedule_slot_without_time payload=%s", {k: payload.get(k) for k in ("dayOfWeek", "startTime", "endTime")})
    return ok


# ------------------------------------------------------------------------------------------ inactive intervals


def parse_intervals(group: Group) -> List[Tuple[datetime, Optional[datetime]]]:
    """The intervals of the group.  An OPEN interval on a group that is ``active`` is a leftover of a restore done by an older image (rollback safety): it is
    treated as closed (at the last update of the row, never before its start)."""
    out: List[Tuple[datetime, Optional[datetime]]] = []
    live = (getattr(group, "status", None) or "active") == "active"
    for item in getattr(group, "inactive_intervals", None) or []:
        if not isinstance(item, dict) or not item.get("from"):
            continue
        try:
            start = datetime.strptime(item["from"], ISO)
            end = datetime.strptime(item["to"], ISO) if item.get("to") else None
        except (TypeError, ValueError):
            continue
        if end is None and live:
            updated = getattr(group, "updated_at", None)
            end = updated if isinstance(updated, datetime) and updated > start else start
        out.append((start, end))
    return out


def lesson_is_inactive(intervals: Sequence[Tuple[datetime, Optional[datetime]]], day: str, start_time: Optional[str]) -> bool:
    """INV-O4-2: the planned START of the lesson lies in an interval [from, to) in which the group is archived."""
    if not intervals:
        return False
    start = hhmm_to_minutes(start_time) if start_time else 0
    instant = bc.lesson_instant_utc(date.fromisoformat(day), start)
    return any(frm <= instant and (to is None or instant < to) for frm, to in intervals)


def archived_at(group: Group) -> Optional[str]:
    """The instant (naive UTC ISO) an archived group was archived: the start of its open interval; ``None`` for a live group."""
    if (group.status or "active") != "archived":
        return None
    for frm, to in reversed(parse_intervals(group)):
        if to is None:
            return frm.strftime(ISO)
    updated = getattr(group, "updated_at", None)  # an archived group without an interval (a row written by an old image)
    return updated.strftime(ISO) if updated else None


def close_open_interval(group: Group, at: datetime, fallback_from: Optional[str] = None) -> bool:
    """Restore: the open interval ends at ``at``.  Returns whether there was one.  A group archived by an older image has no interval: with ``fallback_from``
    (``archived_at()`` read BEFORE the status changed) the interval ``[fallback_from, at)`` is written, so the archived time stays closed for lessons."""
    items = [dict(i) for i in (group.inactive_intervals or [])]
    for item in reversed(items):
        if item.get("to") is None:
            item["to"] = at.strftime(ISO)
            group.inactive_intervals = items
            flag_modified(group, "inactive_intervals")
            return True
    if fallback_from and fallback_from < at.strftime(ISO):
        items.append({"from": fallback_from, "to": at.strftime(ISO)})
        group.inactive_intervals = items
        flag_modified(group, "inactive_intervals")
    return False


def open_interval(group: Group, at: datetime) -> None:
    items = [dict(i) for i in (group.inactive_intervals or [])]
    if not any(i.get("to") is None for i in items):
        items.append({"from": at.strftime(ISO), "to": None})
    group.inactive_intervals = items
    flag_modified(group, "inactive_intervals")


# ================================================================================================== the service


class ScheduleRules:
    """Creation of slots / one-off lessons with the O1 / O11 rules; ``svc`` is the ``AuthService``."""

    def __init__(self, svc) -> None:
        self.svc = svc
        self.db = svc.db

    # ---------------------------------------------------------------------------------------- helpers
    def _lock_group(self, group: Group) -> Group:
        locked = (
            self.db.query(Group).filter(Group.id == group.id, Group.org_id == group.org_id).with_for_update().populate_existing().first()
        )
        if locked is None:
            raise ValueError("Группа не найдена")
        return locked

    @staticmethod
    def archived_error():
        return group_archived()

    def _candidate_dates(self, day_of_week: int, first: date, count: int = 8) -> List[date]:
        offset = (day_of_week - first.isoweekday()) % 7
        start = first + timedelta(days=offset)
        return [start + timedelta(days=7 * i) for i in range(count) if (start + timedelta(days=7 * i)) <= first + timedelta(days=CHECK_HORIZON_DAYS - 1)]

    def _items(self, group: Group, lesson_id: str, dates: Sequence[date], start: str, end: str) -> List[BusyItem]:
        s, e = hhmm_to_minutes(start), hhmm_to_minutes(end)
        return [BusyItem(d.isoformat(), s, e, start, end, str(group.id), group.name, lesson_id) for d in dates]

    # ------------------------------------------------------------------------------------ room warnings
    def room_warnings(self, group: Group, lesson_id: str, items: Sequence[BusyItem], room: Optional[str], fmt: Optional[str]) -> List[Dict[str, str]]:
        """``ROOM_BUSY`` (a warning, never a refusal): the same non-empty room (case / spaces ignored) is busy at an overlapping time.
        Only ``offline`` lessons."""
        key = room_key(room)
        if not key or (fmt or "offline") != "offline" or not items:
            return []
        d_from = date.fromisoformat(min(i.date for i in items))
        d_to = date.fromisoformat(max(i.date for i in items))
        others = self.db.query(Group).filter(Group.org_id == group.org_id, Group.status == "active", Group.id != group.id).all()
        if not others:
            return []
        preload = self.svc._preload_schedule_data([g.id for g in others], date_from=d_from, date_to=d_to)
        wanted = {i.date: i for i in items}
        seen: List[str] = []
        for other in others:
            for occ in self.svc._resolved_occurrences_for_group(other, date_from=d_from, date_to=d_to, preloaded=preload):
                mine = wanted.get(occ["occurrenceDate"])
                if mine is None or (occ.get("format") or "offline") != "offline" or room_key(occ.get("room")) != key:
                    continue
                try:
                    if minutes_overlap(mine.start, mine.end, hhmm_to_minutes(occ["startTime"]), hhmm_to_minutes(occ["endTime"])):
                        if other.name not in seen:
                            seen.append(other.name)
                except (KeyError, ValueError, AttributeError):
                    continue
        shown = normalize_room(room)
        return [{"code": "ROOM_BUSY", "message": f"Кабинет «{shown}» в это время занят группой «{name}»"} for name in seen[:ROOM_WARNING_LIMIT]]

    def warnings_for(self, group: Group, lesson_id: str, dates: Sequence[date], payload: Dict[str, Any]) -> List[Dict[str, str]]:
        """``ROOM_BUSY`` warnings of a transfer / a new version of the schedule for the given dates of one lesson."""
        if not dates or not payload.get("startTime") or not payload.get("endTime"):
            return []
        items = self._items(group, lesson_id, list(dates), payload["startTime"], payload["endTime"])
        return self.room_warnings(group, lesson_id, items, payload.get("room"), payload.get("format"))

    # ----------------------------------------------------------------------------------- recurring slot
    def create_lesson(self, org_id: str, req, *, actor_id: Optional[str], is_org_admin: bool):
        """``POST /org/schedule/lessons`` (SPEC 4.1, O1 / O11)."""
        from .ops_schemas import ScheduleLessonResponseB

        group = self.svc._require_group_access(org_id, req.groupId, actor_id=actor_id, is_org_admin=is_org_admin)
        group = self._lock_group(group)  # the JSON slot list is written under the lock: no lost slot (O1.R4)
        if group.status != "active":
            raise self.archived_error()
        start_minutes, _ = self.svc._validate_lesson_time_range(req.startTime, req.endTime)
        now = bc.business_now()
        today = now.date()
        valid_from = getattr(req, "validFrom", None) or today
        if valid_from < today:
            raise unprocessable("VALID_FROM_PAST", "Дата начала не может быть в прошлом")
        if valid_from > today + timedelta(days=VALID_FROM_MAX_AHEAD_DAYS):
            raise unprocessable("VALID_FROM_RANGE", "Дата начала не может быть позже чем через 365 дней")
        if valid_from == today and req.dayOfWeek == today.isoweekday() and start_minutes < now.hour * 60 + now.minute:
            raise ValueError(
                "Нельзя запланировать урок на прошедшее время сегодняшнего дня "
                f"(текущее время: {now.strftime('%H:%M')})"
            )
        fmt = req.format or "offline"
        if fmt not in {"offline", "online"}:
            raise ValueError("Формат урока должен быть offline или online")
        room = normalize_room(req.room)

        # an equal slot (day + start + end) that is still running is a double click, whatever the teacher
        for slot in group.schedule or []:
            if not isinstance(slot, dict):
                continue
            lesson_id = self.svc._lesson_id_for_slot(group, slot)
            ended = self.svc._recurring_end_override(group, lesson_id)
            if ended and ended.effective_date <= valid_from.isoformat():
                continue
            config = self.svc._configured_schedule_payload_as_of(group, lesson_id, valid_from)
            if (config.get("dayOfWeek"), config.get("startTime"), config.get("endTime")) == (req.dayOfWeek, req.startTime, req.endTime):
                raise conflict("DUPLICATE_SLOT", "Такой урок уже есть в расписании группы")

        new_id = f"lesson-{uuid4().hex[:10]}"
        new_slot = {
            "id": new_id,
            "dayOfWeek": req.dayOfWeek,
            "startTime": req.startTime,
            "endTime": req.endTime,
            "subject": req.subject or "Математика",
            "room": room,
            "format": fmt,
            "color": req.color or "blue",
            "topic": req.topic,
            "validFrom": valid_from.isoformat(),
        }
        dates = self._candidate_dates(req.dayOfWeek, valid_from)
        if valid_from == today and req.dayOfWeek == today.isoweekday():
            dates = dates  # today's lesson is checked too (its time has not passed: checked above)
        items = self._items(group, new_id, dates, req.startTime, req.endTime)
        teachers = LessonTeachers(self.svc)
        if group.teacher_id and items:
            teachers.raise_if_busy(teachers.teacher_conflicts(org_id, group.teacher_id, items, ignore={i.key for i in items}),
                                   None)
        warnings = self.room_warnings(group, new_id, items, room, fmt)
        group.schedule = [*(group.schedule or []), new_slot]
        flag_modified(group, "schedule")
        self.db.commit()
        return ScheduleLessonResponseB(
            id=new_id, groupId=str(group.id), groupName=group.name, classLevel=group.class_level or 5,
            subject=new_slot["subject"], dayOfWeek=new_slot["dayOfWeek"], startTime=new_slot["startTime"],
            endTime=new_slot["endTime"], room=room, format=fmt, color=new_slot["color"], topic=new_slot["topic"],
            validFrom=new_slot["validFrom"], warnings=warnings,
        )

    # ------------------------------------------------------------------------------------------ one-off
    def create_one_off(self, org_id: str, req, *, actor_id: Optional[str], is_org_admin: bool):
        """``POST /org/schedule/occurrences`` (one lesson on one date)."""
        from .models import GroupScheduleOneOffLesson
        from .ops_schemas import ScheduleOccurrenceResponseB
        from uuid import UUID

        if not actor_id:
            raise PermissionError("Не удалось определить автора занятия")
        group = self.svc._require_group_access(org_id, req.groupId, actor_id=actor_id, is_org_admin=is_org_admin)
        group = self._lock_group(group)
        if group.status != "active":
            raise self.archived_error()
        now = bc.business_now()
        if req.lessonDate < now.date():
            raise ValueError("Нельзя добавить разовый урок на прошедшую дату")
        fmt = req.format or "offline"
        room = normalize_room(req.room)
        payload = {
            "dayOfWeek": req.lessonDate.isoweekday(),
            "startTime": req.startTime,
            "endTime": req.endTime,
            "subject": "Математика",
            "room": room,
            "format": fmt,
            "color": req.color or "blue",
            "topic": req.topic,
        }
        start_minutes, _ = self.svc._validate_lesson_time_range(payload["startTime"], payload["endTime"])
        self.svc._validate_schedule_payload(payload)
        if req.lessonDate == now.date() and start_minutes < now.hour * 60 + now.minute:
            raise ValueError("Нельзя добавить разовый урок на прошедшее время сегодняшнего дня")
        lesson_id = f"one-off-{uuid4().hex[:16]}"
        self.svc._assert_teacher_time_is_available(
            group=group, lesson_id=lesson_id, occurrence_date=req.lessonDate, payload=payload
        )
        items = self._items(group, lesson_id, [req.lessonDate], req.startTime, req.endTime)
        warnings = self.room_warnings(group, lesson_id, items, room, fmt)
        self.db.add(
            GroupScheduleOneOffLesson(
                id=lesson_id, org_id=group.org_id, group_id=group.id, lesson_date=req.lessonDate.isoformat(),
                payload=payload, created_by=UUID(actor_id),
            )
        )
        self.db.commit()
        return ScheduleOccurrenceResponseB(
            id=lesson_id, groupId=str(group.id), groupName=group.name, classLevel=group.class_level or 5, **payload,
            occurrenceDate=req.lessonDate.isoformat(), isOneOff=True, warnings=warnings,
        )

    # ------------------------------------------------------------------------- teacher overlap (all writers)
    def assert_teacher_free(self, *, group: Group, lesson_id: str, occurrence_date: date, payload: Dict[str, Any]) -> None:
        """Reject a transfer / revision / one-off lesson that overlaps another lesson of the teacher who teaches THIS lesson (the group
        teacher on that date or a substitute): one function for every writer (``TEACHER_BUSY`` 409 with ``conflicts``)."""
        teachers = LessonTeachers(self.svc)
        book = TeacherBook(self.db)
        end = hhmm_to_minutes(payload["endTime"])
        resolved = book.resolve(group, lesson_id, occurrence_date.isoformat(), end)
        teacher_id = resolved.teacher_id
        if teacher_id is None:
            return
        cand = BusyItem(occurrence_date.isoformat(), hhmm_to_minutes(payload["startTime"]), end, payload["startTime"], payload["endTime"],
                        str(group.id), group.name, lesson_id)
        conflicts = teachers.teacher_conflicts(group.org_id, teacher_id, [cand], ignore={cand.key}, book=book)
        if conflicts:
            from .ops_rules import short_name
            from .models import User

            teachers.raise_if_busy(conflicts, short_name(self.db.query(User).filter(User.id == teacher_id).first()))
