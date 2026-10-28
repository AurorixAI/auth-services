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
    LessonExamBinding,
    LessonAccessOut,
    LessonMoveCheckOut,
    LessonMoveRequest,
    LessonRestoreOut,
    LessonRestoreRequest,
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


def _mover(user: dict = Depends(_member)) -> dict:
    """The move of a lesson (P1.1): an owner / admin of this centre or a teacher; whether the lesson is HIS is decided by the service, which holds the schedule."""
    if not (is_org_manager(user) or user.get("org_role") == "teacher"):
        raise HTTPException(403, err(403, "NOT_LESSON_TEACHER").message)
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
    status: Literal["pending", "all"] = Query("all"), user: dict = Depends(_member), db: Session = Depends(get_db)
):
    """Мои запросы и уроки, где меня назначили заменяющим.  W1 round 2: only a teacher leads lessons, so for an owner / admin everything is empty (they ask nothing and are nobody's substitute)."""
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
    return _subs(db).decide_bulk(user["org_id"], user["user_id"], user["org_role"], request)


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
    return _subs(db).decide(user["org_id"], user["user_id"], user["org_role"], request_id, request)


# ------------------------------------------------------------------------------ #124: the lesson sheet (no request needed)
@router.post("/org/groups/{group_id}/lesson-move", response_model=DecideOut)
def move_lesson_directly(group_id: str, request: LessonMoveRequest, user: dict = Depends(_mover), db: Session = Depends(get_db)):
    """Администратор - или преподаватель, который ведёт этот урок (не позднее чем за 60 минут до начала) - переносит будущий урок прямо из листа урока: правила и
    проверки решения «Перенести урок»; открытый запрос урока решается тем же путём."""
    return _subs(db).move_lesson(user["org_id"], user["user_id"], user["org_role"], group_id, request)


@router.get("/org/groups/{group_id}/lesson-move-check", response_model=LessonMoveCheckOut)
def lesson_move_check(
    group_id: str,
    lessonId: str = Query(..., min_length=1, max_length=100),
    lessonDate: date = Query(...),
    newDate: date = Query(...),
    startTime: str = Query(..., pattern=r"^\d{2}:\d{2}$"),
    endTime: str = Query(..., pattern=r"^\d{2}:\d{2}$"),
    user: dict = Depends(_mover),
    db: Session = Depends(get_db),
):
    """Живая проверка переноса урока без запроса: пересечения учителя нового урока и группы (преподаватель - только для своего урока)."""
    return _subs(db).lesson_move_check(user["org_id"], user["user_id"], user["org_role"], group_id, lessonId, lessonDate, newDate, startTime, endTime)


@router.get("/org/groups/{group_id}/lesson-exam-binding", response_model=LessonExamBinding)
def lesson_exam_binding(
    group_id: str,
    lessonId: str = Query(..., min_length=1, max_length=100),
    lessonDate: date = Query(...),
    user: dict = Depends(_mover),
    db: Session = Depends(get_db),
):
    """Предпроверка «Отменить урок» для листа урока (экзамен, привязанный к уроку): `canCancel=false` — активный экзамен; тот же ответ, что `examBinding` диалога «Замены»."""
    return _subs(db).lesson_exam_binding(user["org_id"], group_id, lessonId, lessonDate, user["user_id"], user["org_role"])


@router.post("/org/groups/{group_id}/lessons/restore", response_model=LessonRestoreOut)
def restore_cancelled_lesson(group_id: str, request: LessonRestoreRequest, user: dict = Depends(_manager), db: Session = Depends(get_db)):
    """W1 S5 «Вернуть отменённый урок» (owner / admin): только будущий урок, с причиной; перенесённый оригинал и урок выходного дня не возвращаются (коды ``RESTORE_*``)."""
    from .lesson_substitutions import Substitutions

    reason = Substitutions._reason(request, 3, 500)
    return _subs(db).restore_lesson(user["org_id"], user["user_id"], user["org_role"], group_id, request.lessonId, request.lessonDate, reason)


# ------------------------------------------------------------------------------------ exam-service (D-1)
@router.get("/org/groups/{group_id}/lesson-access", response_model=LessonAccessOut)
def group_lesson_access(group_id: str, user: dict = Depends(_member), db: Session = Depends(get_db)):
    """Для exam-service: заменяющему отдаётся только группа (название, класс) и ЕГО уроки замены; никакого состава и оплат."""
    from .lesson_substitutions import lesson_access

    return lesson_access(_svc(db), user["org_id"], user["user_id"], group_id)
