"""ALGO - INCOME-2 («Поступления» v2): response models of the income overview, the month collection and the payments journal.

Design: ``artifacts/partner_doc_check/income2_spec.md``.  Every number comes from the ledger / the payment rows of the server; the front
shows them and never adds anything up.  Same convention as PAY-2: a field with a default is always present in the JSON.
"""
from __future__ import annotations

from datetime import date as date_type
from typing import List, Literal, Optional

from pydantic import ConfigDict, Field

from .payments_schemas import Allocation, PayModel, PaymentMethod, ReceiptsPosition

IncomePeriodKind = Literal["day", "week", "month", "custom"]
JournalStatus = Literal["active", "voided", "replaced"]


class IncomePeriod(PayModel):
    """A period and its name in the owner's words."""

    kind: IncomePeriodKind
    from_: date_type = Field(..., alias="from")
    to: date_type
    label: str  # «октябрь» | «сегодня» | «21 октября» | «неделя 12–18 октября» | «период 03.10–20.10»
    days: int
    in_progress: bool = False  # today lies inside the period (the comparison is like for like)

    model_config = ConfigDict(populate_by_name=True, json_schema_serialization_defaults_required=True)


class IncomeTotals(PayModel):
    """Where the money of the period went.  ``to_past_debt + to_current + to_advance + refunded_from_received + transferred == received``."""

    received: int = 0  # money payments entered in the period (transfers between groups are not receipts)
    payments_count: int = 0
    refunded: int = 0  # refunds dated in the period (money given back in the period)
    refunded_from_received: int = 0  # the part of the period's payments that was given back afterwards
    transferred: int = 0  # the part of the period's payments that moved to another group of the student
    to_past_debt: int = 0  # the period's payments by what they paid (relative to each payment's date)
    to_current: int = 0
    to_advance: int = 0
    written_off: int = 0  # debts forgiven in the period
    net_received: int = 0  # received - refunded


class IncomeMethod(PayModel):
    method: PaymentMethod
    label: str  # «Наличные» | «Карта» | «Перевод» | «Click» | «Payme» | «Другое» | «Способ не указан»
    amount: int = 0
    count: int = 0


class IncomeDay(PayModel):
    """A bar of the chart.  EVERY day of the period is present (zeros included); a period longer than 92 days is cut by months."""

    bucket: str  # "2026-10-21" | "2026-10"
    received: int = 0
    count: int = 0


class IncomeCash(PayModel):
    """«Наличные в кассе за сегодня» - only when the period is one day."""

    date: date_type
    label: str  # «Наличные в кассе за сегодня» | «Наличные в кассе за 21 октября»
    cash_received: int = 0
    cash_refunded: int = 0  # refunds paid out in cash that day
    expected: int = 0  # cash_received - cash_refunded
    payments_count: int = 0
    text: str


class CollectionRow(PayModel):
    """One group of «Собрано за месяц» (or the total).  ``charged`` = the cost of the periods that BEGAN in the month."""

    group_id: Optional[str] = None  # null in the total
    group_name: Optional[str] = None
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None
    charged: int = 0
    paid: int = 0  # how much of it is covered by money (any payment, advances of earlier months included)
    forgiven: int = 0
    debt: int = 0  # charged - paid - forgiven
    students: int = 0  # students charged in the month
    students_with_debt: int = 0  # in the total: DISTINCT students (a student who owes in two groups counts once)
    pct: Optional[int] = None  # floor(paid * 100 / charged): 100 only when the whole cost is paid; null when nothing is charged


class MonthCollection(PayModel):
    month: str  # "2026-10"
    month_label: str  # «октябрь 2026»
    from_: date_type = Field(..., alias="from")
    to: date_type
    total: CollectionRow
    groups: List[CollectionRow] = Field(default_factory=list)  # worst «Собрано %» first; a group with nothing charged is not listed

    model_config = ConfigDict(populate_by_name=True, json_schema_serialization_defaults_required=True)


class IncomeCheck(PayModel):
    code: Literal[
        "received_eq_strip", "received_eq_methods", "received_eq_series", "received_eq_journal", "collection_total_eq_groups"
    ]
    left: int
    right: int
    diff: int


class IncomeOverview(PayModel):
    """``GET /org/payments/income``: the whole page above the journal in one answer."""

    as_of: date_type
    period: IncomePeriod
    previous_period: IncomePeriod  # the same kind of period before it (a running one is cut to the same number of days)
    current: IncomeTotals
    previous: IncomeTotals  # the totals of ``previous_period``
    delta_amount: int = 0  # current.received - previous.received
    delta_pct: Optional[int] = None  # null when the previous period received nothing
    summary_text: str
    by_method: List[IncomeMethod] = Field(default_factory=list)  # only the methods that were used
    series: List[IncomeDay] = Field(default_factory=list)
    series_by: Literal["day", "month"] = "day"
    cash: Optional[IncomeCash] = None
    collection: Optional[MonthCollection] = None  # only for a month period
    position: ReceiptsPosition
    checks: List[IncomeCheck] = Field(default_factory=list)
    ok: bool = True  # every check has diff 0 («Сходится»)


class JournalAuthor(PayModel):
    user_id: str
    name: Optional[str] = None
    count: int = 0


class JournalRow(PayModel):
    id: str
    receipt_no: Optional[int] = None
    paid_on: date_type
    paid_time: Optional[str] = None  # "14:35" - when it was entered, Tashkent time
    created_at: Optional[str] = None  # naive UTC, ISO
    student_id: str
    student_name: str
    display_id: Optional[str] = None
    membership_id: str
    group_id: str
    group_name: str
    teacher_name: Optional[str] = None
    amount: int
    method: PaymentMethod
    method_label: str
    created_by_id: Optional[str] = None
    created_by_name: Optional[str] = None
    note: Optional[str] = None
    allocation_text: Optional[str] = None  # «за сентябрь — 200 000 · аванс — 50 000»; null on a voided / replaced payment
    allocations: List[Allocation] = Field(default_factory=list)
    refunded: int = 0
    transferred: int = 0
    status: JournalStatus = "active"
    replaces_receipt_no: Optional[int] = None  # this payment corrects that one
    replaces_amount: Optional[int] = None
    replaced_by_receipt_no: Optional[int] = None  # this payment was corrected by that one (status replaced)
    void_reason: Optional[str] = None
    voided_at: Optional[str] = None
    voided_by_name: Optional[str] = None


class JournalTotals(PayModel):
    """Of the ACTIVE payments of the filtered set; equals the period's «Поступило» for the same filters."""

    count: int = 0
    sum: int = 0
    voided_count: int = 0  # cancelled / replaced rows of the same set (they are in ``rows`` but not in count / sum)


class PaymentsJournal(PayModel):
    """``GET /org/payments/journal``."""

    as_of: date_type
    from_: date_type = Field(..., alias="from")
    to: date_type
    page: int = 1
    size: int = 25
    total: int = 0  # rows of the filtered set (voided included) - for the pager
    pages: int = 0
    totals: JournalTotals
    rows: List[JournalRow] = Field(default_factory=list)
    authors: List[JournalAuthor] = Field(default_factory=list)  # who entered payments in the period (for the «Кто внёс» filter)

    model_config = ConfigDict(populate_by_name=True, json_schema_serialization_defaults_required=True)
