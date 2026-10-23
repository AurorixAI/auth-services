"""
ALGO - Payroll router (N13 P3).

Mounted under /api/v1/auth.  Every handler is tenant-scoped by the org of the token and guarded by
``require_payroll_owner``: the owner only.  An administrator is refused (403) even when the owner gave
the administrator the right to payments, a teacher too; a sheet of another centre is a 404.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models.auth import Organization

from .dependencies import require_payroll_owner
from . import payment_status, xlsx_style
from .payroll_export import UNSAVED_NOTE, XLSX_MIME, build_workbook, export_filename
from .payroll_schemas import (
    CalculateRequest,
    CalculateResponse,
    MonthOverview,
    PayoutCreate,
    PayoutCreated,
    PayoutError,
    PayoutOut,
    PayoutVoidRequest,
    PayoutVoided,
    PayrollSettings,
    PayrollSettingsUpdate,
    RateHistoryRow,
    RatePut,
    RatesResponse,
    RevisionOut,
    SheetBrief,
    SheetCreate,
    SheetDiff,
    SheetOut,
    SheetPatch,
    TeacherRates,
)
from .payroll_rates_service import RatesService
from .payroll_payouts import PayoutError as PayoutError_
from .payroll_payouts import PayrollPayoutService as PrPayoutService
from .payroll_service import PayrollError, PayrollService, parse_month

router = APIRouter(prefix="/auth", tags=["Payroll"])


def _http(exc: PayrollError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


def _xlsx(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type=XLSX_MIME,
        headers=xlsx_style.attachment_headers(
            filename, **{"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}),
    )


# ---- settings ---------------------------------------------------------------------


@router.get("/org/payroll/settings", response_model=PayrollSettings)
def get_payroll_settings(user: dict = Depends(require_payroll_owner), db: Session = Depends(get_db)):
    """Делитель зарплаты (по умолчанию 12)."""
    return PayrollService(db).get_settings(user["org_id"])


@router.put("/org/payroll/settings", response_model=PayrollSettings)
def update_payroll_settings(
    request: PayrollSettingsUpdate,
    user: dict = Depends(require_payroll_owner),
    db: Session = Depends(get_db),
):
    try:
        return PayrollService(db).update_settings(user["org_id"], user["user_id"], request.divisor, request.valid_from)
    except PayrollError as exc:
        raise _http(exc)


# ---- rates (UX-2) -----------------------------------------------------------------


@router.get("/org/payroll/rates", response_model=RatesResponse)
def list_rates(
    month: Optional[str] = Query(None, max_length=7),
    user: dict = Depends(require_payroll_owner),
    db: Session = Depends(get_db),
):
    """Ставки преподавателей на месяц (по умолчанию текущий): по каждому виду занятий — действующая и ближайшая будущая."""
    try:
        return RatesService(db).list_rates(user["org_id"], month)
    except PayrollError as exc:
        raise _http(exc)


@router.put("/org/payroll/rates/{teacher_id}", response_model=TeacherRates)
def put_rates(
    teacher_id: UUID,
    request: RatePut,
    user: dict = Depends(require_payroll_owner),
    db: Session = Depends(get_db),
):
    """Задать ставки преподавателя с месяца ``valid_from`` (ГГГГ-ММ); возвращает его строку списка на текущий месяц."""
    try:
        return RatesService(db).put_rates(user["org_id"], user["user_id"], teacher_id, request)
    except PayrollError as exc:
        raise _http(exc)


@router.get("/org/payroll/rates/{teacher_id}/history", response_model=list[RateHistoryRow])
def rates_history(teacher_id: UUID, user: dict = Depends(require_payroll_owner), db: Session = Depends(get_db)):
    """Все строки ставок преподавателя, новые выше."""
    try:
        return RatesService(db).history(user["org_id"], teacher_id)
    except PayrollError as exc:
        raise _http(exc)


# ---- month ------------------------------------------------------------------------


@router.get("/org/payroll/months/{month}", response_model=MonthOverview)
def month_overview(month: str, user: dict = Depends(require_payroll_owner), db: Session = Depends(get_db)):
    """Преподаватели месяца: типы занятий, подсказанный %, сохранённая ведомость, предупреждения."""
    try:
        return PayrollService(db).month_overview(user["org_id"], month)
    except PayrollError as exc:
        raise _http(exc)


@router.post("/org/payroll/months/{month}/calculate", response_model=CalculateResponse)
def calculate_month(
    month: str,
    request: CalculateRequest,
    user: dict = Depends(require_payroll_owner),
    db: Session = Depends(get_db),
):
    """Предпросмотр ведомостей месяца. Ничего не записывает."""
    try:
        return PayrollService(db).calculate(user["org_id"], month, request)
    except PayrollError as exc:
        raise _http(exc)


@router.get("/org/payroll/months/{month}/export.xlsx")
def export_month(month: str, user: dict = Depends(require_payroll_owner), db: Session = Depends(get_db)):
    """Excel: первый лист «Итого» (учитель · заработано · выплачено · к выплате) и лист на каждого преподавателя с сохранённой ведомостью месяца."""
    missing: list = []
    try:
        sheets, label = PayrollService(db).sheets_for_export(user["org_id"], None, month, unsaved=missing)
    except PayrollError as exc:
        raise _http(exc)
    # the teachers who held lessons in the month but have no saved sheet: told on the first sheet «Итого», never silently left out
    unsaved = [{"teacher_name": t["teacher_name"], "note": UNSAVED_NOTE} for t in missing]
    org = db.get(Organization, user["org_id"])
    summary = {"month": label, "month_open": sheets[0].month_open, "unsaved": unsaved, "org_name": org.name if org else None,
               "as_of": payment_status.business_today()}
    return _xlsx(build_workbook([s.model_dump() for s in sheets], summary=summary), export_filename(label))


# ---- sheets -----------------------------------------------------------------------


@router.post("/org/payroll/sheets", response_model=SheetOut, status_code=201)
def save_sheet(
    request: SheetCreate, user: dict = Depends(require_payroll_owner), db: Session = Depends(get_db)
):
    """Сохранить ведомость преподавателя; если она уже есть — следующая редакция (нужен ``revision``)."""
    try:
        return PayrollService(db).save_sheet(user["org_id"], user["user_id"], request)
    except PayrollError as exc:
        raise _http(exc)


@router.get("/org/payroll/sheets", response_model=list[SheetBrief])
def list_sheets(
    month: Optional[str] = Query(None, max_length=7),
    user: dict = Depends(require_payroll_owner),
    db: Session = Depends(get_db),
):
    try:
        return PayrollService(db).list_sheets(user["org_id"], month)
    except PayrollError as exc:
        raise _http(exc)


@router.get("/org/payroll/sheets/{sheet_id}", response_model=SheetOut)
def get_sheet(sheet_id: UUID, user: dict = Depends(require_payroll_owner), db: Session = Depends(get_db)):
    try:
        return PayrollService(db).get_sheet(user["org_id"], sheet_id)
    except PayrollError as exc:
        raise _http(exc)


@router.patch("/org/payroll/sheets/{sheet_id}", response_model=SheetOut)
def patch_sheet(
    sheet_id: UUID,
    request: SheetPatch,
    user: dict = Depends(require_payroll_owner),
    db: Session = Depends(get_db),
):
    """Правка = новая редакция. ``revision`` защищает от чужой правки (409 SHEET_REVISION_CONFLICT)."""
    try:
        return PayrollService(db).patch_sheet(user["org_id"], user["user_id"], sheet_id, request)
    except PayrollError as exc:
        raise _http(exc)


@router.get("/org/payroll/sheets/{sheet_id}/revisions", response_model=list[RevisionOut])
def sheet_revisions(sheet_id: UUID, user: dict = Depends(require_payroll_owner), db: Session = Depends(get_db)):
    try:
        return PayrollService(db).revisions(user["org_id"], sheet_id)
    except PayrollError as exc:
        raise _http(exc)


@router.get("/org/payroll/sheets/{sheet_id}/diff", response_model=SheetDiff)
def sheet_diff(sheet_id: UUID, user: dict = Depends(require_payroll_owner), db: Session = Depends(get_db)):
    """Что изменится при пересчёте по текущим данным (по строкам)."""
    try:
        return PayrollService(db).diff(user["org_id"], sheet_id)
    except PayrollError as exc:
        raise _http(exc)


@router.get("/org/payroll/sheets/{sheet_id}/export.xlsx")
def export_sheet(sheet_id: UUID, user: dict = Depends(require_payroll_owner), db: Session = Depends(get_db)):
    try:
        sheets, label = PayrollService(db).sheets_for_export(user["org_id"], sheet_id, None)
    except PayrollError as exc:
        raise _http(exc)
    sheet = sheets[0]
    return _xlsx(build_workbook([sheet.model_dump()]), export_filename(label, sheet.teacher_name))


# ---- payouts (LOGIC-A, decision 6) ---------------------------------------------------------------------

_PAYOUT_ERRORS = {403: {"model": PayoutError}, 404: {"model": PayoutError}, 409: {"model": PayoutError}, 422: {"model": PayoutError}}


def _payout_http(exc: PayoutError_) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


def _fresh(db: Session, org_id: str):
    service = PayrollService(db)
    return lambda sheet: service._fresh_total(org_id, sheet)


@router.get("/org/payroll/payouts", response_model=list[PayoutOut], responses=_PAYOUT_ERRORS)
def list_payouts(
    month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"),
    teacher_id: Optional[UUID] = None,
    user: dict = Depends(require_payroll_owner),
    db: Session = Depends(get_db),
):
    """Выплаты (живые и отменённые) за месяц / преподавателя."""
    try:
        first = parse_month(month)[0] if month else None
        return PrPayoutService(db).list(user["org_id"], month=first, teacher_id=teacher_id)
    except PayrollError as exc:
        raise _http(exc)


@router.post("/org/payroll/sheets/{sheet_id}/payouts", response_model=PayoutCreated, status_code=201, responses=_PAYOUT_ERRORS)
def create_payout(
    sheet_id: UUID,
    request: PayoutCreate,
    idempotency_key: Optional[UUID] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(require_payroll_owner),
    db: Session = Depends(get_db),
):
    """Отметить выплату зарплаты по сохранённой ведомости (аванс учителю допустим: «переплачено»)."""
    try:
        return PrPayoutService(db).create(user["org_id"], sheet_id, user["user_id"], request, idempotency_key, _fresh(db, user["org_id"]))
    except PayoutError_ as exc:
        raise _payout_http(exc)


@router.post("/org/payroll/payouts/{payout_id}/void", response_model=PayoutVoided, responses=_PAYOUT_ERRORS)
def void_payout(
    payout_id: UUID,
    request: PayoutVoidRequest,
    user: dict = Depends(require_payroll_owner),
    db: Session = Depends(get_db),
):
    try:
        return PrPayoutService(db).void(user["org_id"], payout_id, user["user_id"], request.reason, _fresh(db, user["org_id"]))
    except PayoutError_ as exc:
        raise _payout_http(exc)
