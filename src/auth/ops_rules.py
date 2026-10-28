"""Shared rules and the error type of the operations package (LOGIC-B).

Every new or changed endpoint answers a refusal as ``{"detail": {"code": "<CODE>", "message": "<ru text>", ...}}``
(SPEC 0.2).  ``OpsError`` is an ``HTTPException`` so it works in every FastAPI app (also the test apps that mount
the router only) and a service called directly can be asserted with ``pytest.raises(OpsError)``.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

# ---------------------------------------------------------------------------------------------------- errors


class OpsError(HTTPException):
    """A coded refusal.  ``status_code`` follows 0.2: 403 rights, 404 no object, 409 state, 422 input, 503 outside."""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message, **extra})
        self.code = code
        self.message = message
        self.extra = extra


def forbidden(code: str, message: str, **extra: Any) -> OpsError:
    return OpsError(403, code, message, **extra)


def not_found(code: str, message: str, **extra: Any) -> OpsError:
    return OpsError(404, code, message, **extra)


def conflict(code: str, message: str, **extra: Any) -> OpsError:
    return OpsError(409, code, message, **extra)


def unprocessable(code: str, message: str, **extra: Any) -> OpsError:
    return OpsError(422, code, message, **extra)


def unavailable(code: str, message: str, **extra: Any) -> OpsError:
    return OpsError(503, code, message, **extra)


class GroupArchivedForEnrolment(ValueError):
    """Enrolment into an archived group: a ValueError (the bulk add reports it per student), the router answers 409 ``GROUP_ARCHIVED``."""


def group_archived() -> OpsError:
    """409 ``GROUP_ARCHIVED``: an archived group is read-only (SPEC O4)."""
    return conflict("GROUP_ARCHIVED", ERROR_CATALOGUE["GROUP_ARCHIVED"][1])


# code -> (HTTP status, default message); the catalogue the contract exports (``errors.json``)
ERROR_CATALOGUE: Dict[str, tuple[int, str]] = {
    "DUPLICATE_SLOT": (409, "Такой урок уже есть в расписании группы"),
    "TEACHER_BUSY": (409, "У преподавателя уже есть урок в это время"),
    "GROUP_ARCHIVED": (409, "Группа в архиве: изменить её нельзя. Верните её из архива"),
    "VALID_FROM_PAST": (422, "Дата начала не может быть в прошлом"),
    "END_BEFORE_START": (422, "Дата окончания не может быть раньше даты начала"),
    "NO_STUDENTS": (409, "В группе нет учеников — урок начать нельзя. Добавьте учеников в группу."),
    "NO_STUDENTS_COMPLETE": (409, "В составе урока нет учеников — завершить нельзя. Отметьте «Урока не было»."),
    "ATTENDANCE_CONFLICT": (409, "Отметки уже изменил другой сотрудник"),
    "LESSON_HAS_LIVE_EXAM": (409, "У урока есть незавершённый экзамен"),
    "LESSON_TOO_OLD": (403, "Урок старше 7 дней — его закрывает администратор или владелец"),
    "LESSON_OUTSIDE_WINDOW": (422, "Урок вне окна незакрытых уроков"),
    "NOT_LESSON_TEACHER": (403, "Этот урок ведёт другой преподаватель"),
    "TARGET_NOT_TEACHER": (422, "Выберите преподавателя центра"),
    "TEACHER_REQUIRED": (422, "Выберите преподавателя"),
    "LESSON_TEACHER_CHANGED": (409, "Преподаватель урока уже изменён"),
    "LESSON_CANCELLED": (409, "Урок отменён"),
    "LESSON_NOT_FOUND": (404, "Урок не найден"),
    "STATUS_USE_ARCHIVE_ACTION": (422, "Используйте «В архив» / «Вернуть из архива» у группы"),
    "GROUP_ARCHIVE_BLOCKED": (409, "Группу нельзя архивировать"),
    "ALREADY_ARCHIVED": (409, "Группа уже в архиве"),
    "NOT_ARCHIVED": (409, "Группа не в архиве"),
    "TEACHER_INACTIVE": (409, "Преподаватель группы больше не ведёт уроки"),
    "EXAM_SERVICE_UNAVAILABLE": (503, "Не удалось проверить экзамены группы. Повторите попытку."),
    "MEMBER_HAS_ASSIGNMENTS": (409, "У сотрудника есть группы или начатые уроки"),
    "NOT_REMOVED": (409, "Сотрудник не удалён"),
    "UNASSIGNED_NOT_CONFIRMED": (422, "Подтвердите, что группа останется без преподавателя"),
    "EMAIL_REQUIRED": (422, "Укажите email приглашаемого сотрудника"),
    "EMAIL_INVALID": (422, "Проверьте email приглашаемого сотрудника"),
    "ONLY_OWNER_INVITES_ADMIN": (403, "Приглашать администраторов может только владелец"),
    "ALREADY_MEMBER": (409, "Этот email уже работает в центре"),
    "EMAIL_IN_OTHER_ORG": (409, "Этот email уже зарегистрирован в другом центре. Пока один email работает только в одном центре."),
    "INVITE_EMAIL_MISMATCH": (400, "Это приглашение выдано для другого email"),
    "IMPORT_IN_PROGRESS": (409, "Импорт уже выполняется. Подождите минуту"),
    "STAFF_ROLE_REQUIRED": (403, "Добавлять учеников может администратор центра"),
    "GROUP_NOT_FOUND": (404, "Группа не найдена"),
    "MEMBER_NOT_FOUND": (404, "Сотрудник не найден"),
    "VALID_FROM_RANGE": (422, "Дата начала не может быть позже чем через 365 дней"),
}

# ---------------------------------------------------------------------------------------- window rule (decision 7)

TEACHER_LESSON_WINDOW_DAYS = 7


def teacher_lesson_window_start(today: date) -> date:
    """A teacher closes / reconciles lessons with ``lesson_date >= today - 7`` (SPEC decision 7)."""
    return today - timedelta(days=TEACHER_LESSON_WINDOW_DAYS)


def lesson_age_days(lesson_date: date, today: date) -> int:
    return (today - lesson_date).days


def is_too_old_for_teacher(lesson_date: date, today: date) -> bool:
    return lesson_date < teacher_lesson_window_start(today)


# ------------------------------------------------------------------------------------------------- small helpers

ROOM_PLACEHOLDER_LEGACY = "Кабинет 204"


def normalize_room(value: Optional[str]) -> Optional[str]:
    """``room`` is stored as typed or as ``None`` (SPEC O11): blank -> ``None``; no placeholder is invented."""
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def room_key(value: Optional[str]) -> Optional[str]:
    """Case- and space-insensitive key of a room, for the ``ROOM_BUSY`` warning."""
    room = normalize_room(value)
    return " ".join(room.lower().split()) if room else None


def minutes_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Half-open intervals: 10:00-11:30 and 11:30-13:00 do not overlap."""
    return a_start < b_end and a_end > b_start


def hhmm_to_minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def full_name(user: Any) -> Optional[str]:
    if user is None:
        return None
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return name or None


def short_name(user: Any) -> Optional[str]:
    """«Рахимов Т.» as the spec shows it (last name + initial of the first name)."""
    if user is None:
        return None
    last = (user.last_name or "").strip()
    first = (user.first_name or "").strip()
    if last and first:
        return f"{last} {first[0]}."
    return last or first or None


# ------------------------------------------------------------------------------------ student status (O15)


def student_status_view(status: Optional[str], has_group: bool) -> str:
    """``status_view`` of a student (SPEC O15): the DB status stays ("active" = "was enrolled"); the answer says what is true now.

    ``archived`` -> ``archived``; ``active`` without a live membership -> ``no_group``; a NULL status -> ``new``; else the status itself.
    """
    if status == "archived":
        return "archived"
    if status == "active" and not has_group:
        return "no_group"
    return status or "new"
