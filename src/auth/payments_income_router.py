"""
ALGO - INCOME-2 endpoints («Поступления» v2: the overview with the comparison, the month collection, the payments journal).

Mounted by ``payments_router`` (under ``/api/v1/auth``).  Spec: ``artifacts/partner_doc_check/income2_spec.md``.  Same guard as every
money endpoint: ``require_payments_access`` (owner / admin with payments access); everything is limited to the organisation of the caller.
"""
from datetime import date
from typing import Literal, Optional
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from src.core.database import get_db

from .dependencies import require_payments_access
from .payments_income import IncomeReports
from .payments_income_schemas import IncomeOverview, JournalKind, PaymentsJournal
from .payments_schemas import PayError
from .payments_service import PaymentError

router = APIRouter(tags=["Payments"])  # the prefix /auth comes from payments_router

_ERRORS = {403: {"model": PayError}, 422: {"model": PayError}}


def _http(exc: PaymentError) -> HTTPException:
    return HTTPException(exc.status_code, exc.detail)


@router.get("/org/payments/income", response_model=IncomeOverview, responses=_ERRORS)
def income_overview(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    period: Optional[Literal["day", "week", "month", "custom"]] = Query(
        None, description="Вид периода; без него определяется по датам (один день / 7 дней с понедельника / целый месяц / свой)"),
    method: Optional[str] = None,
    group_id: Optional[UUID] = None,
    teacher_id: Optional[UUID] = None,
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Страница «Поступления» над журналом: четыре цифры периода с сравнением с предыдущим таким же, фраза-вывод, «куда ушли
    деньги», способы, ряд по дням (каждый день есть, с нулями), наличные за день, «Собрано за месяц» по группам (только месяц)."""
    try:
        return IncomeReports(db).overview(
            user["org_id"], from_, to, kind=period, method=method, group_id=group_id, teacher_id=teacher_id)
    except PaymentError as exc:
        raise _http(exc)


@router.get("/org/payments/journal", response_model=PaymentsJournal, responses=_ERRORS)
def payments_journal(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=100),
    method: Optional[str] = None,
    group_id: Optional[UUID] = None,
    teacher_id: Optional[UUID] = None,
    created_by: Optional[UUID] = None,
    q: Optional[str] = Query(None, max_length=100, description="Имя / фамилия / ID ученика (или № квитанции)"),
    status: Literal["all", "active", "voided"] = "all",
    kind: Optional[JournalKind] = Query(
        None, description="Только оплаты, в «куда пошли» которых есть этот вид (old_debt | current | forward | advance | refunded | transferred); "
                          "в строке остаётся весь split, totals.kind_sum = сумма этого вида по отобранным строкам = цифра плашки «Куда ушли деньги» "
                          "за тот же период и фильтры. Отменённые и исправленные строки вида не имеют и не показываются."),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
):
    """Журнал оплат периода: новые сверху, отменённые и исправленные видны (status), итог = сумма действующих оплат набора."""
    try:
        return IncomeReports(db).journal(
            user["org_id"], from_, to, page=page, size=size, method=method, group_id=group_id, teacher_id=teacher_id,
            created_by=created_by, q=q, status=status, kind=kind)
    except PaymentError as exc:
        raise _http(exc)


@router.get(
    "/org/payments/journal/export",
    responses={
        200: {"content": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}, "text/csv": {}},
              "description": "Файл журнала"},
        **_ERRORS,
    },
)
def payments_journal_export(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    format: Literal["xlsx", "csv"] = "xlsx",
    method: Optional[str] = None,
    group_id: Optional[UUID] = None,
    teacher_id: Optional[UUID] = None,
    created_by: Optional[UUID] = None,
    q: Optional[str] = Query(None, max_length=100),
    status: Literal["all", "active", "voided"] = "all",
    kind: Optional[JournalKind] = Query(
        None, description="Только оплаты, в «куда пошли» которых есть этот вид (old_debt | current | forward | advance | refunded | transferred); "
                          "в строке остаётся весь split, totals.kind_sum = сумма этого вида по отобранным строкам = цифра плашки «Куда ушли деньги» "
                          "за тот же период и фильтры. Отменённые и исправленные строки вида не имеют и не показываются."),
    user: dict = Depends(require_payments_access),
    db: Session = Depends(get_db),
) -> Response:
    """Журнал оплат файлом (xlsx / csv) с теми же фильтрами; ячейка, начинающаяся с = + - @, формулой не становится."""
    try:
        data, content_type, filename = IncomeReports(db).journal_file(
            user["org_id"], from_, to, fmt=format, method=method, group_id=group_id, teacher_id=teacher_id,
            created_by=created_by, q=q, status=status, kind=kind)
    except PaymentError as exc:
        raise _http(exc)
    return Response(
        content=data, media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"; filename*=UTF-8\'\'{quote(filename)}'})
