"""
ALGO - LOGIC-A endpoints (pack «деньги»): opening balance, pause, leave / archive with money, lesson type of a group.

Mounted by ``payments_router`` (so under ``/api/v1/auth``).  The routes and their schemas are the FROZEN contract of the
pack (``artifacts/partner_doc_check/logicA_contract.json``, ``tests/fixtures/logicA``): a change needs a change of the spec
(``logicA_spec.md``) and of the schemas first.  Guards: opening balance / leave settlement / price - ``require_payments_access``;
pause, leave, lesson type - ``require_org_admin`` (the sums inside the answer only with access to money).
"""
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from src.core.database import get_db

from .leave_service import LeaveService
from .lesson_types import LessonTypeService
from .logica_service import OpeningService, PauseService
from .payments_service import PaymentError
from .dependencies import has_payments_access, require_org_admin, require_payments_access
from .payments_schemas import (
    GroupLessonTypeChanged,
    GroupLessonTypes,
    LeavePreview,
    LeaveRequest,
    LeaveResult,
    LessonTypePut,
    LogicAError,
    OpeningOut,
    OpeningPut,
    OpeningSaved,
    OpeningVoided,
    PauseCreate,
    PauseOut,
    PausePatch,
    PauseSaved,
    PauseVoid,
    PauseVoided,
    RecordVoid,
)

router = APIRouter(tags=["LogicA"])  # the prefix /auth comes from payments_router

_ERRORS = {403: {"model": LogicAError}, 404: {"model": LogicAError}, 409: {"model": LogicAError}, 422: {"model": LogicAError}}


