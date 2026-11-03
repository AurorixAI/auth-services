"""The internal calls of the operations package to exam-service (LOGIC-B O4 / O8).

Auth owns lessons and groups, exam-service owns exams, attempts and access codes.  Each call is short (5 s) and idempotent on the other side.

* ``completion_check`` / ``lesson_completed`` (O8, ending a lesson): FAIL-OPEN - the lesson is more important than the exam (payroll): an
  unreachable exam-service lets the lesson end with a warning (``examSync.warning``);
* ``group_archive_impact`` / ``group_archive_release`` (O4, archiving a group): FAIL-CLOSED - ``EXAM_SERVICE_UNAVAILABLE`` (503).

* ``lesson_cancellation`` / ``recurring_end_impact`` / ``recurring_end_release`` (SV-14 of wave E; their transport used to be three copies in ``service.py``): FAIL-CLOSED, called
  from ``AuthService`` (the cancellation of a lesson, the stop / change of a weekly lesson); the failures reach the user through ``refusal`` - one mapping and one set of texts.

Tests replace ``_post`` (one seam for every call).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, NamedTuple, Optional

import httpx

from src.core.config import get_settings

logger = logging.getLogger(__name__)

TIMEOUT = 5.0


class ExamBridgeError(Exception):
    """Base of what the road to exam-service raises."""


class ExamBridgeUnavailable(ExamBridgeError):
    """exam-service is not configured, unreachable or answered with an error.  ``status``: the HTTP status it answered with (``None``: no answer at all)."""

    def __init__(self, message: str = "", status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class ExamBridgeBadAnswer(ExamBridgeUnavailable):
    """exam-service answered 200 with something that is not what the call needs (not JSON, a wrong shape); ``str(exc)`` is the text for the user."""


class ExamBridgeConflict(ExamBridgeError):
    """exam-service refused (409) - the reason is its ``detail``."""

    def __init__(self, detail: Any) -> None:
        super().__init__(str(detail))
        self.detail = detail


class ExamAnswerError(RuntimeError):
    """exam-service ANSWERED a call with a status that is neither 200 nor 409 (``status``): 5xx is a trouble of the moment (retried later), a 4xx is its verdict about THIS lesson (R3 6)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Texts(NamedTuple):
    """What the user reads when a call of the road fails (SV-14 of wave E: the three calls auth makes from the schedule / the cancellation of a lesson say it
    in one way): ``refused`` - exam-service said 409 without a reason of its own, ``down`` - no answer / not configured, ``failed`` - it answered with another status."""

    refused: str
    down: str
    failed: str


CANCELLATION = Texts("Нельзя отменить урок с активным экзаменом", "Сервис экзаменов временно недоступен; урок не был отменён",
                     "Не удалось синхронизировать отмену урока с экзаменом")
RECURRING_IMPACT = Texts("Нельзя изменить расписание с активным экзаменом", "Сервис экзаменов временно недоступен; остановка расписания не выполнена",
                         "Не удалось проверить связанные экзамены")
RECURRING_RELEASE = Texts("Нельзя прекратить расписание с активным экзаменом", "Сервис экзаменов временно недоступен; регулярное расписание не прекращено",
                          "Не удалось безопасно вернуть экзамены в черновики")
BAD_ANSWER = "Сервис экзаменов вернул некорректный ответ"


