"""
ALGO - PAY-2 endpoints: «деньги покупают дни» (design ``artifacts/partner_doc_check/pay2_tech_design.md`` §4).

Mounted by ``payments_router`` (so under ``/api/v1/auth``).  The routes and their schemas are the FROZEN contract of
PAY-2 (``tests/fixtures/pay2``, ``artifacts/partner_doc_check/pay2_contract.json``): a change needs a change of the
design and of the schemas first.  Same guard as every money endpoint: ``require_payments_access``.

"""
from datetime import date
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from src.core.database import get_db

from .dependencies import require_payments_access
from . import xlsx_style
from .payments_reports import PaymentsReports
from .payments_service import PaymentError, PaymentService
from .payments_schemas import (
    BalancesPage,
    MembershipPaymentRow,
    PayError,
    PaymentPreview,
    ReceiptItems,
    ReceiptsReport,
    RecordVoid,
    RefundCreate,
    RefundCreated,
    RefundVoided,
    StatementOut,
    WriteoffCreate,
    WriteoffCreated,
    WriteoffVoided,
)

router = APIRouter(tags=["Payments"])  # the prefix /auth comes from payments_router

_ERRORS = {403: {"model": PayError}, 404: {"model": PayError}, 409: {"model": PayError}, 422: {"model": PayError}}


