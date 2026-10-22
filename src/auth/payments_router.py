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

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BeforeValidator, Field
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from src.core.database import get_db

from . import payments_group_prices, pricing
from .dependencies import require_payments_access, require_payments_settings
from .payments_files import MAX_BODY_BYTES, MAX_FILE_BYTES, file_headers
from .logica_router import router as logica_router
from .openings_router import router as openings_router
from .payments_income_router import router as income_router
from .payments_pay2_router import router as pay2_router
from .payments_schemas import (
    MAX_MONEY,
    GroupPriceChanged,
    GroupPriceList,
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
    PaymentSettingsChanged,
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
router.include_router(pay2_router)  # PAY-2: preview, write-off, refund, statement, receipts report, balances
router.include_router(income_router)  # INCOME-2: income overview + month collection, payments journal (+ Excel)
router.include_router(logica_router)  # LOGIC-A: opening balance, pause, leave with money, lesson type
router.include_router(openings_router)  # OPENING-BULK: the table of openings, batch preview / apply, state, Excel


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


@router.put("/org/settings/payments", response_model=PaymentSettingsChanged)
def update_payment_settings(
    request: PaymentSettingsUpdate,
    dry_run: bool = Query(False, description="1 — ничего не записывать, вернуть последствия (impact) новой даты начала учёта"),
    user: dict = Depends(require_payments_settings),
    db: Session = Depends(get_db),
):
    """Изменить настройки оплат (поля, которых нет в теле, не меняются). PAY-2: ``accounting_start_date`` — начало
    учёта оплат (только владелец); ``?dry_run=1`` — сколько членств получат новый долг и на какую сумму."""
    try:
        return PaymentService(db).update_settings(user["org_id"], user["user_id"], request, dry_run=dry_run)
    except PaymentError as exc:
        raise _http(exc)


# ---- status -----------------------------------------------------------------


@router.get("/org/payments/summary", response_model=PaymentsSummary)
def payments_summary(user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Счётчики для меню и баннера."""
    return PaymentService(db).summary(user["org_id"])


@router.get("/org/payments/status", response_model=PaymentStatusPage)
def payments_status(
    bucket: Literal["debt", "expiring", "active", "paused", "all", "due", "none"] = "all",
    group_id: Optional[UUID] = None,
    teacher_id: Optional[UUID] = None,
    search: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    sort: Optional[Literal["debt"]] = Query(None, description="debt — по убыванию долга (PAY-2)"),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Членства со статусом оплаты (по вкладкам), с точным total. PAY-2: вкладка ``debt`` — члены с долгом > 0
    (в том числе ушедшие и архивные), ``sort=debt`` — по убыванию долга. LOGIC-A (решение владельца 21.10): вкладки
    ``debt`` Должники / ``expiring`` Скоро платить / ``active`` Оплачено / ``paused`` На паузе (``due`` — старое имя ``debt``; ``none`` — старая
    вкладка, один релиз)."""
    return PaymentService(db).status_page(
        user["org_id"], bucket=bucket, group_id=group_id, teacher_id=teacher_id,
        search=search, limit=limit, offset=offset, sort=sort,
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
        "«Цена в месяц» ученика больше не задаётся. Цена задаётся у группы: "
        "PUT /api/v1/auth/org/groups/{group_id}/price. Индивидуальная цена ученика — "
        "PUT /api/v1/auth/org/memberships/{membership_id}/price-override"
    ),
}


@router.put("/org/memberships/{membership_id}/fee", status_code=410, include_in_schema=False)
def set_membership_fee_gone(membership_id: UUID, user: dict = Depends(require_payments_access)):
    """Снято (ROADMAP #105): 410 с указанием новых путей. Права проверяются, как у всех денежных ручек."""
    raise HTTPException(410, FEE_GONE)


# ---- group price and individual price (ROADMAP #105) ------------------------------------------------


@router.get("/org/group-prices", response_model=GroupPriceList)
def group_price_list(user: dict = Depends(require_payments_access), db: Session = Depends(get_db)):
    """Страница «Оплаты → Цены групп»: все НЕ архивные группы центра (архивные не показываются) — название, учитель,
    вид занятий (``kind``), цена на сегодня (``current``, null — не задана), ближайшая запланированная
    (``scheduled`` с ``row_id`` для отмены; ``scheduled_count`` — сколько их всего) и сколько активных учеников с
    индивидуальной ценой на сегодня. Четыре запроса на любое число групп. Только денежные роли."""
    return payments_group_prices.list_group_prices(db, user["org_id"])


@router.get("/org/groups/{group_id}/prices", response_model=GroupPricesOut)
def group_prices(
    group_id: UUID, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)
):
    """Цена группы на сегодня (``current``, null — не задана) и история изменений, новые сверху.

    След изменений (аудит денег) — в ``history[].changes``: все записи ЭТОЙ даты ``valid_from``, новые сверху:
    ``at``, ``actor_name``, ``old_amount`` (что действовало в этот день до записи; null — ничего), ``new_amount``,
    ``note``. Первая — запись, создавшая строку, остальные — её исправления. Только денежные роли."""
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
    LOGIC-A: ``impact.money`` - что изменится у денег (долг / аванс / «оплачено до»); цена задним числом, которая меняет начисление
    уже начавшегося периода у ученика с оплатами, - 409 ``PRICE_BACKDATE_WITH_PAYMENTS``.

    Каждая запись (новая, исправление той же даты) оставляет строку аудита (``group_audit``, ``group_price_set``) в той
    же транзакции — она видна в ``history[].changes``. Тот же запрос ещё раз (двойной клик: сумма, примечание и
    ``valid_from`` те же) ничего не меняет и вторую строку аудита не пишет."""
    try:
        return pricing.PricingService(db).set_group_price(
            user["org_id"], group_id, user["user_id"], amount=request.amount, valid_from=request.valid_from,
            note=request.note, dry_run=dry_run, owner_view=user.get("org_role") == "owner",
        )
    except pricing.PriceError as exc:
        raise _http(exc)


@router.delete("/org/groups/{group_id}/prices/{row_id}", response_model=GroupPricesOut)
def cancel_group_price(
    group_id: UUID, row_id: UUID, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)
):
    """Отменить ЗАПЛАНИРОВАННУЮ цену группы (``valid_from`` позже сегодняшнего дня): строка удаляется, в аудит
    (``group_audit``, ``group_price_cancelled``) пишется снимок удалённой строки — в той же транзакции. Ответ — как у GET
    ``/prices`` (отмена видна в ``cancelled``). Цена, которая уже действует: 409; чужая или неизвестная строка: 404."""
    try:
        return pricing.PricingService(db).cancel_group_price(user["org_id"], group_id, row_id, user["user_id"])
    except pricing.PriceError as exc:
        raise _http(exc)


@router.get("/org/memberships/{membership_id}/price-override", response_model=MembershipPriceOut)
def get_price_override(
    membership_id: UUID, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)
):
    """Действующая цена членства сегодня и история индивидуальных цен.

    След изменений — в ``history[].changes`` (записи этой даты ``valid_from``, новые сверху): ``at``, ``actor_name``,
    ``old_amount`` + ``old_source`` (действующая цена ученика в этот день до записи и откуда она: ``group`` /
    ``override`` / ``none``), ``new_amount`` (null — «вернули цену группы»), ``reason``. Только денежные роли."""
    try:
        return pricing.PricingService(db).override_history(user["org_id"], membership_id)
    except pricing.PriceError as exc:
        raise _http(exc)


@router.put("/org/memberships/{membership_id}/price-override", response_model=PriceOverrideChanged)
def set_price_override(
    membership_id: UUID,
    request: PriceOverrideUpdate,
    dry_run: bool = Query(False, description="1 — только последствия (money), ничего не записывать"),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """«Индивидуальная цена» (``amount`` + причина обязательна) или «Вернуть цену группы» (``amount`` null) — с даты
    ``valid_from`` (по умолчанию сегодня). Ответ — история и обновлённая строка членства (``row``).

    Каждая запись, включая «вернуть цену группы», оставляет строку аудита (``group_audit``, ``membership_price_set``) в
    той же транзакции — она видна в ``history[].changes``. Повтор того же запроса (сумма, причина, ``valid_from``)
    ничего не меняет и аудит не дублирует."""
    try:
        out = pricing.PricingService(db).set_override(
            user["org_id"], membership_id, user["user_id"], amount=request.amount,
            valid_from=request.valid_from, reason=request.reason, dry_run=dry_run,
            owner_view=user.get("org_role") == "owner",
        )
        out["row"] = PaymentService(db)._row(user["org_id"], membership_id)
        return out
    except (PaymentError, pricing.PriceError) as exc:
        raise _http(exc)


@router.delete("/org/memberships/{membership_id}/price-override/{row_id}", response_model=PriceOverrideChanged)
def cancel_price_override(
    membership_id: UUID, row_id: UUID, user: dict = Depends(require_payments_access), db: Session = Depends(get_db)
):
    """Отменить ЗАПЛАНИРОВАННУЮ индивидуальную цену (``valid_from`` позже сегодняшнего дня): строка удаляется, в аудит
    (``membership_price_cancelled``) пишется снимок — в той же транзакции. Ответ — как у PUT: история, ``cancelled`` и
    обновлённая строка членства (``row``). Уже действующая: 409; чужая или неизвестная строка: 404."""
    try:
        out = pricing.PricingService(db).cancel_override(user["org_id"], membership_id, row_id, user["user_id"])
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
    idempotency_key: Optional[UUID] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Внести оплату. PAY-2: только сумма, дата, способ, примечание — период и распределение считает система
    (``allocation`` в ответе). ``Idempotency-Key`` защищает от двойного клика."""
    try:
        return PaymentService(db).create_payment(
            user["org_id"], membership_id, user["user_id"], request, idempotency_key
        )
    except PaymentError as exc:
        raise _http(exc)


@router.post("/org/payments/{payment_id}/replace", response_model=PaymentCreated, status_code=201)
def replace_payment(
    payment_id: UUID,
    request: PaymentReplace,
    idempotency_key: Optional[UUID] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Исправить оплату: старая отменяется, новая создаётся — одной транзакцией."""
    try:
        return PaymentService(db).replace_payment(
            user["org_id"], payment_id, user["user_id"], request, request.reason, idempotency_key
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
