"""
ALGO - Payments router (N13 P2).

Mounted under /api/v1/auth.  Every handler is tenant-scoped by the org of the
token and guarded by ``require_payments_access`` (owner always; admin only when
the owner switched ``admin_can_manage`` on; teacher never).  The settings
endpoints are the owner's alone.  Money is returned by nothing else - that
includes the amount hint (``quote``), the receipt and the cheque files (N13 P2.2).

ROADMAP #105 - the price of a group and the individual price of a student live here too (``pricing.py``): the same
guard, so a teacher never sees a price and an admin sees it only when the owner allowed money.
"""
from datetime import date
from typing import Annotated, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BeforeValidator, Field
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from src.core.database import get_db

from . import pricing
from .dependencies import require_payments_access, require_payments_settings
from .payments_files import MAX_BODY_BYTES, MAX_FILE_BYTES, file_headers
from .payments_schemas import (
    MAX_MONEY,
    GroupPriceChanged,
    GroupPricesOut,
    GroupPriceUpdate,
    MembershipPaymentRow,
    MembershipPriceOut,
    PaymentCreate,
    PaymentCreated,
    PaymentFileOut,
    PaymentOut,
    PaymentQuote,
    PaymentReplace,
    PaymentSettings,
    PaymentSettingsUpdate,
    PaymentsSummary,
    PaymentStatusPage,
    PaymentVoid,
    PaymentVoided,
    PriceOverrideChanged,
    PriceOverrideUpdate,
    ReceiptOut,
)
from .payments_service import PaymentError, PaymentService

router = APIRouter(prefix="/auth", tags=["Payments"])


def _http(exc: PaymentError | pricing.PriceError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


def _blank_to_none(value):
    """A form that leaves a field empty sends ``?valid_to=``: that is "not given", not an invalid date."""
    return None if isinstance(value, str) and not value.strip() else value


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
    """Группы ученика с ценой (``price``) и статусом оплаты."""
    try:
        return PaymentService(db).student_memberships(user["org_id"], student_id)
    except PaymentError as exc:
        raise _http(exc)


FEE_GONE = {
    "code": "FEE_ENDPOINT_GONE",
    "message": (
        "«Оплата в месяц» ученика больше не задаётся. Цена задаётся у группы: "
        "PUT /api/v1/auth/org/groups/{group_id}/price. Индивидуальная цена ученика — "
        "PUT /api/v1/auth/org/memberships/{membership_id}/price-override"
    ),
}


@router.put("/org/memberships/{membership_id}/fee", status_code=410, include_in_schema=False)
def set_membership_fee_gone(membership_id: UUID, user: dict = Depends(require_payments_access)):
    """Снято (ROADMAP #105): 410 с указанием новых путей. Права проверяются, как у всех денежных ручек."""
    raise HTTPException(410, FEE_GONE)


# ---- group price and individual price (ROADMAP #105) ------------------------------------------------


@router.get("/org/groups/{group_id}/prices", response_model=GroupPricesOut)
def group_prices(
    group_id: UUID, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)
):
    """Цена группы на сегодня (``current``, null — не задана) и история изменений, новые сверху."""
    try:
        return pricing.PricingService(db).group_prices(user["org_id"], group_id)
    except pricing.PriceError as exc:
        raise _http(exc)


