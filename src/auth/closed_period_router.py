"""Money-1 (A1): the closed month of a teacher - who may still be corrected and «Переоткрыть месяц».

Mounted by ``router.py`` (so under ``/api/v1/auth``).  The state of the months is a fact of the centre (no sums): any owner / administrator reads it; reopening moves money, so it is
behind the one dependency of the money rights (``require_payments_access``: the owner, an administrator the owner allowed)."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from uuid import UUID

from src.core.database import get_db

from . import closed_period
from .attendance_access import is_org_manager
from .dependencies import require_org_admin, require_org_member, require_payments_access
from .ops_schemas import OpenActionsByMonth

router = APIRouter(tags=["ClosedPeriod"])


class ReopenRequest(BaseModel):
    teacher_id: UUID
    reason: str = Field("", max_length=2000)          # 3..500 after the trim is checked by the function (``REASON_REQUIRED``, a Russian sentence), not by pydantic's English 422
    sheet_revision: Optional[int] = Field(None, ge=1)   # the revision of the sheet the screen saw (``GET /org/payroll-periods`` -> ``sheetRevision``); required (R3 #11)


@router.get("/org/payroll-periods")
def list_payroll_periods(month: str = Query(..., max_length=7), user: dict = Depends(require_org_admin), db: Session = Depends(get_db)):
    """Месяц у каждого преподавателя с ведомостью: ``state`` = ``open`` | ``closed`` | ``reopened`` (``closedOn`` — когда ведомость сохранена после конца месяца)."""
    return {"month": month, "items": closed_period.list_periods(db, user["org_id"], month)}


@router.post("/org/payroll-periods/{month}/reopen")
def reopen_payroll_period(month: str, request: ReopenRequest, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Переоткрыть закрытый месяц преподавателя (причина обязательна, пишется в журнал); повтор — ``changed: false``."""
    return closed_period.reopen_month(db, user["org_id"], request.teacher_id, month, user["user_id"], request.reason, request.sheet_revision)


@router.get("/org/attendance/open-actions/by-month", response_model=OpenActionsByMonth)
def open_actions_by_month(user: dict = Depends(require_org_member), db: Session = Depends(get_db)):
    """«Уроки без отметки» по месяцам (R3 #2): все уроки без отметки открытых месяцев от начала учёта центра, самые старые месяцы первыми, с счётчиком в каждом месяце и общим.
    Тот же отбор, что у ``GET /org/attendance/open-actions`` (преподаватель видит свои уроки, владелец и администратор — все); закрытые месяцы сюда не входят."""
    from .service import AuthService  # lazy: service imports a great deal

    try:
        actions = AuthService(db).get_open_attendance_actions(user["org_id"], actor_id=user["user_id"], is_org_admin=is_org_manager(user))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    return closed_period.group_open_actions_by_month(db, user["org_id"], actions)
