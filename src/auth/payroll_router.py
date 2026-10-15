"""
ALGO - Payroll router (N13 P3).

Mounted under /api/v1/auth.  Every handler is tenant-scoped by the org of the token and guarded by
``require_payroll_owner``: the owner only.  An administrator is refused (403) even when the owner gave
the administrator the right to payments, a teacher too; a sheet of another centre is a 404.
"""
from typing import Optional
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from src.core.database import get_db

from .dependencies import require_payroll_owner
from .payroll_export import XLSX_MIME, build_workbook, export_filename
from .payroll_schemas import (
    CalculateRequest,
    CalculateResponse,
    MonthOverview,
    PayrollSettings,
    PayrollSettingsUpdate,
    RevisionOut,
    SheetBrief,
    SheetCreate,
    SheetDiff,
    SheetOut,
    SheetPatch,
)
from .payroll_service import PayrollError, PayrollService

router = APIRouter(prefix="/auth", tags=["Payroll"])


def _http(exc: PayrollError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


def _xlsx(content: bytes, filename: str) -> Response:
    ascii_name = "".join(ch for ch in filename.encode("ascii", "ignore").decode() if ch not in '"\\;').strip()
    if not ascii_name.lower().endswith(".xlsx"):
        ascii_name = "payroll.xlsx"
    return Response(
        content=content,
        media_type=XLSX_MIME,
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
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
        return PayrollService(db).update_settings(user["org_id"], user["user_id"], request.divisor)
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
    """Excel: лист на каждого преподавателя с сохранённой ведомостью месяца."""
    try:
        sheets, label = PayrollService(db).sheets_for_export(user["org_id"], None, month)
    except PayrollError as exc:
        raise _http(exc)
    return _xlsx(build_workbook([s.model_dump() for s in sheets]), export_filename(label))


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
