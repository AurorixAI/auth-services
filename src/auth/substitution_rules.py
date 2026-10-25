"""Error catalogue and small rules of LOGIC-B2 (SPEC 4.4).  Errors are ``ops_rules.OpsError`` (``{"detail": {"code", "message", ...}}``)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from .ops_rules import OpsError, conflict, forbidden, not_found, unprocessable

URGENT_MINUTES = 1440          # < 24 h before the start
CREATE_MAX_LESSONS = 60
CREATE_MAX_DAYS_AHEAD = 90
MOVE_MAX_DAYS_AHEAD = 60
TEACHER_MOVE_LEAD_MINUTES = 60                  # P1.1: a teacher moves his own lesson only while more than this is left before it starts
MY_LESSONS_MAX_DAYS = 62
DECIDE_BULK_MAX = 50

ERROR_CATALOGUE: Dict[str, tuple[int, str]] = {
    "SUBSTITUTION_NOT_FOUND": (404, "Запрос на замену не найден"),
    "SUBSTITUTION_ALREADY_DECIDED": (409, "Запрос уже решён"),
    "NOT_REQUEST_OWNER": (403, "Это запрос другого преподавателя"),
    "MANAGER_ASSIGNS_DIRECTLY": (403, "Вы можете назначить замену сами: меню урока «Заменить преподавателя»"),
    "LESSON_ALREADY_STARTED": (409, "Урок уже начался или прошёл. Обратитесь к администратору"),
    "NO_LESSONS_REQUESTABLE": (409, "Ни один из выбранных уроков нельзя запросить"),
    "TOO_MANY_LESSONS": (422, "Не более 60 уроков в одном запросе"),
    "LESSON_TOO_FAR": (422, "Запрос можно подать не дальше чем на 90 дней вперёд"),
    "COMMENT_REQUIRED": (422, "Опишите причину — не менее 3 символов"),
    "SUBSTITUTE_IS_REQUESTER": (422, "Нельзя назначить заменой того, кто запросил замену"),
    "MOVE_SAME_SLOT": (422, "Выберите другую дату или время"),
    "MOVE_IN_PAST": (422, "Новое время урока уже прошло"),
    "MOVE_TOO_FAR": (422, "Перенести можно не дальше чем на 60 дней"),
    "GROUP_BUSY": (409, "У группы уже есть урок в это время"),
    "REASON_REQUIRED": (422, "Укажите причину (не менее 3 символов)"),
    "TEACHER_REQUIRED": (422, "Выберите преподавателя"),
    "MOVE_FIELDS_REQUIRED": (422, "Укажите новую дату и время урока"),
    "PERIOD_TOO_LONG": (422, "Период не может превышать 63 дня"),
}


# #124: the codes of the administrator's move without a request (kept apart: the frozen ``logicB2_contract.json`` lists ``ERROR_CATALOGUE`` only)
DIRECT_MOVE_ERRORS: Dict[str, tuple[int, str]] = {
    "LESSON_CHANGED": (409, "Расписание урока изменилось: обновите страницу и повторите"),
    "LESSON_HAS_NO_TEACHER": (422, "У урока нет действующего преподавателя: назначьте его и повторите"),
    # P1.1: the teacher moves his own lesson (the same path); the administrator is not bound by the first two
    "NOT_LESSON_TEACHER": (403, "Перенести урок может преподаватель группы или администратор"),
    "MOVE_TOO_LATE": (409, "Перенести урок можно не позднее чем за 60 минут до начала. Нажмите «Не смогу провести» — решит администратор"),
    "FOREIGN_REQUEST_PENDING": (409, "По этому уроку есть запрос другого преподавателя: решит администратор"),
}


def err(status: int, code: str, message: str = None, **extra: Any) -> OpsError:
    default = {**DIRECT_MOVE_ERRORS, **ERROR_CATALOGUE}.get(code, (status, ""))[1]
    return OpsError(status, code, message or default, **extra)


def minutes_to_start(starts_at: datetime, now_utc: datetime) -> int:
    """Whole minutes from now to the start, rounded UP (a lesson 0-59 s ahead is 1 minute: urgent, not overdue; <= 0 once it has started)."""
    return -int((-(starts_at - now_utc).total_seconds()) // 60)


def is_urgent(minutes: int) -> bool:
    return 0 < minutes < URGENT_MINUTES


def is_overdue(minutes: int) -> bool:
    return minutes <= 0
