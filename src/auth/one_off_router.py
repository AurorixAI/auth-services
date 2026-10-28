"""#154 endpoints of the purpose of one-off lessons (``ONE_OFF_PURPOSE`` in ``one_off_purpose.py``).  Mounted into ``ops_router``.

* ``GET /org/groups/{id}/makeup-candidates`` - the cancelled lessons that can be made up (the teacher of the group, an owner / admin);
* ``GET /org/one-off-decisions`` and ``POST /org/one-off-lessons/{id}/money-decision`` - what waits for «Оплатить / Не оплачивать» and the decision (an owner / admin of this centre only)."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.database import get_db

from .attendance_access import is_org_manager
from .dependencies import require_org_member
from .ops_router import _manager
from .ops_rules import not_found

router = APIRouter(tags=["#154 one-off purpose"])


class MakeupCandidate(BaseModel):
    lessonId: str
    lessonDate: str
    startTime: str
    endTime: str
    label: str
    reason: Optional[str] = None


class MakeupCandidates(BaseModel):
    items: List[MakeupCandidate]
    emptyReason: Optional[str] = None


class OneOffPayrollSheet(BaseModel):
    month: str
    status: Optional[str] = None          # the owner only: unpaid | due | settled | overpaid


class OneOffPerson(BaseModel):
    id: str
    name: Optional[str] = None
    role: Optional[str] = None


class OneOffDecisionItem(BaseModel):
    lessonId: str
    groupId: str
    groupName: str
    lessonDate: str
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    purpose: str
    title: str
    teacher: Optional[OneOffPerson] = None
    createdBy: Optional[OneOffPerson] = None
    studentsPresent: int
    studentsTotal: int
    payrollSheet: Optional[OneOffPayrollSheet] = None


class OneOffDecisions(BaseModel):
    total: int
    items: List[OneOffDecisionItem]


class MoneyDecisionRequest(BaseModel):
    decision: Literal["pay", "skip"]


class MoneyDecisionOut(BaseModel):
    lessonId: str
    money: str
    payrollSheet: Optional[OneOffPayrollSheet] = None


def _svc(db: Session):
    from .service import AuthService  # lazy: router.py imports this module while it is being imported itself

    return AuthService(db)


def _purposes(db: Session):
    from .one_off_purpose import OneOffPurpose

    return OneOffPurpose(_svc(db))


@router.get("/org/groups/{group_id}/makeup-candidates", response_model=MakeupCandidates)
def makeup_candidates(group_id: str, user: dict = Depends(require_org_member), db: Session = Depends(get_db)):
    """Отменённые уроки группы, которые можно отработать (преподаватель — только своей группы, без причин отмены)."""
    if not user.get("org_id"):
        raise HTTPException(403, "Нет организации")
    manager = is_org_manager(user)
    if not (manager or user.get("org_role") == "teacher"):
        raise HTTPException(403, "Нет доступа к этой группе")
    try:
        group = _svc(db)._require_group_access(user["org_id"], group_id, actor_id=user["user_id"], is_org_admin=manager)
    except ValueError as exc:
        raise not_found("GROUP_NOT_FOUND", str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return _purposes(db).candidates(group, manager=manager)


@router.get("/org/one-off-decisions", response_model=OneOffDecisions)
def one_off_decisions(user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """«Ждут решения»: проведённые дополнительные уроки с оплатой «прошу оплатить» и пустые отработки (только owner / admin)."""
    return _purposes(db).pending(user["org_id"], owner=user.get("org_role") == "owner")


@router.post("/org/one-off-lessons/{lesson_id}/money-decision", response_model=MoneyDecisionOut)
def decide_one_off_money(lesson_id: str, request: MoneyDecisionRequest, user: dict = Depends(_manager), db: Session = Depends(get_db)) -> Dict[str, Any]:
    """«Оплатить» / «Не оплачивать»: один раз, только проведённый урок (повтор — 409 ``ALREADY_DECIDED``)."""
    try:
        return _purposes(db).decide(user["org_id"], user["user_id"], user["org_role"], lesson_id, request.decision, owner=user.get("org_role") == "owner")
    except BaseException:
        db.rollback()
        raise
