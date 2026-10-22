"""The internal calls of the operations package to exam-service (LOGIC-B O4 / O8).

Auth owns lessons and groups, exam-service owns exams, attempts and access codes.  Each call is short (5 s) and idempotent on the other side.

* ``completion_check`` / ``lesson_completed`` (O8, ending a lesson): FAIL-OPEN - the lesson is more important than the exam (payroll): an
  unreachable exam-service lets the lesson end with a warning (``examSync.warning``);
* ``group_archive_impact`` / ``group_archive_release`` (O4, archiving a group): FAIL-CLOSED - ``EXAM_SERVICE_UNAVAILABLE`` (503).

Tests replace ``_post`` (one seam for every call).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from src.core.config import get_settings

logger = logging.getLogger(__name__)

TIMEOUT = 5.0


class ExamBridgeUnavailable(Exception):
    """exam-service is not configured, unreachable or answered with an error."""


class ExamBridgeConflict(Exception):
    """exam-service refused (409) - the reason is its ``detail``."""

    def __init__(self, detail: Any) -> None:
        super().__init__(str(detail))
        self.detail = detail


def _post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_settings()
    if not settings.internal_service_token:
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
        except ValueError:
            detail = None
        raise ExamBridgeConflict(detail)
    if response.status_code != 200:
        logger.error("exam-service %s answered %s: %s", path, response.status_code, response.text[:300])
        raise ExamBridgeUnavailable(f"status {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise ExamBridgeUnavailable("not json") from exc


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