def _http(exc: PaymentError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


def _role(user: dict) -> str:
    return user.get("org_role") or "admin"


@router.get(
    "/org/memberships/{membership_id}/payments/preview", response_model=PaymentPreview, responses=_ERRORS
)
def payment_preview(
    membership_id: UUID,
    amount: Optional[int] = Query(None, description="Сумма оплаты; без неё — только блок due"),
    paid_on: Optional[date] = Query(None, description="Дата оплаты; по умолчанию сегодня (бизнес-дата)"),
    replace_payment_id: Optional[UUID] = Query(None, description="Исправляемая оплата: исключается из расчёта"),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """«Что закроет»: долг и текущий период сегодня + куда пойдёт введённая сумма. Ничего не записывает."""
    try:
        return PaymentService(db).preview(
            user["org_id"], membership_id, amount=amount, paid_on=paid_on, replace_payment_id=replace_payment_id)
    except PaymentError as exc:
        raise _http(exc)


@router.post(
    "/org/memberships/{membership_id}/writeoffs", response_model=WriteoffCreated, status_code=201,
    responses=_ERRORS,
)
def create_writeoff(
    membership_id: UUID,
    request: WriteoffCreate,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Простить долг (льгота, болезнь, договорённость): ``amount`` ≤ текущего долга или ``all: true``; причина 3..500."""
    try:
        return PaymentService(db).create_writeoff(
            user["org_id"], membership_id, user["user_id"], amount=request.amount, whole=request.all_debt,
            reason=request.reason, effective_on=request.effective_on, actor_role=_role(user))
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/writeoffs/{writeoff_id}/void", response_model=WriteoffVoided, responses=_ERRORS)
def void_writeoff(
    writeoff_id: UUID,
    request: RecordVoid,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Отменить прощение долга (долг возвращается); причина 3..500."""
    try:
        return PaymentService(db).void_writeoff(user["org_id"], writeoff_id, user["user_id"], request.reason,
                                                actor_role=_role(user))
    except PaymentError as exc:
        raise _http(exc)


@router.post(
    "/org/memberships/{membership_id}/refunds", response_model=RefundCreated, status_code=201, responses=_ERRORS
)
def create_refund(
    membership_id: UUID,
    request: RefundCreate,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Вернуть аванс: ``amount`` ≤ возвратимого, способ обязателен. Это запись возврата, оплата не удаляется."""
    try:
        return PaymentService(db).create_refund(
            user["org_id"], membership_id, user["user_id"], amount=request.amount, method=request.method,
            reason=request.reason, note=request.note, effective_on=request.effective_on, actor_role=_role(user))
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/refunds/{refund_id}/void", response_model=RefundVoided, responses=_ERRORS)
def void_refund(
    refund_id: UUID,
    request: RecordVoid,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Отменить возврат (аванс возвращается); причина 3..500."""
    try:
        return PaymentService(db).void_refund(user["org_id"], refund_id, user["user_id"], request.reason,
                                              actor_role=_role(user))
    except PaymentError as exc:
        raise _http(exc)


@router.get("/org/memberships/{membership_id}/statement", response_model=StatementOut, responses=_ERRORS)
def membership_statement(
    membership_id: UUID,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    include_voided: bool = False,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Выписка ученика в группе: стоимость периодов, оплаты (и куда пошли), прощённые долги, возвраты, переносы; бегущий итог."""
    try:
        return PaymentsReports(db).statement(
            user["org_id"], membership_id, date_from=from_, date_to=to, include_voided=include_voided)
    except PaymentError as exc:
        raise _http(exc)


@router.get(
    "/org/memberships/{membership_id}/statement/export",
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {},
                "text/csv": {},
            },
            "description": "Файл выписки",
        },
        **_ERRORS,
    },
)
def membership_statement_export(
    membership_id: UUID,
    format: Literal["xlsx", "csv"] = "xlsx",
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    include_voided: bool = False,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
) -> Response:
    """Выписка файлом (xlsx / csv)."""
    try:
        data, content_type, filename = PaymentsReports(db).statement_file(
            user["org_id"], membership_id, fmt=format, date_from=from_, date_to=to, include_voided=include_voided)
    except PaymentError as exc:
        raise _http(exc)
    return Response(
        content=data, media_type=content_type,
        headers=xlsx_style.attachment_headers(filename))


@router.get("/org/payments/receipts", response_model=ReceiptsReport, responses=_ERRORS)
def receipts_report(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    group_by: Literal["day", "month"] = "day",
    method: Optional[str] = None,
    group_id: Optional[UUID] = None,
    teacher_id: Optional[UUID] = None,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """«Поступления центра»: сколько пришло, куда ушло, прощено, возвращено; по способам / группам / преподавателям;
    позиция «долг / аванс» на сегодня; сверка. Диапазон ≤ 366 дней, ``group_by=day`` — ≤ 92."""
    try:
        return PaymentsReports(db).receipts_report(
            user["org_id"], from_, to, group_by=group_by, method=method, group_id=group_id, teacher_id=teacher_id)
    except PaymentError as exc:
        raise _http(exc)


@router.get("/org/payments/receipts/items", response_model=ReceiptItems, responses=_ERRORS)
def receipts_items(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    column: Literal["received", "to_past_debt", "to_current", "to_advance", "written_off", "refunded"] = "received",
    bucket: Optional[str] = Query(None, description="День YYYY-MM-DD или месяц YYYY-MM из series"),
    method: Optional[str] = None,
    group_id: Optional[UUID] = None,
    teacher_id: Optional[UUID] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Раскрытие цифры отчёта до конкретных оплат / прощённых долгов / возвратов."""
    try:
        return PaymentsReports(db).receipts_items(
            user["org_id"], from_, to, column=column, bucket=bucket, method=method, group_id=group_id,
            teacher_id=teacher_id, limit=limit, offset=offset)
    except PaymentError as exc:
        raise _http(exc)


@router.get("/org/payments/balances", response_model=BalancesPage, responses=_ERRORS)
def payments_balances(
    kind: Literal["debt", "advance"] = "debt",
    group_id: Optional[UUID] = None,
    teacher_id: Optional[UUID] = None,
    search: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Раскрытие «позиции»: членства с долгом / авансом (в том числе ушедших и архивных)."""
    try:
        return PaymentsReports(db).balances(
            user["org_id"], kind=kind, group_id=group_id, teacher_id=teacher_id, search=search, limit=limit, offset=offset)
    except PaymentError as exc:
        raise _http(exc)
