"""
Internal router — called only by trusted microservices (diagnostic-service, exam-service).
NOT exposed externally; in production this should be behind network-level ACL.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any, Dict, List, Optional

from src.api.database import get_db
from src.auth.diagnostic_skill_states import apply_skill_states
from src.auth.service import AuthService
from src.core.config import get_settings


def has_valid_internal_token(token: Optional[str]) -> bool:
    """Whether ``token`` is the first-party service token (constant-time compare; no secret configured = never).

    The yes/no form of ``require_internal_service_token``, for a PUBLIC route that answers an internal caller with
    more than it tells the world."""
    expected = get_settings().internal_service_token
    if not (expected and token):
        return False
    try:
        # Bytes, not str: compare_digest raises TypeError on a non-ASCII str, and a header value from the outside
        # can be anything (it must be a refusal, never a 500).
        return secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
    except (AttributeError, TypeError, UnicodeError):
        return False


def require_internal_service_token(
    x_internal_service_token: Optional[str] = Header(None, alias="X-Internal-Service-Token"),
) -> None:
    """Authenticate only first-party service-to-service calls.

    These endpoints mutate diagnostic state and must never be protected merely
    by a Docker-network assumption.  A missing server-side secret fails closed
    so a production misconfiguration cannot silently expose the callbacks.
    """
    expected = get_settings().internal_service_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Внутренняя связка сервисов не настроена",
        )
    if not has_valid_internal_token(x_internal_service_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недействительный внутренний токен сервиса",
        )


router = APIRouter(
    prefix="/internal",
    tags=["Internal"],
    dependencies=[Depends(require_internal_service_token)],
)


def _svc(db: Session) -> AuthService:
    return AuthService(db)


# ── Cross-service org scoping ─────────────────────────────────────────────────
# Lets diagnostic-service (and other services) resolve "which student IDs
# belong to this org" without holding its own copy of org membership — used
# to scope analytics queries to the caller's own organization.

@router.get("/org/{org_id}/student-ids", response_model=List[str],
            summary="[Internal] All student IDs belonging to an org")
def internal_org_student_ids(org_id: str, db: Session = Depends(get_db)):
    from src.models.auth import Student

    rows = db.query(Student.id).filter(Student.org_id == org_id).all()
    return [str(r[0]) for r in rows]


@router.get(
    "/org/{org_id}/groups/{group_id}/lessons/{lesson_id}/attendance",
    summary="[Internal] Точный статус проведения одного занятия",
)
def internal_lesson_attendance_status(
    org_id: str,
    group_id: str,
    lesson_id: str,
    lesson_date: str,
    db: Session = Depends(get_db),
):
    """Return the authoritative attendance lifecycle for one occurrence.

    The exam service must not infer that a lesson has started from its clock
    or from a client request.  This narrowly scoped internal endpoint lets it
    check the exact immutable occurrence before making an online lobby public.
    A missing row means the teacher has not started the lesson yet.
    """
    try:
        date.fromisoformat(lesson_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата урока должна иметь формат YYYY-MM-DD",
        ) from exc

    from src.models.auth import GroupAttendance

    attendance = (
        db.query(GroupAttendance)
        .filter(
            GroupAttendance.org_id == org_id,
            GroupAttendance.group_id == group_id,
            GroupAttendance.lesson_id == lesson_id,
            GroupAttendance.lesson_date == lesson_date,
        )
        .first()
    )
    return {
        "status": attendance.status if attendance else "scheduled",
        "started_at": attendance.started_at.isoformat() if attendance and attendance.started_at else None,
    }


# ── Diagnostic callbacks ──────────────────────────────────────────────────────

class DiagStartCallback(BaseModel):
    session_id: str
    # When the engine created the session.  Optional (older payloads lack it); needed only for a LATE callback that
    # reaches an invite cancelled meanwhile: it is accepted when the session started before the cancellation.
    session_started_at: Optional[datetime] = None


class DiagCompleteCallback(BaseModel):
    session_id: str
    session_started_at: Optional[datetime] = None
    final_theta: Optional[float] = None
    se: Optional[float] = None
    total_items: Optional[int] = None
    correct_answers: Optional[int] = None
    identified_gaps: Optional[List[str]] = None
    accuracy: Optional[float] = None
    # per-topic mastery breakdown: {topic_id: {"alpha": float, "level": str}}
    topic_breakdown: Optional[Dict[str, Any]] = None
    # Level decided by diagnostic-service (level_policy). Optional: older
    # payloads lack them and auth falls back to theta for those rows.
    overall_level: Optional[str] = None
    level_status: Optional[str] = None
    class_equivalent: Optional[str] = None
    level_provisional: Optional[bool] = None


@router.post("/diagnostic/{code}/start", status_code=200,
             summary="[Internal] diagnostic-service → пометить invite in_progress")
def internal_start_diagnostic(
    code: str,
    body: DiagStartCallback,
    db: Session = Depends(get_db),
):
    try:
        return _svc(db).start_diagnostic(
            code,
            cat_session_id=body.session_id,
            session_started_at=body.session_started_at.isoformat() if body.session_started_at else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/diagnostic/{code}/complete", status_code=200,
             summary="[Internal] diagnostic-service → сохранить результат и пометить completed")
def internal_complete_diagnostic(
    code: str,
    body: DiagCompleteCallback,
    db: Session = Depends(get_db),
):
    result_data: Dict[str, Any] = {
        "session_id": body.session_id,
        "session_started_at": body.session_started_at.isoformat() if body.session_started_at else None,
        "final_theta": body.final_theta,
        "se": body.se,
        "total_items": body.total_items,
        "correct_answers": body.correct_answers,
        "identified_gaps": body.identified_gaps or [],
        "accuracy": body.accuracy,
        "overall_level": body.overall_level,
        "level_status": body.level_status,
        "class_equivalent": body.class_equivalent,
        "level_provisional": body.level_provisional,
    }
    try:
        saved = _svc(db).complete_diagnostic(code, result_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Upsert StudentSkillState per topic from diagnostic breakdown
    if body.topic_breakdown and saved.get("student_id"):
        # Replayed by the sender's outbox (retries, requeues): idempotent, a malformed topic is skipped (DM-036).
        apply_skill_states(
            db,
            student_id=saved["student_id"],
            org_id=saved.get("org_id"),
            theta=body.final_theta,
            topic_breakdown=body.topic_breakdown,
            gaps=set(body.identified_gaps or []),
            result_at=body.session_started_at,
        )

    return saved
