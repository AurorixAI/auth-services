"""
ALGO - OPENING-BULK endpoints: «Остатки на начало» (the table of every membership, the batch preview / apply, the state of the checklist, the
Excel round trip).

Mounted by ``payments_router`` (under ``/api/v1/auth``).  The routes and their schemas are the FROZEN contract of the pack
(``artifacts/partner_doc_check/openings_contract.json``, ``tests/fixtures/openings``).  Guard of every route: ``require_payments_access`` (the owner,
or an admin with the right to money; everybody else 403); everything is limited to the organisation of the caller (a foreign id = not found).
"""
from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy.orm import Session

from src.core.database import get_db

from . import xlsx_style
from .dependencies import require_payments_access
from .openings_schemas import (
    OpeningBatchApplied,
    OpeningBatchPreview,
    OpeningBatchPreviewRequest,
    OpeningBatchRequest,
    OpeningsError,
    OpeningsImportPreview,
    OpeningsList,
    OpeningsStateIn,
    OpeningsSummary,
)
from .openings_service import MAX_IMPORT_BYTES, OpeningsService
from .payments_service import PaymentError

router = APIRouter(tags=["Openings"])  # the prefix /auth comes from payments_router

_ERRORS = {403: {"model": OpeningsError}, 404: {"model": OpeningsError}, 409: {"model": OpeningsError}, 422: {"model": OpeningsError}}
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _http(exc: PaymentError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


def _role(user: dict) -> str:
    return user.get("org_role") or "admin"


@router.get("/org/opening-balances", response_model=OpeningsList, responses=_ERRORS)
def list_openings(
    group_id: Optional[UUID] = None,
    missing_only: bool = Query(False, description="Только членства без остатка (до начала учёта, без денег)"),
    q: Optional[str] = Query(None, max_length=100, description="Поиск: фамилия, имя, ID ученика"),
    include_left: bool = Query(False, description="Добавить ушедших до начала учёта (им доступен только долг)"),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Все активные членства центра по группам: что внесено, версия строки, можно ли править. Без пересчёта долгов."""
    try:
        return OpeningsService(db).list(user["org_id"], group_id=group_id, missing_only=missing_only, q=q, include_left=include_left)
    except PaymentError as exc:
        raise _http(exc)


@router.get("/org/opening-balances/summary", response_model=OpeningsSummary, responses=_ERRORS)
def openings_summary(user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Состояние запуска и счётчики без строк (баннер «Должников», чек-лист настроек)."""
    try:
        return OpeningsService(db).summary(user["org_id"])
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/opening-balances/batch/preview", response_model=OpeningBatchPreview, responses=_ERRORS)
def preview_batch(request: OpeningBatchPreviewRequest, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Сухой прогон пакета: ошибки по строкам, сводка по видам, эффект на долги, digest. Ничего не пишется."""
    try:
        return OpeningsService(db).preview(user["org_id"], request)
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/opening-balances/batch", response_model=OpeningBatchApplied, responses=_ERRORS)
def apply_batch(request: OpeningBatchRequest, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Применить пакет одной транзакцией (всё или ничего). Повтор того же idempotency_key — тот же ответ."""
    try:
        return OpeningsService(db).apply(user["org_id"], user["user_id"], _role(user), request)
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/opening-balances/state", response_model=OpeningsSummary, responses=_ERRORS)
def set_openings_state(request: OpeningsStateIn, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """«Готово» / «Не нужно»: состояние запуска остатков (``pending`` ставит только скрипт релиза)."""
    try:
        return OpeningsService(db).set_state(user["org_id"], user["user_id"], request.state)
    except PaymentError as exc:
        raise _http(exc)


@router.get(
    "/org/opening-balances/export",
    responses={200: {"content": {_XLSX: {}}, "description": "Файл остатков (лист «Остатки»)"}, **_ERRORS},
)
def export_openings(
    group_id: Optional[UUID] = None,
    missing_only: bool = False,
    q: Optional[str] = Query(None, max_length=100),
    include_left: bool = False,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
) -> Response:
    """Остатки файлом .xlsx, заполненным текущими значениями: первый лист «Инструкция», лист «Остатки» (заголовок, колонки импорта, справочные серые, «Итого», скрытые ID в конце); читается по названиям колонок."""
    try:
        data, filename = OpeningsService(db).export(user["org_id"], group_id=group_id, missing_only=missing_only, q=q, include_left=include_left)
    except PaymentError as exc:
        raise _http(exc)
    return Response(content=data, media_type=_XLSX, headers=xlsx_style.attachment_headers(filename))


@router.post("/org/opening-balances/import/preview", response_model=OpeningsImportPreview, responses=_ERRORS)
async def import_preview(file: UploadFile = File(...), user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Разобрать файл остатков (.xlsx, ≤ 5 МБ, ≤ 5 000 строк): что будет изменено, ошибки строк, готовые items для POST /batch. Ничего не пишется."""
    data = await file.read(MAX_IMPORT_BYTES + 1)
    try:
        return await run_in_threadpool(OpeningsService(db).import_preview, user["org_id"], data)
    except PaymentError as exc:
        raise _http(exc)
