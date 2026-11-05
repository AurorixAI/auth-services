"""#154 endpoints of the purpose of one-off lessons (``ONE_OFF_PURPOSE`` in ``one_off_purpose.py``).  Mounted into ``ops_router``.

* ``GET /org/groups/{id}/makeup-candidates`` - the cancelled lessons that can be made up (the teacher of the group, an owner / admin);
* ``GET /org/one-off-decisions`` and ``POST /org/one-off-lessons/{id}/money-decision`` - what waits for «Оплатить / Не оплачивать» and the decision (an owner / admin of this centre only)."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.database import get_db

from .attendance_access import is_org_manager
from .dependencies import require_org_member, require_payments_access
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
    studentsPaid: int = 0                              # Money-1 A3: for how many students «Оплатить» pays (rule 4.3: a mark «Не был» counts)
    moneyEstimate: Optional[Dict[str, Any]] = None     # Money-1 A3: the owner only: ``{"amount", "reason"}`` - what «Оплатить» pays the teacher (≈)
    periodClosed: bool = False                         # the month of the teacher is closed: «Оплатить» / «Не оплачивать» are refused (409 PERIOD_CLOSED) - the owner reopens the month first
    payrollSheet: Optional[OneOffPayrollSheet] = None


class OneOffDecisions(BaseModel):
    total: int
    items: List[OneOffDecisionItem]


class OneOffDecidedItem(OneOffDecisionItem):
    """R3 #28: a decided additional lesson (the list of «Решённые»)."""

    money: str                                         # paid | none
    decidedAt: Optional[str] = None
    decidedBy: Optional[OneOffPerson] = None
    canUndo: bool = True                               # false: the month of the teacher is closed - reopen it first


class OneOffDecided(BaseModel):
    total: int
    items: List[OneOffDecidedItem]


class MoneyDecisionRequest(BaseModel):
    decision: Literal["pay", "skip", "undo"]       # Money-1 A3: «undo» takes the decision back (back to «ждёт решения») while the month of the teacher is open


class MoneyDecisionOut(BaseModel):
    lessonId: str
    money: str
    payrollSheet: Optional[OneOffPayrollSheet] = None
    month: Optional[str] = None                        # ``YYYY-MM`` of the lesson: the month whose pay the decision touches
    dryRun: bool = False                               # the preview before the confirmation: nothing was written
    moneyEffects: Optional[Dict[str, Any]] = None      # the block of every money action (``money_effects.py``): ``payroll[]`` = the saved sheet ``freshBefore -> freshAfter`` (the owner), ``text``


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
    return _purposes(db).candidates(group, manager=manager, actor=user["user_id"])


@router.get("/org/one-off-decisions/decided", response_model=OneOffDecided)
def one_off_decided(month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"), limit: int = Query(200, ge=1, le=500), user: dict = Depends(require_payments_access),
                    db: Session = Depends(get_db)):
    """«Решённые»: дополнительные уроки, по которым уже решено «Оплатить» / «Не оплачивать» (новые сверху; ``month`` — YYYY-MM); отсюда решение можно отменить, пока месяц преподавателя открыт.
    Владелец и администратор с правом на деньги; суммы ведомости видит только владелец."""
    return _purposes(db).decided(user["org_id"], owner=user.get("org_role") == "owner", month=month, limit=limit)


@router.get("/org/one-off-decisions", response_model=OneOffDecisions)
def one_off_decisions(user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """«Ждут решения»: проведённые дополнительные уроки с оплатой «прошу оплатить» (отработка не ждёт решения; очередь читают owner и любой admin, решает owner / admin с правом денег)."""
    return _purposes(db).pending(user["org_id"], owner=user.get("org_role") == "owner")


@router.post("/org/one-off-lessons/{lesson_id}/money-decision", response_model=MoneyDecisionOut)
def decide_one_off_money(lesson_id: str, request: MoneyDecisionRequest, dry_run: bool = Query(False, description="только последствия для ведомости, ничего не записывать"),
                         user: dict = Depends(_manager), db: Session = Depends(get_db)) -> Dict[str, Any]:
    """«Оплатить» / «Не оплачивать»: один раз, только проведённый урок (повтор — 409 ``ALREADY_DECIDED``); ``undo`` — отменить решение, пока месяц преподавателя открыт."""
    try:
        return _purposes(db).decide(user["org_id"], user["user_id"], user["org_role"], lesson_id, request.decision, owner=user.get("org_role") == "owner", dry_run=dry_run)
    except BaseException:
        db.rollback()
        raise