def _http(exc: PaymentError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


def _role(user: dict) -> str:
    return user.get("org_role") or "admin"


# ---- opening balance ------------------------------------------------------------------------------------


@router.get("/org/memberships/{membership_id}/opening-balance", response_model=Optional[OpeningOut], responses=_ERRORS)
def get_opening(membership_id: UUID, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Остаток на начало: живая запись членства или null."""
    try:
        return OpeningService(db).get(user["org_id"], membership_id)
    except PaymentError as exc:
        raise _http(exc)


@router.put("/org/memberships/{membership_id}/opening-balance", response_model=OpeningSaved, responses=_ERRORS)
def put_opening(
    membership_id: UUID,
    request: OpeningPut,
    dry_run: bool = Query(False, description="Только посчитать последствия, ничего не писать"),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Создать / заменить остаток на начало (все поля пустые = отменить живой)."""
    try:
        return OpeningService(db).put(user["org_id"], membership_id, user["user_id"], request, dry_run=dry_run, actor_role=_role(user))
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/opening-balances/{opening_id}/void", response_model=OpeningVoided, responses=_ERRORS)
def void_opening(
    opening_id: UUID, request: RecordVoid, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)
):
    try:
        return OpeningService(db).void(user["org_id"], opening_id, user["user_id"], request.reason, actor_role=_role(user))
    except PaymentError as exc:
        raise _http(exc)


# ---- pause ----------------------------------------------------------------------------------------------


@router.get("/org/memberships/{membership_id}/pauses", response_model=list[PauseOut], responses=_ERRORS)
def list_pauses(membership_id: UUID, user: dict = Depends(require_org_admin), db: Session = Depends(get_db)):
    try:
        return PauseService(db).list(user["org_id"], membership_id)
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/memberships/{membership_id}/pauses", response_model=PauseSaved, status_code=201, responses=_ERRORS)
def create_pause(
    membership_id: UUID,
    request: PauseCreate,
    dry_run: bool = Query(False),
    idempotency_key: Optional[UUID] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Пауза: дни паузы не начисляются, место сохраняется. Те же даты повторно (двойной клик) - ничего нового."""
    try:
        return PauseService(db).create(
            user["org_id"], membership_id, user["user_id"], request, dry_run=dry_run, money=has_payments_access(user, db),
            owner=user.get("org_role") == "owner", actor_role=_role(user))
    except PaymentError as exc:
        raise _http(exc)


@router.patch("/org/pauses/{pause_id}", response_model=PauseSaved, responses=_ERRORS)
def patch_pause(
    pause_id: UUID,
    request: PausePatch,
    dry_run: bool = Query(False),
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    try:
        return PauseService(db).patch(
            user["org_id"], pause_id, user["user_id"], request, dry_run=dry_run, money=has_payments_access(user, db),
            owner=user.get("org_role") == "owner", actor_role=_role(user))
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/pauses/{pause_id}/void", response_model=PauseVoided, responses=_ERRORS)
def void_pause(pause_id: UUID, request: PauseVoid, user: dict = Depends(require_org_admin), db: Session = Depends(get_db)):
    try:
        return PauseService(db).void(user["org_id"], pause_id, user["user_id"], request.reason, money=has_payments_access(user, db),
                                     actor_role=_role(user))
    except PaymentError as exc:
        raise _http(exc)


def _not_ready():
    raise HTTPException(501, {"code": "NOT_IMPLEMENTED", "message": "LOGIC-A: not implemented yet"})


# ---- leave / archive with money -------------------------------------------------------------------------


@router.get("/org/groups/{group_id}/students/{student_id}/leave-preview", response_model=LeavePreview, responses=_ERRORS)
def leave_preview(group_id: UUID, student_id: UUID, user: dict = Depends(require_org_admin), db: Session = Depends(get_db)):
    """Что будет с деньгами ученика, если убрать его из группы сейчас: долг / аванс / возвратимое по этому членству."""
    try:
        return LeaveService(db).preview_group(user["org_id"], group_id, student_id, money=has_payments_access(user, db))
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/groups/{group_id}/students/{student_id}/leave", response_model=LeaveResult, responses=_ERRORS)
def leave_group(
    group_id: UUID,
    student_id: UUID,
    request: Optional[LeaveRequest] = None,
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Убрать из группы + выбранное с деньгами (``keep`` / ``writeoff`` / ``refund``) в одной транзакции."""
    try:
        return LeaveService(db).leave(user["org_id"], str(group_id), str(student_id), user["user_id"], _role(user), request,
                                      money=has_payments_access(user, db))
    except PaymentError as exc:
        raise _http(exc)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/org/students/{student_id}/archive-preview", response_model=LeavePreview, responses=_ERRORS)
def archive_preview(student_id: UUID, user: dict = Depends(require_org_admin), db: Session = Depends(get_db)):
    """Что будет с деньгами ученика по каждой его группе, если отправить его в архив сейчас."""
    try:
        return LeaveService(db).preview_student(user["org_id"], student_id, money=has_payments_access(user, db))
    except PaymentError as exc:
        raise _http(exc)


# ---- lesson type of a group -----------------------------------------------------------------------------


@router.get("/org/groups/{group_id}/lesson-types", response_model=GroupLessonTypes, responses=_ERRORS)
def get_lesson_types(group_id: UUID, user: dict = Depends(require_org_admin), db: Session = Depends(get_db)):
    try:
        return LessonTypeService(db).get(user["org_id"], group_id)
    except PaymentError as exc:
        raise _http(exc)


@router.put("/org/groups/{group_id}/lesson-type", response_model=GroupLessonTypeChanged, responses=_ERRORS)
def put_lesson_type(
    group_id: UUID,
    request: LessonTypePut,
    dry_run: bool = Query(False),
    user: dict = Depends(require_org_admin),
    db: Session = Depends(get_db),
):
    """Тип занятий группы с даты (по умолчанию сегодня; не раньше начала прошлого месяца). Список ведомостей — только владельцу."""
    try:
        return LessonTypeService(db).put(user["org_id"], group_id, user["user_id"], request, dry_run=dry_run,
                                         owner=user.get("org_role") == "owner", actor_role=_role(user))
    except PaymentError as exc:
        raise _http(exc)
