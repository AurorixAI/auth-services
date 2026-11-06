"""Money-1 V8: «Моя зарплата» of a teacher - ``GET /auth/org/me/payroll?month=YYYY-MM`` and ``GET /auth/org/me/payroll/months``.  Only the teacher, only his own data (no id in the request:
the teacher is the one of the token)."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from uuid import UUID

from src.core.database import get_db

from .dependencies import require_org_member
from .my_payroll import MyPayroll
from .payroll_service import PayrollError

router = APIRouter(prefix="/auth", tags=["Payroll"])


class MyMonth(BaseModel):
    month: str
    month_label: str
    state: str  # saved | open_preliminary | not_saved
    preliminary: bool
    lessons_counted: int
    total: Optional[int] = None
    paid: int
    balance: int  # > 0 left to pay, < 0 paid forward
    carry_in: int
    text: str
    updated_at: Optional[str] = None


class MyMonths(BaseModel):
    items: List[MyMonth]


def _teacher(user: dict) -> UUID:
    if user.get("org_role") != "teacher":
        raise HTTPException(403, "Эта страница — для преподавателя")
    return UUID(str(user["user_id"]))


@router.get("/org/me/payroll", response_model=MyMonth)
def my_payroll(month: str = Query(..., max_length=7), user: dict = Depends(require_org_member), db: Session = Depends(get_db)):
    """Зарплата преподавателя за месяц: засчитано уроков, сумма, выплачено, остаток. Закрытый месяц — по сохранённой ведомости; текущий — «предварительно». Без цен и данных учеников."""
    teacher = _teacher(user)
    try:
        return MyPayroll(db).month(user["org_id"], teacher, month)
    except PayrollError as exc:
        raise HTTPException(exc.status_code, exc.detail)


@router.get("/org/me/payroll/months", response_model=MyMonths)
def my_payroll_months(user: dict = Depends(require_org_member), db: Session = Depends(get_db)):
    """Месяцы преподавателя (текущий и последние 12 с ведомостью или проведёнными уроками), новые сверху."""
    teacher = _teacher(user)
    return {"items": MyPayroll(db).months(user["org_id"], teacher)}
