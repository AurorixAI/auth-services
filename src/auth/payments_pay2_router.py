"""
ALGO - PAY-2 endpoints: «деньги покупают дни» (design ``artifacts/partner_doc_check/pay2_tech_design.md`` §4).

Mounted by ``payments_router`` (so under ``/api/v1/auth``).  The routes and their schemas are the FROZEN contract of
PAY-2 (``tests/fixtures/pay2``, ``artifacts/partner_doc_check/pay2_contract.json``): a change needs a change of the
design and of the schemas first.  Same guard as every money endpoint: ``require_payments_access``.

Until the ledger service is wired (stream B) a handler answers 501.
"""
from datetime import date
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from src.core.database import get_db

from .dependencies import require_payments_access
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


def _not_ready():
    raise HTTPException(501, {"code": "NOT_IMPLEMENTED", "message": "PAY-2: ещё не подключено"})


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
    _not_ready()


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
    """Списать долг (льгота, болезнь, договорённость): ``amount`` ≤ текущего долга или ``all: true``; причина 3..500."""
    _not_ready()


@router.post("/org/writeoffs/{writeoff_id}/void", response_model=WriteoffVoided, responses=_ERRORS)
def void_writeoff(
    writeoff_id: UUID,
    request: RecordVoid,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Отменить списание (долг возвращается); причина 3..500."""
    _not_ready()


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
    _not_ready()


@router.post("/org/refunds/{refund_id}/void", response_model=RefundVoided, responses=_ERRORS)
def void_refund(
    refund_id: UUID,
    request: RecordVoid,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Отменить возврат (аванс возвращается); причина 3..500."""
    _not_ready()


@router.get("/org/memberships/{membership_id}/statement", response_model=StatementOut, responses=_ERRORS)
def membership_statement(
    membership_id: UUID,
    from_: Optional[date] = Query(None, alias="from"),
    to: Optional[date] = None,
    include_voided: bool = False,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Выписка ученика в группе: начисления, оплаты (и куда пошли), списания, возвраты, переносы; бегущий итог."""
    _not_ready()


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
    _not_ready()


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
    """«Поступления центра»: сколько пришло, куда ушло, списано, возвращено; по способам / группам / преподавателям;
    позиция «долг / аванс» на сегодня; сверка. Диапазон ≤ 366 дней, ``group_by=day`` — ≤ 92."""
    _not_ready()


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
    """Раскрытие цифры отчёта до конкретных оплат / списаний / возвратов."""
    _not_ready()


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
    _not_ready()
