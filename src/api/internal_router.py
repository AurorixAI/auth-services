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
from src.auth.service import AuthService
from src.core.config import get_settings


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
    if not x_internal_service_token or not secrets.compare_digest(
        x_internal_service_token, expected
    ):
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
        _upsert_skill_states(
            db=db,
            student_id=saved["student_id"],
            org_id=saved.get("org_id"),
            theta=body.final_theta,
            topic_breakdown=body.topic_breakdown,
            gaps=set(body.identified_gaps or []),
        )

    return saved


def _upsert_skill_states(
    db: Session,
    student_id: str,
    org_id: Optional[str],
    theta: Optional[float],
    topic_breakdown: Dict[str, Any],
    gaps: set,
) -> None:
    """
    Upsert StudentSkillState for each topic in diagnostic breakdown.
    Alpha (per-topic mastery score) maps to status:
      alpha >= 0.4  → mastered
      alpha >= -0.2 → learning
      alpha >= -0.8 → struggling
      else          → gap
    """
    import uuid as _uuid
    from datetime import datetime, timezone
    from src.models.analytics import StudentSkillState

    now = datetime.now(timezone.utc)
    student_uuid = _uuid.UUID(student_id)
    org_uuid = _uuid.UUID(org_id) if org_id else None

    for skill_id, info in topic_breakdown.items():
        alpha: float = info.get("alpha", 0.0)

        if alpha >= 0.4:
            status = "mastered"
        elif alpha >= -0.2:
            status = "learning"
        elif alpha >= -0.8:
            status = "struggling"
        else:
            status = "gap"

        # Override: if confirmed as gap by IRT engine
        if skill_id in gaps:
            status = "gap"

        # Mastery probability: sigmoid-like mapping of alpha [-2, 2] → [0, 1]
        mastery = round(max(0.0, min(1.0, 0.5 + alpha / 4.0)), 4)

        existing = (
            db.query(StudentSkillState)
            .filter(
                StudentSkillState.student_id == student_uuid,
                StudentSkillState.skill_id == skill_id,
            )
            .first()
        )

        if existing:
            prev_mastery = float(existing.mastery_probability or 0.5)
            if mastery > prev_mastery + 0.05:
                existing.trend = "improving"
            elif mastery < prev_mastery - 0.05:
                existing.trend = "declining"
            else:
                existing.trend = "stable"
            existing.mastery_probability = mastery
            existing.status = status
            existing.theta = theta
            existing.last_attempt_at = now
            existing.updated_at = now
        else:
            state = StudentSkillState(
                student_id=student_uuid,
                org_id=org_uuid,
                skill_id=skill_id,
                mastery_probability=mastery,
                status=status,
                trend="stable",
                theta=theta,
                last_attempt_at=now,
                updated_at=now,
            )
            db.add(state)

    db.commit()
