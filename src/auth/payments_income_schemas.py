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
    """Where the money of the period went.  ``to_past_debt + to_current + to_forward + to_advance + refunded_from_received + transferred == received``.
    Each payment is split by the CURRENT ledger allocation, every piece classified relative to the payment's own date (see ``IncomeStripPart``)."""

    received: int = 0  # money payments entered in the period (transfers between groups are not receipts)
    payments_count: int = 0
    refunded: int = 0  # refunds dated in the period (money given back in the period)
    refunded_from_received: int = 0  # the part of the period's payments that was given back afterwards
    transferred: int = 0  # the part of the period's payments that moved to another group of the student
    to_past_debt: int = 0  # the period's payments by what they paid (relative to each payment's date)
    to_current: int = 0
    to_forward: int = 0  # S1: paid FORWARD - a period that starts after the payment's date and has been consumed since (was inside ``to_advance``)
    to_advance: int = 0  # S1: money no period has absorbed yet (the ledger's advance); always <= position.advance_total
    written_off: int = 0  # debts forgiven in the period
    net_received: int = 0  # received - refunded


class IncomeStripPart(PayModel):
    """One part of «Куда ушли деньги» (the labels are the server's words): ``old_debt`` «на старые долги» (the period ended before the payment's date), ``current`` «за текущий период»
    (the period contains it), ``forward`` «вперёд (за будущие периоды)» (starts after it, consumed since), ``advance`` «пока не потрачено (аванс)», ``refunded``, ``transferred``."""

    kind: Literal["old_debt", "current", "forward", "advance", "refunded", "transferred"]
    label: str
    amount: int = 0


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
    scope: str = "centre"  # the till of the whole centre, always: group / teacher / method filters never narrow it
    filtered_view: bool = False  # true when a group or teacher filter is active: say «в кассе центра за сегодня (все группы)»
    scope_text: Optional[str] = None  # «в кассе центра за сегодня (все группы)» when filtered_view, else null


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
    text: str  # «Оплачено 1 900 000 из 2 500 000 сум (76%)» - the line of the card
    total: CollectionRow
    groups: List[CollectionRow] = Field(default_factory=list)  # worst «Собрано %» first; a group with nothing charged is not listed

    model_config = ConfigDict(populate_by_name=True, json_schema_serialization_defaults_required=True)


class IncomePosition(ReceiptsPosition):
    """Where the centre stands TODAY (the two cards «Долг на сегодня» and «Аванс»): the receipts position plus the students with an advance."""

    students_with_advance: int = 0  # DISTINCT students (a student with an advance in two groups counts once)


class IncomeCheck(PayModel):
    code: Literal[
        "received_eq_strip", "received_eq_methods", "received_eq_series", "received_eq_journal", "collection_total_eq_groups",
        # Σ of the strip by kind == Σ of the journal rows' split by kind (same filters); present for a kind when either side is non-zero
        "strip_old_debt_eq_journal", "strip_current_eq_journal", "strip_forward_eq_journal", "strip_advance_eq_journal", "strip_refunded_eq_journal",
        "strip_transferred_eq_journal",
    ]
    left: int
    right: int
    diff: int


class IncomeOverview(PayModel):
    """``GET /org/payments/income``: the whole page above the journal in one answer."""

    as_of: date_type
    period: IncomePeriod
    previous_period: IncomePeriod  # the same kind of period before it (a running one is cut to the same number of days)
    previous_label: str  # the human name of that period, ALWAYS present (also when it had no payments): «сентябрь» | «вчера» | «прошлая неделя» | «12–18 окт.»
    previous_hint: Optional[str] = None  # the comparison phrase in the dative, ready to follow «к»: «сентябрю» / «1–22 сентября» / «тем же дням прошлой недели (12–15 окт.)»
    current: IncomeTotals
    previous: IncomeTotals  # the totals of ``previous_period``
    delta_amount: int = 0  # current.received - previous.received
    delta_pct: Optional[int] = None  # null when the previous period received nothing
    delta_direction: Optional[Literal["up", "down", "same"]] = None  # null when there is nothing to compare with
    delta_text: Optional[str] = None  # «12% к сентябрю» (the front adds ▲ / ▼ and the colour by ``delta_direction``)
    summary_text: str
    by_method: List[IncomeMethod] = Field(default_factory=list)  # only the methods that were used
    strip: List[IncomeStripPart] = Field(default_factory=list)  # «Куда ушли деньги»: the six parts of ``current`` (zeros included) with the server's labels; the amounts sum to ``current.received``
    series: List[IncomeDay] = Field(default_factory=list)
    series_by: Literal["day", "month"] = "day"
    cash: Optional[IncomeCash] = None
    collection: Optional[MonthCollection] = None  # only for a month period
    position: IncomePosition
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
    paid_time: Optional[str] = None  # "14:35" - the entry time, Tashkent; ONLY when the payment was entered on its own day (paid_on), null for a back-dated one
    entered_at_text: Optional[str] = None  # «внесена 21.10 в 11:40» (Tashkent) when the payment was entered on another day than paid_on, else null
    created_at: Optional[str] = None  # naive UTC, ISO (the raw moment of entry)
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
    split: List[IncomeStripPart] = Field(default_factory=list)  # the same kinds as the strip, only the non-zero parts of THIS payment; the rows of a set sum to the strip of the same filters
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
    authors: List[JournalAuthor] = Field(
        default_factory=list,
        description="Who entered payments (for the «Кто внёс» filter): counted over the period and ALL the other filters (method, group, teacher, q) "
                    "but IGNORING ``created_by`` (and ``status``), so choosing a person does not collapse the options to that person.")

    model_config = ConfigDict(populate_by_name=True, json_schema_serialization_defaults_required=True)