def _post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_settings()
    if not settings.internal_service_token:
        logger.error("exam-service call %s: the internal service token is not configured", path)
        raise ExamBridgeUnavailable("internal service token is not configured")
    endpoint = f"{settings.exam_service_url.rstrip('/')}{path}"
    try:
        response = httpx.post(
            endpoint, json=payload, headers={"X-Internal-Service-Token": settings.internal_service_token}, timeout=TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise ExamBridgeUnavailable(str(exc)) from exc
    if response.status_code == 409:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        raise ExamBridgeConflict(detail)
    if response.status_code != 200:
        logger.error("exam-service %s answered %s: %s", path, response.status_code, response.text[:300])
        raise ExamBridgeUnavailable(f"status {response.status_code}", status=response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise ExamBridgeBadAnswer(BAD_ANSWER) from exc


def refusal_text(detail: Any) -> Optional[str]:
    """The reason of a 409 as text: a string, or the ``message`` of a coded detail; ``None`` when exam-service gave none."""
    if isinstance(detail, dict):
        detail = detail.get("message")
    return detail.strip() or None if isinstance(detail, str) else None


def refusal(exc: ExamBridgeError, texts: Texts) -> Exception:
    """The exception a caller that reports to the user (the lesson cancellation, the stop / change of a weekly lesson) raises for a failure of the road, ONE mapping for
    all three calls: 409 -> ``ValueError`` (its reason, else ``texts.refused``), no answer / not configured -> ``RuntimeError(texts.down)``, another status ->
    ``ExamAnswerError(status, texts.failed)`` (a ``RuntimeError``), an answer that is not what was asked -> ``RuntimeError`` with its own text."""
    if isinstance(exc, ExamBridgeConflict):
        return ValueError(refusal_text(exc.detail) or texts.refused)
    if isinstance(exc, ExamBridgeBadAnswer):
        return RuntimeError(str(exc))
    if isinstance(exc, ExamBridgeUnavailable) and exc.status is not None:
        return ExamAnswerError(exc.status, texts.failed)
    return RuntimeError(texts.down)


def completion_check(org_id: Any, group_id: Any, lesson_id: str, lesson_date: str) -> List[Dict[str, Any]]:
    """The exams bound to the lesson that are still live: ``[{examId, title, status, remainingSeconds, waitingCount, ...}]``."""
    body = _post(
        "/internal/lesson-bindings/completion-check",
        {"org_id": str(org_id), "group_id": str(group_id), "lesson_id": lesson_id, "lesson_date": lesson_date},
    )
    return list(body.get("exams") or [])


def lesson_completed(org_id: Any, group_id: Any, lesson_id: str, lesson_date: str, actor_id: Any) -> Dict[str, List[str]]:
    """Tell exam-service the lesson is over: a lobby / print-ready exam without attempts goes back to draft, an active one runs on."""
    body = _post(
        "/internal/lesson-bindings/completed",
        {"org_id": str(org_id), "group_id": str(group_id), "lesson_id": lesson_id, "lesson_date": lesson_date,
         "actor_id": str(actor_id) if actor_id else None},
    )
    return {"released": list(body.get("released") or []), "leftRunning": list(body.get("leftRunning") or [])}


def group_archive_impact(org_id: Any, group_id: Any) -> List[Dict[str, Any]]:
    body = _post("/internal/group-bindings/archive-impact", {"org_id": str(org_id), "group_id": str(group_id)})
    return list(body.get("exams") or [])


def group_archive_release(org_id: Any, group_id: Any, actor_id: Any, reason: Optional[str]) -> List[str]:
    body = _post(
        "/internal/group-bindings/archive-release",
        {"org_id": str(org_id), "group_id": str(group_id), "actor_id": str(actor_id) if actor_id else None, "reason": reason},
    )
    return list(body.get("releasedExamIds") or [])


def lesson_cancellation(phase: str, org_id: Any, group_id: Any, lesson_id: str, lesson_date: str, reason: Optional[str]) -> None:
    """``phase``: ``cancellation-check`` (may this lesson be cancelled - no live exam?) or ``release`` (give the exams of the cancelled lesson back to drafts)."""
    _post(
        f"/internal/lesson-bindings/{phase}",
        {"org_id": str(org_id), "group_id": str(group_id), "lesson_id": lesson_id, "lesson_date": lesson_date, "reason": reason},
    )


def recurring_end_impact(org_id: Any, group_id: Any, lesson_id: str, effective_from: str) -> Dict[str, Any]:
    """The exams of the future dates of a weekly lesson that a stop / a change of it touches (exam-service is the only one who knows about sessions)."""
    body = _post(
        "/internal/lesson-bindings/recurring-end-impact",
        {"org_id": str(org_id), "group_id": str(group_id), "lesson_id": lesson_id, "effective_from": effective_from},
    )
    if not isinstance(body, dict):
        raise ExamBridgeBadAnswer(BAD_ANSWER)
    return body


def recurring_end_release(org_id: Any, group_id: Any, group_name: str, lesson_id: str, effective_from: str, actor_id: str, reason: str) -> List[str]:
    """Atomically give back to drafts only the future deliveries approved in the preview."""
    body = _post(
        "/internal/lesson-bindings/recurring-end-release",
        {"org_id": str(org_id), "group_id": str(group_id), "group_name": group_name, "lesson_id": lesson_id, "effective_from": effective_from,
         "actor_id": actor_id, "reason": reason},
    )
    released = body.get("released_exam_ids") if isinstance(body, dict) else None
    if not isinstance(released, list):
        raise ExamBridgeBadAnswer("Сервис экзаменов не подтвердил возврат экзаменов")
    return [str(exam_id) for exam_id in released]
