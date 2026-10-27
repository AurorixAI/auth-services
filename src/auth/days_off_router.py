"""PAY-F, part 2: the routes of «Выходной день центра» (mounted into the auth router: ``/api/v1/auth/org/days-off``).

Owner / admin of the centre give, list and cancel a day off; any member of the centre may ask whether a date is one (the hint when a lesson is created)."""
from __future__ import annotations

from datetime import date
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.core.database import get_db

from .dependencies import require_org_member
from .cancellation_kind import ChangeCancellationKind, change_cancellation_kind
from .days_off import DayOffApplied, DayOffDates, DayOffForDate, DayOffOut, DayOffPlan, DayOffRequest, DaysOffService, DayOffVoided
from .ops_router import _manager

router = APIRouter(tags=["PAY-F days off"])


def _service(db: Session) -> DaysOffService:
    from .service import AuthService  # lazy: router.py imports this module while it is being imported itself

    return DaysOffService(AuthService(db))


@router.get("/org/days-off", response_model=List[DayOffOut])
def list_days_off(user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """The journal of the days off (the live ones and the cancelled ones)."""
    return _service(db).list(user["org_id"])


@router.get("/org/days-off/for-date", response_model=DayOffForDate)
def day_off_for_date(day: date = Query(..., alias="date"), user: dict = Depends(require_org_member), db: Session = Depends(get_db)):
    """Is ``date`` a day off of the centre (a warning when a lesson is created on it - never a block)."""
    return _service(db).for_date(user["org_id"], day)


@router.post("/org/days-off/preview", response_model=DayOffPlan)
def preview_day_off(request: DayOffDates, user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """Dry run: how many lessons in how many groups would be cancelled, and which cannot be cancelled (nothing is written)."""
    return _service(db).plan(user["org_id"], request)


@router.post("/org/days-off", response_model=DayOffApplied, status_code=201)
def create_day_off(request: DayOffRequest, user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """Give the day off and cancel its lessons (the usual cancellation, kind ``holiday``)."""
    return _service(db).create(user["org_id"], user["user_id"], request)


@router.post("/org/days-off/{day_off_id}/apply", response_model=DayOffApplied)
def apply_day_off(day_off_id: UUID, user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """Finish a day off (idempotent): the lessons already cancelled stay, the rest is tried again (the exam-service did not answer, a lesson was busy)."""
    return _service(db).apply(user["org_id"], user["user_id"], day_off_id)


@router.post("/org/days-off/{day_off_id}/void", response_model=DayOffVoided)
def void_day_off(day_off_id: UUID, dry_run: bool = Query(False), user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """«Отменить выходной»: its own, untouched, future lessons come back; the others are listed (``dry_run``: only the lists)."""
    return _service(db).void(user["org_id"], user["user_id"], user["org_role"], day_off_id, dry_run=dry_run)


@router.put("/org/groups/{group_id}/attendance/cancellation-kind")
def put_cancellation_kind(group_id: UUID, request: ChangeCancellationKind, user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """«Изменить причину отмены»: the kind of a lesson that is already cancelled (``centre`` / ``holiday``), owner / admin, with a reason; the recalculation follows by itself."""
    return change_cancellation_kind(db, user["org_id"], str(group_id), request, actor_id=user["user_id"])
