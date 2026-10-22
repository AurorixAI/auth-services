"""LOGIC-B2 endpoints: a teacher's request "I cannot teach" and the owner / admin decision (SPEC 4).  Mounted into ``ops_router``.

Rights (4.6): the request endpoints are for a TEACHER of the centre (owner / admin get 403 ``MANAGER_ASSIGNS_DIRECTLY``); list / summary / candidates / move-check / decide are
for the owner / admin of this centre; the detail for a manager or the requester.  Static paths are registered before ``{id}``.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.core.database import get_db

from .attendance_access import is_org_manager
from .dependencies import require_org_member
from .ops_router import _manager
from .substitution_rules import err
from .substitution_schemas import (
    CandidatesOut,
    ColleagueOut,
    CreateSubstitutionOut,
    CreateSubstitutionRequest,
    DecideBulkOut,
    DecideBulkRequest,
    DecideOut,
    DecideRequest,
    LessonAccessOut,
    MineOut,
    MoveCheckOut,
    MyLessonsOut,
    SubstitutionDetailOut,
    SubstitutionItem,
    SubstitutionListOut,
    SubstitutionSummary,
    WithdrawBatchOut,
    WithdrawBatchRequest,
)

router = APIRouter(tags=["LOGIC-B2 substitutions"])


def _svc(db: Session):
    from .service import AuthService  # lazy: router.py imports this module while it is being imported itself

    return AuthService(db)


def _subs(db: Session):
    from .lesson_substitutions import Substitutions

    return Substitutions(_svc(db))


def _teacher(user: dict = Depends(require_org_member)) -> dict:
    """A teacher of this centre.  Owner / admin: 403 ``MANAGER_ASSIGNS_DIRECTLY``; a global role without a membership: 403 "Нет организации"."""
    if not user.get("org_id"):
        raise HTTPException(403, "Нет организации")
    if is_org_manager(user):
        raise err(403, "MANAGER_ASSIGNS_DIRECTLY")
    if user.get("org_role") != "teacher":
        raise HTTPException(403, "Недостаточно прав. Требуется: teacher")
    return user


def _member(user: dict = Depends(require_org_member)) -> dict:
    if not user.get("org_id"):
        raise HTTPException(403, "Нет организации")
    return user


# ------------------------------------------------------------------------------------------------ teacher
@router.get("/org/substitutions/my-lessons", response_model=MyLessonsOut)
def my_lessons(
    dateFrom: date = Query(...), dateTo: date = Query(...), user: dict = Depends(_teacher), db: Session = Depends(get_db)
):
    """Мои уроки периода: можно ли запросить замену (период ≤ 62 дней)."""
    return _subs(db).my_lessons(user["org_id"], user["user_id"], dateFrom, dateTo)


@router.post("/org/substitutions", response_model=CreateSubstitutionOut, status_code=201)
def create_substitution_requests(request: CreateSubstitutionRequest, user: dict = Depends(_teacher), db: Session = Depends(get_db)):
    """«Не смогу провести»: запрос на один или несколько своих будущих уроков."""
    return _subs(db).create(user["org_id"], user["user_id"], request)


@router.get("/org/substitutions/mine", response_model=MineOut)
def my_substitution_requests(
    status: Literal["pending", "all"] = Query("all"), user: dict = Depends(_teacher), db: Session = Depends(get_db)
):
    """Мои запросы и уроки, где меня назначили заменяющим."""
    return _subs(db).mine(user["org_id"], user["user_id"], status)


@router.get("/org/substitutions/colleagues", response_model=list[ColleagueOut])
def substitution_colleagues(user: dict = Depends(_member), db: Session = Depends(get_db)):
    """«Предложить коллегу»: активные сотрудники центра, которые могут вести уроки (кроме меня); id, имя, роль."""
    return _subs(db).colleagues(user["org_id"], user["user_id"])


@router.post("/org/substitutions/withdraw", response_model=WithdrawBatchOut)
def withdraw_substitution_requests(request: WithdrawBatchRequest, user: dict = Depends(_teacher), db: Session = Depends(get_db)):
    """Отозвать несколько запросов."""
    return _subs(db).withdraw_batch(user["org_id"], user["user_id"], request.ids)


# --------------------------------------------------------------------------------------- owner / admin
@router.post("/org/substitutions/decide-bulk", response_model=DecideBulkOut)
def decide_substitutions_bulk(request: DecideBulkRequest, user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """Массовое решение (substitute / cancel / reject); каждый запрос - своя транзакция."""
    return _subs(db).decide_bulk(user["org_id"], user["user_id"], user.get("org_role") or "admin", request)


@router.get("/org/substitutions/summary", response_model=SubstitutionSummary)
def substitutions_summary(user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """Счётчик бейджа меню «Замены» (только счёт, без записи)."""
    return _subs(db).summary(user["org_id"])


@router.get("/org/substitutions", response_model=SubstitutionListOut)
def list_substitutions(
    status: Literal["pending", "decided", "all"] = Query("pending"),
    groupId: Optional[str] = None,
    teacherId: Optional[str] = None,
    dateFrom: Optional[date] = None,
    dateTo: Optional[date] = None,
    urgent: Optional[bool] = None,
    search: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(_manager),
    db: Session = Depends(get_db),
):
    """Раздел «Замены»: запросы преподавателей."""
    return _subs(db).list(user["org_id"], status=status, group_id=groupId, teacher_id=teacherId, date_from=dateFrom, date_to=dateTo, urgent=urgent,
                          search=search, limit=limit, offset=offset)


@router.get("/org/substitutions/{request_id}", response_model=SubstitutionDetailOut)
def substitution_detail(request_id: str, user: dict = Depends(_member), db: Session = Depends(get_db)):
    """Запрос с цепочкой, журналом и привязкой экзамена (manager или проситель)."""
    return _subs(db).detail(user["org_id"], user["user_id"], is_org_manager(user), request_id)


@router.post("/org/substitutions/{request_id}/withdraw", response_model=SubstitutionItem)
def withdraw_substitution_request(request_id: str, user: dict = Depends(_teacher), db: Session = Depends(get_db)):
    """Отозвать свой запрос (только pending)."""
    return _subs(db).withdraw(user["org_id"], user["user_id"], request_id)


@router.get("/org/substitutions/{request_id}/candidates", response_model=CandidatesOut)
def substitution_candidates(request_id: str, user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """Кто может провести урок: предложенный первым, занятые отмечены."""
    return _subs(db).candidates(user["org_id"], user.get("org_role") == "owner", request_id)


@router.get("/org/substitutions/{request_id}/move-check", response_model=MoveCheckOut)
def substitution_move_check(
    request_id: str,
    newDate: date = Query(...),
    startTime: str = Query(..., pattern=r"^\d{2}:\d{2}$"),
    endTime: str = Query(..., pattern=r"^\d{2}:\d{2}$"),
    user: dict = Depends(_manager),
    db: Session = Depends(get_db),
):
    """Живая проверка переноса: пересечения учителя нового урока и группы."""
    return _subs(db).move_check(user["org_id"], request_id, newDate, startTime, endTime)


@router.post("/org/substitutions/{request_id}/decide", response_model=DecideOut)
def decide_substitution(request_id: str, request: DecideRequest, user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """Решение: назначить замену / перенести / отменить / отклонить."""
    return _subs(db).decide(user["org_id"], user["user_id"], user.get("org_role") or "admin", request_id, request)


# ------------------------------------------------------------------------------------ exam-service (D-1)
@router.get("/org/groups/{group_id}/lesson-access", response_model=LessonAccessOut)
def group_lesson_access(group_id: str, user: dict = Depends(_member), db: Session = Depends(get_db)):
    """Для exam-service: заменяющему отдаётся только группа (название, класс) и ЕГО уроки замены; никакого состава и оплат."""
    from .lesson_substitutions import lesson_access

    return lesson_access(_svc(db), user["org_id"], user["user_id"], group_id)