@router.put("/org/groups/{group_id}/price", response_model=GroupPriceChanged)
def set_group_price(
    group_id: UUID,
    request: GroupPriceUpdate,
    dry_run: bool = Query(False, description="1 — только последствия (impact), ничего не записывать"),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Задать цену группы: сумма + «действует с» (по умолчанию сегодня; прошлая дата разрешена) + примечание.

    В ответе — новая история и ``impact``: сколько учеников возьмут цену (``students_following``), у скольких
    индивидуальная (``students_with_override``) и сколько сохранённых ведомостей зарплаты станут «данные изменились»
    (``stale_payroll_sheets``; их список ``stale_sheets`` — только владельцу). ``?dry_run=1`` — только ``impact``.
    Оплаты, уже внесённые, не меняются никогда."""
    try:
        return pricing.PricingService(db).set_group_price(
            user["org_id"], group_id, user["user_id"], amount=request.amount, valid_from=request.valid_from,
            note=request.note, dry_run=dry_run, owner_view=user.get("org_role") == "owner",
        )
    except pricing.PriceError as exc:
        raise _http(exc)


@router.get("/org/memberships/{membership_id}/price-override", response_model=MembershipPriceOut)
def get_price_override(
    membership_id: UUID, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)
):
    """Действующая цена членства сегодня и история индивидуальных цен."""
    try:
        return pricing.PricingService(db).override_history(user["org_id"], membership_id)
    except pricing.PriceError as exc:
        raise _http(exc)


@router.put("/org/memberships/{membership_id}/price-override", response_model=PriceOverrideChanged)
def set_price_override(
    membership_id: UUID,
    request: PriceOverrideUpdate,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """«Индивидуальная цена» (``amount`` + причина обязательна) или «Вернуть цену группы» (``amount`` null) — с даты
    ``valid_from`` (по умолчанию сегодня). Ответ — история и обновлённая строка членства (``row``)."""
    try:
        out = pricing.PricingService(db).set_override(
            user["org_id"], membership_id, user["user_id"], amount=request.amount,
            valid_from=request.valid_from, reason=request.reason,
        )
        out["row"] = PaymentService(db)._row(user["org_id"], membership_id)
        return out
    except (PaymentError, pricing.PriceError) as exc:
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


@router.get("/org/memberships/{membership_id}/payments/quote", response_model=PaymentQuote)
def payment_quote(
    membership_id: UUID,
    paid_on: date,
    valid_from: Annotated[Optional[date], BeforeValidator(_blank_to_none)] = None,
    valid_to: Annotated[Optional[date], BeforeValidator(_blank_to_none)] = None,
    lessons_count: Annotated[Optional[Annotated[int, Field(ge=1, le=120)]], BeforeValidator(_blank_to_none)] = None,
    monthly_fee: Annotated[Optional[Annotated[int, Field(ge=0, le=MAX_MONEY)]], BeforeValidator(_blank_to_none)] = None,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Подсказка периода и суммы для формы оплаты; ничего не записывает.

    Даты по умолчанию считаются тем же кодом, что при внесении оплаты; ошибки — те же 422.
    База — действующая цена ученика (цена группы или индивидуальная) на первый день оплачиваемого периода
    (``price`` в ответе). ``monthly_fee`` (необязательно) заменяет её только для этого расчёта, ничего не сохраняя;
    в ответе ``monthly_fee`` — та сумма, по которой считали."""
    try:
        return PaymentService(db).quote(
            user["org_id"], membership_id, paid_on=paid_on, valid_from=valid_from,
            valid_to=valid_to, lessons_count=lessons_count, monthly_fee=monthly_fee,
        )
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


# ---- receipt and cheques (N13 P2.2) -------------------------------------------


@router.get("/org/payments/{payment_id}/receipt", response_model=ReceiptOut)
def payment_receipt(
    payment_id: UUID, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)
):
    """Данные квитанции (у переноса оплаты квитанции нет — 409)."""
    try:
        return PaymentService(db).receipt(user["org_id"], payment_id)
    except PaymentError as exc:
        raise _http(exc)


@router.post(
    "/org/payments/{payment_id}/files",
    response_model=PaymentFileOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def upload_payment_file(
    payment_id: UUID,
    request: Request,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Приложить фото или скан чека (JPEG/PNG/PDF до 2 МБ, не более 3 файлов; не к аннулированной оплате)."""
    # ``file: UploadFile`` would make FastAPI buffer the whole body before this handler runs; the size
    # is checked here first (the access check above has already run as a dependency).
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        raise HTTPException(411, "Не указан размер файла")
    if declared > MAX_BODY_BYTES:
        raise HTTPException(413, "Файл слишком большой (макс. 2 МБ)")
    service = PaymentService(db)
    try:
        # A payment that cannot take a cheque (unknown, voided, a carry-over, already three) is refused
        # BEFORE the multipart body is parsed.  Blocking DB work runs in the thread pool.
        await run_in_threadpool(service.precheck_file_upload, user["org_id"], payment_id)
    except PaymentError as exc:
        raise _http(exc)
    form = await request.form()
    try:
        upload = form.get("file")
        if not isinstance(upload, StarletteUploadFile):
            raise HTTPException(422, "Передайте файл в поле file")
        # Never read more than one byte past the limit into memory.
        data = await upload.read(MAX_FILE_BYTES + 1)
        filename = upload.filename
    finally:
        await form.close()
    try:
        # The authoritative check (under the row lock) is in add_file.
        return await run_in_threadpool(
            service.add_file, user["org_id"], payment_id, user["user_id"], data, filename
        )
    except PaymentError as exc:
        raise _http(exc)


@router.get("/org/payments/{payment_id}/files/{file_id}")
def download_payment_file(
    payment_id: UUID,
    file_id: UUID,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Отдать чек (inline, без угадывания типа)."""
    try:
        stored = PaymentService(db).get_file(user["org_id"], payment_id, file_id)
    except PaymentError as exc:
        raise _http(exc)
    return Response(content=stored.data, media_type=stored.content_type, headers=file_headers(stored.filename))


@router.delete("/org/payments/{payment_id}/files/{file_id}")
def delete_payment_file(
    payment_id: UUID,
    file_id: UUID,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Удалить чек: владелец или тот, кто его загрузил."""
    try:
        PaymentService(db).delete_file(
            user["org_id"], payment_id, file_id, user["user_id"], is_owner=user.get("org_role") == "owner"
        )
    except PaymentError as exc:
        raise _http(exc)
    return {"ok": True}
