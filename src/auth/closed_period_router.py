"""Money-1 (A1): the closed month of a teacher - who may still be corrected and «Переоткрыть месяц».

Mounted by ``router.py`` (so under ``/api/v1/auth``).  The state of the months is a fact of the centre (no sums): any owner / administrator reads it; reopening moves money, so it is
behind the one dependency of the money rights (``require_payments_access``: the owner, an administrator the owner allowed)."""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from uuid import UUID

from src.core.database import get_db

from . import closed_period
from .dependencies import require_org_admin, require_payments_access

router = APIRouter(tags=["ClosedPeriod"])


class ReopenRequest(BaseModel):
    teacher_id: UUID
    reason: str = Field(..., min_length=3, max_length=500)


@router.get("/org/payroll-periods")
def list_payroll_periods(month: str = Query(..., max_length=7), user: dict = Depends(require_org_admin), db: Session = Depends(get_db)):
    """Месяц у каждого преподавателя с ведомостью: ``state`` = ``open`` | ``closed`` | ``reopened`` (``closedOn`` — когда ведомость сохранена после конца месяца)."""
    return {"month": month, "items": closed_period.list_periods(db, user["org_id"], month)}


@router.post("/org/payroll-periods/{month}/reopen")
def reopen_payroll_period(month: str, request: ReopenRequest, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Переоткрыть закрытый месяц преподавателя (причина обязательна, пишется в журнал); повтор — ``changed: false``."""
    return closed_period.reopen_month(db, user["org_id"], request.teacher_id, month, user["user_id"], request.reason)
