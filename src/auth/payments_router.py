"""
ALGO - Payments router (N13 P2).

Mounted under /api/v1/auth.  Every handler is tenant-scoped by the org of the
token and guarded by ``require_payments_access`` (owner always; admin only when
the owner switched ``admin_can_manage`` on; teacher never).  The settings
endpoints are the owner's alone.  Money is returned by nothing else.
"""
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.core.database import get_db

from .dependencies import require_payments_access, require_payments_settings
from .payments_schemas import (
    FeeUpdate,
    MembershipPaymentRow,
    PaymentCreate,
    PaymentCreated,
    PaymentOut,
    PaymentReplace,
    PaymentSettings,
    PaymentSettingsUpdate,
    PaymentsSummary,
    PaymentStatusPage,
    PaymentVoid,
    PaymentVoided,
)
from .payments_service import PaymentError, PaymentService

router = APIRouter(prefix="/auth", tags=["Payments"])


def _http(exc: PaymentError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


# ---- settings (owner) -------------------------------------------------------


@router.get("/org/settings/payments", response_model=PaymentSettings)
def get_payment_settings(user: dict = Depends(require_payments_settings), db: Session = Depends(get_db)):
    """Режим периода оплаты, пороги «скоро», право администратора."""
    return PaymentService(db).get_settings(user["org_id"])


@router.put("/org/settings/payments", response_model=PaymentSettings)
def update_payment_settings(
    request: PaymentSettingsUpdate,
    user: dict = Depends(require_payments_settings),
    db: Session = Depends(get_db),
):
    """Изменить настройки оплат (поля, которых нет в теле, не меняются)."""
    try:
        return PaymentService(db).update_settings(user["org_id"], user["user_id"], request)
    except PaymentError as exc:
        raise _http(exc)


# ---- status -----------------------------------------------------------------


@router.get("/org/payments/summary", response_model=PaymentsSummary)
def payments_summary(user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Счётчики для меню и баннера."""
    return PaymentService(db).summary(user["org_id"])


@router.get("/org/payments/status", response_model=PaymentStatusPage)
def payments_status(
    bucket: Literal["due", "expiring", "none", "active", "all"] = "all",
    group_id: Optional[UUID] = None,
    teacher_id: Optional[UUID] = None,
    search: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Членства со статусом оплаты (по вкладкам), с точным total."""
    return PaymentService(db).status_page(
        user["org_id"], bucket=bucket, group_id=group_id, teacher_id=teacher_id,
        search=search, limit=limit, offset=offset,
    )


@router.get("/org/students/{student_id}/memberships", response_model=list[MembershipPaymentRow])
def student_memberships(
    student_id: UUID, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)
):
    """Группы ученика с оплатой в месяц и статусом."""
    try:
        return PaymentService(db).student_memberships(user["org_id"], student_id)
    except PaymentError as exc:
        raise _http(exc)


@router.put("/org/memberships/{membership_id}/fee", response_model=MembershipPaymentRow)
def set_membership_fee(
    membership_id: UUID,
    request: FeeUpdate,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Задать «оплату в месяц» членства (null — сбросить)."""
    try:
        return PaymentService(db).set_fee(
            user["org_id"], membership_id, user["user_id"], request.monthly_fee
        )
    except PaymentError as exc:
        raise _http(exc)


# ---- payments ---------------------------------------------------------------


@router.get("/org/memberships/{membership_id}/payments", response_model=list[PaymentOut])
def list_payments(
    membership_id: UUID,
    include_voided: bool = True,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """История оплат членства, новые сверху."""
    try:
        return PaymentService(db).list_payments(user["org_id"], membership_id, include_voided)
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/memberships/{membership_id}/payments", response_model=PaymentCreated, status_code=201)
def create_payment(
    membership_id: UUID,
    request: PaymentCreate,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Внести оплату; значения по умолчанию — по настройкам центра."""
    try:
        return PaymentService(db).create_payment(
            user["org_id"], membership_id, user["user_id"], request
        )
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/payments/{payment_id}/replace", response_model=PaymentCreated, status_code=201)
def replace_payment(
    payment_id: UUID,
    request: PaymentReplace,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Исправить оплату: старая отменяется, новая создаётся — одной транзакцией."""
    try:
        return PaymentService(db).replace_payment(
            user["org_id"], payment_id, user["user_id"], request, request.reason
        )
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/payments/{payment_id}/void", response_model=PaymentVoided)
def void_payment(
    payment_id: UUID,
    request: PaymentVoid,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Отменить оплату (удаления нет); причина 3..500 символов."""
    try:
        return PaymentService(db).void_payment(
            user["org_id"], payment_id, user["user_id"], request.reason
        )
    except PaymentError as exc:
        raise _http(exc)
