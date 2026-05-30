"""
Internal router — called only by trusted microservices (diagnostic-service, exam-service).
NOT exposed externally; in production this should be behind network-level ACL.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any, Dict, List, Optional

from src.api.database import get_db
from src.auth.service import AuthService

router = APIRouter(prefix="/internal", tags=["Internal"])


def _svc(db: Session) -> AuthService:
    return AuthService(db)


# ── Diagnostic callbacks ──────────────────────────────────────────────────────

class DiagStartCallback(BaseModel):
    session_id: str


class DiagCompleteCallback(BaseModel):
    session_id: str
    final_theta: Optional[float] = None
    se: Optional[float] = None
    total_items: Optional[int] = None
    identified_gaps: Optional[List[str]] = None
    accuracy: Optional[float] = None
    # per-topic mastery breakdown: {topic_id: {"alpha": float, "level": str}}
    topic_breakdown: Optional[Dict[str, Any]] = None


@router.post("/diagnostic/{code}/start", status_code=200,
             summary="[Internal] diagnostic-service → пометить invite in_progress")
def internal_start_diagnostic(
    code: str,
    body: DiagStartCallback,
    db: Session = Depends(get_db),
):
    try:
        return _svc(db).start_diagnostic(code, cat_session_id=body.session_id)
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
        "final_theta": body.final_theta,
        "se": body.se,
        "total_items": body.total_items,
        "identified_gaps": body.identified_gaps or [],
        "accuracy": body.accuracy,
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
