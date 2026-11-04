"""Money-1 A2 / A3: «Вернуть проведённый урок» and the impact of a correction of a held lesson.

Mounted by ``router.py`` (under ``/api/v1/auth``).  Both move or show money of a held lesson: the one dependency of the money rights (``require_payments_access``)."""
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.core.database import get_db

from .dependencies import require_payments_access
from .lesson_return import AttendanceImpactRequest, ReturnLessonRequest, correction_impact, return_completed_lesson

router = APIRouter(tags=["LessonReturn"])


@router.post("/org/groups/{group_id}/attendance/return")
def return_held_lesson(group_id: UUID, request: ReturnLessonRequest, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Вернуть проведённый урок в «не проведён» (причина обязательна, месяц должен быть открыт). ``dryRun`` — только последствия для денег; ответ: ``moneyEffects``."""
    return return_completed_lesson(db, user["org_id"], group_id, request, actor_id=user["user_id"], owner=user.get("org_role") == "owner")


@router.post("/org/groups/{group_id}/attendance/impact")
def attendance_correction_impact(group_id: UUID, request: AttendanceImpactRequest, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Что исправление отметок проведённого урока сделает с деньгами (ничего не записывает): ответ ``moneyEffects``."""
    return correction_impact(db, user["org_id"], group_id, request, owner=user.get("org_role") == "owner")
