"""ALGO - request/response schemas of the payments API (N13 P2).  snake_case."""
from __future__ import annotations

from datetime import date
from datetime import date as date_type
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_MONEY = 10_000_000_000

PeriodMode = Literal["calendar", "lessons"]
BillingAnchor = Literal["payment_date", "fixed_day"]
QuoteBasis = Literal["full", "lessons", "days", "package", "custom", "none"]
MembershipStatus = Literal["none", "free", "expired", "ends_today", "expiring", "active", "paused"]
PriceSource = Literal["group", "override", "none"]


class PriceScheduled(BaseModel):
    """A future row of the individual price of a membership (``DELETE /org/memberships/{id}/price-override/{row_id}`` takes it back)."""

    row_id: str
    amount: Optional[int] = None
    valid_from: date_type
    reason: Optional[str] = None


class PriceOut(BaseModel):
    """The effective price of a membership on a day (ROADMAP #105, ``src/auth/pricing.py``).

    ``source``: ``group`` - the price of the group; ``override`` - the individual price of the student (then
    ``reason`` says why and ``group_amount`` is what the group price would be); ``none`` - not set anywhere.
    ``valid_from`` - the day the row that gave the price started.  The sum means "Оплата в месяц" (calendar
    periods) or "Стоимость пакета" (lesson packages) as the centre's billing mode says.
    """

    amount: Optional[int] = None
    source: PriceSource = "none"
    valid_from: Optional[date_type] = None
    group_amount: Optional[int] = None
    reason: Optional[str] = None
    # #126 (additive): who set the individual price in force; the nearest FUTURE individual price of the membership (``amount`` null = «цена группы снова»)
    set_by_name: Optional[str] = None
    scheduled: Optional["PriceScheduled"] = None


# ---- PAY-2: money as money (design ``artifacts/partner_doc_check/pay2_tech_design.md`` §4) ---------------------------
#
# A payment is a sum.  Charges, the distribution of the money over periods, "paid until", debt and advance are DERIVED
# by ``billing_ledger.run_ledger`` from the facts (payments, write-offs, refunds, transfers, stays, prices, settings).
# The models below are the FROZEN contract of that API: every response field is always present (``null`` when empty).

PaymentMethod = Literal["cash", "card", "bank_transfer", "click", "payme", "other", "unknown"]
# ``unknown`` is what the migration gives to old rows; a new payment / refund must name a real method.
NewPaymentMethod = Literal["cash", "card", "bank_transfer", "click", "payme", "other"]
PaymentKind = Literal["payment", "legacy_carry"]
AllocationBucket = Literal["past_debt", "current", "advance"]
SegmentKind = Literal["period", "package", "opening"]  # "opening": the debt of the opening balance (LOGIC-A)
SegmentBasis = Literal["full", "lessons", "days", "zero", "opening"]
# closed: nothing unpaid; future: the period has not begun, something is unpaid; partial: begun, some money, not all;
# open: begun, no money; unpriced: no price is set for the period (nothing is charged).
SegmentState = Literal["closed", "partial", "open", "future", "unpriced"]
SegmentBucket = Literal["past_debt", "current", "future"]
StatementRowType = Literal["charge", "payment", "writeoff", "refund", "transfer_in", "transfer_out",
                           "opening_debt", "opening_paid_until", "pause"]  # LOGIC-A: the opening balance and a pause (info row, no money)
ReceiptColumn = Literal["received", "to_past_debt", "to_current", "to_advance", "written_off", "refunded"]
ReceiptItemType = Literal["payment", "writeoff", "refund"]
PayWarningCode = Literal[
    "price_missing", "horizon_capped", "large_overpay", "student_archived", "left_debt_only", "price_change_ahead",
    "paid_over_60_days_ago",
    # LEGACY (period payments of the old model): no longer emitted after PAY-2 stream B, dropped from this list then.
    "overlaps_previous", "gap_before", "period_over_62_days", "short_period_full_amount",
]


class PayModel(BaseModel):
    """Base of the PAY-2 response models: a field with a default is still ALWAYS in the JSON, so it is ``required``
    in the OpenAPI schema (the generated front types are exact)."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class PayErrorDetail(PayModel):
    """``{"detail": {...}}`` of an error (``PriceError`` / ``PaymentError`` with a code).

    Codes: ``DUPLICATE_PAYMENT`` (409), ``STUDENT_ARCHIVED`` (409), ``ALREADY_VOIDED`` (409), ``VOID_BREAKS_REFUND``
    (409), ``CONCURRENT_EDIT`` (409), ``AMOUNT_RANGE`` (422), ``PAID_ON_RANGE`` (422), ``METHOD_REQUIRED`` (422),
    ``REASON_REQUIRED`` (422), ``WRITEOFF_EXCEEDS_DEBT`` (422, ``debt``), ``REFUND_EXCEEDS_ADVANCE`` (422,
    ``refundable``), ``ACCOUNTING_START_RANGE`` (422), ``RANGE_TOO_LONG`` (422).  403 / 404 carry a plain message.
    """

    code: Optional[str] = None
    message: str
    debt: Optional[int] = None  # WRITEOFF_EXCEEDS_DEBT: the debt that can be written off
    refundable: Optional[int] = None  # REFUND_EXCEEDS_ADVANCE: the most that can be returned
    payment_id: Optional[str] = None  # DUPLICATE_PAYMENT: the payment the same Idempotency-Key created first


class PayError(PayModel):
    detail: PayErrorDetail


class SegmentLessons(PayModel):
    """A period prorated by the lessons of the group: lessons in the charged part / in the whole period."""

    in_part: int
    in_period: int


class Segment(PayModel):
    """One charge: a period of the student in the group (or a lessons package)."""

    id: str  # "seg:{run}:{period_start}" - deterministic
    kind: SegmentKind = "period"
    period_from: date_type  # the whole period of the centre/student (before cutting)
    period_to: date_type
    billable_days: int  # days of the period that are charged (the student was in the group, after the accounting start)
    days_text: Optional[str] = None  # «с 16.10 (7 уроков из 13)» for an incomplete period, else null
    price: PriceOut = Field(default_factory=PriceOut)  # the price at the START of the period
    charge: int  # charged for the segment
    basis: SegmentBasis = "full"
    lessons: Optional[SegmentLessons] = None
    paid: int = 0  # money that lies on the segment (payments and transfers in; write-offs not included)
    written_off: int = 0
    unpaid: int = 0  # charge - paid - written_off
    remaining: int = 0  # LOGIC-A: the same as ``unpaid`` under the name the month header uses (charge - paid - written_off)
    covered_days: int = 0  # floor(money * billable_days / charge); a day is paid only when paid in full
    covered_through: Optional[date_type] = None  # last paid day of the segment (null - none)
    partial_credit: int = 0  # money "inside" the first unpaid day; counts towards the next payment
    state: SegmentState = "open"
    # relative to ``as_of`` (today): the period has ended / is running / has not begun (same rule as ``Allocation.bucket``)
    bucket: SegmentBucket = "current"


class Allocation(PayModel):
    """A piece of one payment in one segment ("where the money went")."""

    segment_id: str
    period_from: date_type
    period_to: date_type
    amount: int
    days: int  # days this piece adds to the segment's paid days (the pieces of all payments sum to covered_days)
    bucket: AllocationBucket  # relative to the DATE OF THE PAYMENT: ended before it / running / starts after it
    closes: bool  # after this piece the segment is paid in full
    label: str  # «Закрыла долг 15.09–14.10» | «На текущий период 15.10–14.11» | «Аванс на 15.11–14.12»
    price: Optional[int] = None  # the price of that period (differs from today's price when a price change is announced)
    # LOGIC-A: what is still unpaid of THAT period right after this piece (after the earlier payments / write-offs / refunds in
    # the order of the money line); null on the "advance" line and on a snapshot of a receipt made before LOGIC-A.
    segment_remaining_after: Optional[int] = None


class SnapshotPrice(PayModel):
    amount: Optional[int] = None
    source: PriceSource = "none"


class PaymentAllocationOut(PayModel):
    """The CURRENT (factual) distribution of a payment, ``PaymentOut.allocation``."""

    items: List[Allocation] = Field(default_factory=list)
    refunded: int = 0  # how much of the payment went back to the student (refunds, LIFO)
    transferred: int = 0  # how much of it travelled to another group of the student (transfers out, LIFO)
    net: int = 0  # amount - refunded - transferred


class AllocationSummary(PayModel):
    """What a just entered payment did (``PaymentCreated.allocation``)."""

    items: List[Allocation] = Field(default_factory=list)
    debt_before: int = 0
    debt_after: int = 0
    advance_after: int = 0
    paid_until_before: Optional[date_type] = None
    paid_until_after: Optional[date_type] = None


class PaymentSettings(BaseModel):
    period_mode: PeriodMode = "calendar"
    # N13 P2.2.  ``payment_date``: a calendar period starts on the payment date (05.10 -> 04.11), or the day after the
    # previous "paid until" when there is one (an overdue student closes the debt first).
    # ``fixed_day``: the centre has one payment day, ``billing_day`` (1..28): day 1 -> 01.10-31.10,
    # day 10 -> 10.10-09.11; a student who comes mid-period pays for the incomplete one.
    billing_anchor: BillingAnchor = "payment_date"
    billing_day: int = Field(1, ge=1, le=28)
    lessons_per_payment: int = Field(12, ge=1, le=60)
    expiring_days: int = Field(3, ge=0, le=14)
    expiring_lessons: int = Field(3, ge=0, le=10)
    # An excuse must be confirmed this many days before the end of the paid
    # period, or when this many lessons are left in a lessons package.
    excuse_deadline_days: int = Field(5, ge=0, le=30)
    excuse_deadline_lessons: int = Field(2, ge=0, le=12)
    admin_can_manage: bool = False
    # PAY-2.  "Начало учёта оплат": days before it are never charged (a student entered with a past join date gets no
    # debt for the months before ALGO).  GET always returns the EFFECTIVE date: the stored setting, else the centre's
    # earliest live payment date / the migration day (stored by the migration), else the day the centre was created.
    accounting_start_date: Optional[date_type] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class PaymentSettingsUpdate(BaseModel):
    """PATCH semantics: a field that is absent (or null) stays as it was."""

    period_mode: Optional[PeriodMode] = None
    billing_anchor: Optional[BillingAnchor] = None
    billing_day: Optional[int] = Field(None, ge=1, le=28)
    lessons_per_payment: Optional[int] = Field(None, ge=1, le=60)
    expiring_days: Optional[int] = Field(None, ge=0, le=14)
    expiring_lessons: Optional[int] = Field(None, ge=0, le=10)
    excuse_deadline_days: Optional[int] = Field(None, ge=0, le=30)
    excuse_deadline_lessons: Optional[int] = Field(None, ge=0, le=12)
    admin_can_manage: Optional[bool] = None
    # PAY-2, the owner only; not later than today + 31 days (422 ``ACCOUNTING_START_RANGE``).
    accounting_start_date: Optional[date_type] = None


class CarriedFromOut(BaseModel):
    """The payment a carry-over (N13 P2.1) was made from, and the group it belonged to."""

    payment_id: str
    group_name: str


class CarriedOutOut(BaseModel):
    """The payment moved to another group with the student on ``date`` (business date, N13 P2.1)."""

    date: date_type
    group_name: Optional[str] = None


class PaymentFileOut(BaseModel):
    """A photo / scan of the cheque attached to a payment (the bytes are served by their own endpoint)."""

    id: str
    filename: Optional[str] = None
    content_type: str
    size_bytes: int
    uploaded_at: Optional[str] = None
    uploaded_by_name: Optional[str] = None


class PaymentOut(BaseModel):
    id: str
    membership_id: str
    amount: int
    currency: str
    paid_on: date
    # PAY-2: a payment is a sum, it has no period.  ``valid_from`` / ``valid_to`` / ``lessons_count`` are LEGACY: set on
    # rows entered before PAY-2 (``legacy_period`` true) and, for ONE release, filled on new payments for the old front
    # (first period of the distribution / "paid until") - the new front must not read them.
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    lessons_count: Optional[int] = None
    legacy_period: bool = False
    method: PaymentMethod = "unknown"
    kind: PaymentKind = "payment"  # ``legacy_carry``: an old carry-over copy (amount 0), hidden by the UI
    # The CURRENT distribution of the payment over the periods (null on a voided payment and on a legacy carry).
    allocation: Optional[PaymentAllocationOut] = None
    note: Optional[str] = None
    replaces_payment_id: Optional[str] = None
    # N13 P2.2: the receipt number (None on a carry-over: it is not money); the payment did not cover
    # whole periods of the centre; the attached cheques (without their bytes).
    receipt_no: Optional[int] = None
    partial_period: bool = False
    files: List[PaymentFileOut] = Field(default_factory=list)
    # Set on a carry-over made by a transfer (amount 0): "перенос из «<group>»".
    carried_from: Optional[CarriedFromOut] = None
    # Set on the original once it moved: it covers only the days before ``date``.
    carried_out: Optional[CarriedOutOut] = None
    created_by: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    voided_at: Optional[str] = None
    voided_by: Optional[str] = None
    voided_by_name: Optional[str] = None
    void_reason: Optional[str] = None


class MembershipPaymentRow(BaseModel):
    membership_id: str
    student_id: str
    student_name: str
    display_id: Optional[str] = None
    class_level: Optional[int] = None
    group_id: str
    group_name: str
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None
    is_primary: bool
    is_active: bool
    # ROADMAP #105: the effective price today.  ``monthly_fee`` is the same sum under the old name (nothing writes it
    # any more; PUT /org/memberships/{id}/fee answers 410) - the front switches to ``price`` and drops it.
    price: PriceOut = Field(default_factory=PriceOut)
    monthly_fee: Optional[int] = None
    status: MembershipStatus
    period_mode: Optional[PeriodMode] = None
    paid_until: Optional[date] = None
    days_overdue: Optional[int] = None
    days_left: Optional[int] = None
    lessons_total: Optional[int] = None
    lessons_used: Optional[int] = None
    lessons_left: Optional[int] = None
    last_payment: Optional[PaymentOut] = None
    # PAY-2 (always present).  ``paid_until`` may be inside a period (money buys days).  ``debt`` = what the periods
    # that have begun cost minus the money; ``advance`` = money beyond that; never both > 0.  ``partial_credit`` - money
    # inside the first unpaid day.  ``lessons_unpaid`` - held lessons not covered by the money (lessons mode).
    # ``refundable`` - how much of the advance can be returned; ``writeoff_allowed`` - debt > 0 and the caller may.
    debt: int = 0
    past_debt: int = 0
    current_due: int = 0
    advance: int = 0
    partial_credit: int = 0
    price_missing: bool = False
    lessons_unpaid: int = 0
    refundable: int = 0
    writeoff_allowed: bool = False
    # LOGIC-A.  ``paused``: today lies inside a live pause (status ``paused``); ``opening``: the live "opening balance" of
    # the membership; ``price_ends_on_leave``: the individual price of the membership was ended by a departure.
    paused: Optional["PausedRange"] = None
    opening: Optional["OpeningBrief"] = None
    price_ends_on_leave: bool = False
    # LOGIC-A (owner, 21.10): the tab of the row - ``debt`` Должники (debt > 0, any reason; students who left included), ``expiring`` Скоро
    # платить (no debt, the next period is due within ``expiring_days``), ``active`` Оплачено (no debt, not due soon; an advance included),
    # ``paused`` На паузе (today is inside a pause, no debt); null: a free student, one without a price, a membership that is not tracked.
    # ``debt_reason`` (code) and ``reason`` (the sentence of the debtor line) say why a debtor is one.
    bucket: Optional[Literal["debt", "expiring", "active", "paused"]] = None
    debt_reason: Optional[Literal["overdue", "partial", "never_paid"]] = None
    reason: Optional[str] = None  # the line of a debtor: «Просрочено на 12 дней» | «Не доплачено за октябрь: 150 000» | «Ещё не платил»


class PaymentsBuckets(BaseModel):
    """Memberships per tab of the payments page (the same sets ``GET /org/payments/status?bucket=`` lists)."""

    debt: int = 0
    expiring: int = 0
    active: int = 0
    paused: int = 0


class PaymentsSummary(BaseModel):
    as_of: str
    due: int
    ends_today: int
    expired: int
    expiring: int
    none: int
    active: int  # the "paid" tab: statuses active + free
    free: int
    tracked_total: int
    period_mode: PeriodMode
    lessons_per_payment: int
    # DISTINCT students (the counters above count memberships, so a student in two groups counts twice there):
    # ``students_due`` - at least one membership in the ``due`` bucket (expired or ends today), ``students_expired`` -
    # at least one expired membership.  Added next to the old fields, which keep their meaning.
    students_due: int
    students_expired: int
    # PAY-2: the debt and the advance of the whole centre today (memberships of students who left / archived included).
    debt_total: int = 0
    advance_total: int = 0
    memberships_with_debt: int = 0
    students_with_debt: int = 0
    accounting_start_date: Optional[date_type] = None
    paused: int = 0  # LOGIC-A: memberships whose today is inside a pause
    # LOGIC-A (owner, 21.10): the new tabs.  ``buckets.debt`` == ``memberships_with_debt``; Σ debt of that tab == ``debt_total``;
    # ``debtors`` == ``students_with_debt`` (distinct students).  ``free`` (existing) and ``price_missing`` are the students outside the
    # tabs (a warning line); ``students_with_advance`` is the advance card (with ``advance_total``).
    buckets: "PaymentsBuckets" = Field(default_factory=lambda: PaymentsBuckets())
    price_missing: int = 0
    students_with_advance: int = 0
    debtors: int = 0
    students_paused: int = 0  # DISTINCT students with a membership on pause today (== the students of the «На паузе» tab); ``buckets.paused`` counts memberships


class PaymentStatusPage(BaseModel):
    items: List[MembershipPaymentRow]
    total: int
    limit: int
    offset: int
    as_of: str


# ---- group price and individual price (ROADMAP #105) ------------------------------------------------


class GroupPriceCurrent(BaseModel):
    amount: int
    valid_from: date_type
    note: Optional[str] = None


class GroupPriceChange(BaseModel):
    """One write of the price of a day (the audit trail, ``group_audit``): who, when, from what to what."""

    at: Optional[str] = None  # naive UTC, ISO
    actor_name: Optional[str] = None
    old_amount: Optional[int] = None  # in force on that day just before the write; null - none
    new_amount: int
    note: Optional[str] = None


class GroupPriceRow(BaseModel):
    id: str
    amount: int
    valid_from: date_type
    note: Optional[str] = None
    created_by: Optional[str] = None  # empty on a row made by the migration (valid_from 2000-01-01)
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    is_current: bool = False  # the row that gives the price today
    is_future: bool = False  # valid_from is after today
    changes: List[GroupPriceChange] = Field(default_factory=list)  # the writes of THIS valid_from, newest first


class GroupPriceCancelled(BaseModel):
    """A scheduled group price that was taken back (``DELETE /org/groups/{id}/prices/{row_id}``): the row is gone."""

    at: Optional[str] = None  # naive UTC, ISO
    actor_name: Optional[str] = None
    amount: Optional[int] = None
    valid_from: Optional[date_type] = None
    note: Optional[str] = None


class GroupPricesOut(BaseModel):
    """``GET /org/groups/{id}/prices``: the price today and the history, newest first."""

    group_id: str
    group_name: str
    as_of: str  # the business today (Tashkent) the ``current`` is for
    period_mode: PeriodMode
    price_label: str  # «Оплата в месяц» (calendar) | «Стоимость пакета» (lessons)
    current: Optional[GroupPriceCurrent] = None
    history: List[GroupPriceRow] = Field(default_factory=list)
    cancelled: List[GroupPriceCancelled] = Field(default_factory=list)  # the last 20 scheduled prices taken back, newest first


class GroupPriceCurrentLite(BaseModel):
    amount: int
    valid_from: date_type


class GroupPriceListScheduled(BaseModel):
    row_id: str  # for DELETE /org/groups/{group_id}/prices/{row_id}
    amount: int
    valid_from: date_type


class GroupPriceListItem(BaseModel):
    group_id: str
    name: str
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None
    kind: str  # Group.lesson_type: group | mini_group | pair | individual
    current: Optional[GroupPriceCurrentLite] = None  # null - the price is not set
    scheduled: Optional[GroupPriceListScheduled] = None  # the NEXT scheduled price (valid_from after today)
    scheduled_count: int = 0  # how many scheduled prices the group has (the next one is shown)
    scheduled_override_count: int = 0  # #126: active students with an individual price that starts AFTER today
    students_with_override: int = 0  # active students with an individual price in force today


class GroupPriceList(BaseModel):
    """``GET /org/group-prices``: the prices of every non-archived group of the centre."""

    as_of: str  # the business today (Tashkent)
    period_mode: PeriodMode
    price_label: str  # «Оплата в месяц» (calendar) | «Стоимость пакета» (lessons)
    items: List[GroupPriceListItem] = Field(default_factory=list)  # by name


class PriceImpactSheet(BaseModel):
    sheet_id: str
    month: str
    teacher_id: str
    teacher_name: Optional[str] = None


class PriceImpact(BaseModel):
    """What a change of the group price touches.  Payments already entered never change."""

    students_following: int  # active students who follow the group price and so get the new one
    students_with_override: int  # active students with an individual price: not touched
    stale_payroll_sheets: int  # saved payroll sheets that would turn «данные изменились»
    stale_sheets: List[PriceImpactSheet] = Field(default_factory=list)  # the sheets themselves: the owner only
    # LOGIC-A (decision 4): the money effect, computed by the ledger "what if"; null for a caller without access to money.
    money: Optional["PriceImpactMoney"] = None


class GroupPriceUpdate(BaseModel):
    amount: int = Field(..., ge=0, le=MAX_MONEY)
    valid_from: Optional[date_type] = None  # default: today (Tashkent); a past day is allowed
    note: Optional[str] = Field(None, max_length=200)


class GroupPriceChanged(BaseModel):
    """``PUT /org/groups/{id}/price``: the new state + the impact; with ``?dry_run=1`` only the impact."""

    dry_run: bool = False
    impact: PriceImpact
    group_id: Optional[str] = None
    group_name: Optional[str] = None
    as_of: Optional[str] = None
    period_mode: Optional[PeriodMode] = None
    price_label: Optional[str] = None
    current: Optional[GroupPriceCurrent] = None
    history: Optional[List[GroupPriceRow]] = None
    cancelled: Optional[List[GroupPriceCancelled]] = None


class PriceOverrideChange(BaseModel):
    """One write of the individual price of a day (the audit trail, ``group_audit``)."""

    at: Optional[str] = None  # naive UTC, ISO
    actor_name: Optional[str] = None
    old_amount: Optional[int] = None  # the EFFECTIVE price that day just before the write; null - none
    old_source: Optional[PriceSource] = None  # where it came from: group / override / none
    new_amount: Optional[int] = None  # null = "from this day the price of the group again"
    reason: Optional[str] = None


class PriceOverrideCancelled(BaseModel):
    """A scheduled individual price that was taken back (``DELETE /org/memberships/{id}/price-override/{row_id}``)."""

    at: Optional[str] = None
    actor_name: Optional[str] = None
    amount: Optional[int] = None  # null: it was a scheduled "the group price again"
    valid_from: Optional[date_type] = None
    reason: Optional[str] = None


class PriceOverrideRow(BaseModel):
    id: str
    amount: Optional[int] = None  # null = "from this day the price of the group again"
    valid_from: date_type
    reason: Optional[str] = None
    created_by: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    is_current: bool = False
    is_future: bool = False
    changes: List[PriceOverrideChange] = Field(default_factory=list)  # the writes of THIS valid_from, newest first


class MembershipPriceOut(BaseModel):
    """``GET /org/memberships/{id}/price-override``: the price today and the history of the exceptions."""

    membership_id: str
    group_id: str
    as_of: str
    period_mode: PeriodMode
    price_label: str
    price: PriceOut
    history: List[PriceOverrideRow] = Field(default_factory=list)
    cancelled: List[PriceOverrideCancelled] = Field(default_factory=list)  # the last 20, newest first


class PriceOverrideUpdate(BaseModel):
    """``amount`` a sum: the individual price from ``valid_from`` (``reason`` 3..200 characters is mandatory);
    ``amount`` null: from that day the price of the group again (``reason`` optional)."""

    amount: Optional[int] = Field(None, ge=0, le=MAX_MONEY)
    valid_from: Optional[date_type] = None
    reason: Optional[str] = Field(None, max_length=200)


class PriceOverrideChanged(MembershipPriceOut):
    row: "MembershipPaymentRow"
    dry_run: bool = False
    # LOGIC-A: what this price of this membership touches - the same block as the group price: the counters, the stale sheets (the list for
    # the owner only) and ``money`` (the ledger "what if": debt / advance / «оплачено до» of this membership).  ``null`` on GET / cancel.
    impact: Optional["PriceImpact"] = None


class PaymentCreate(BaseModel):
    """PAY-2: amount only.  ``amount`` 1..``MAX_MONEY`` (422 ``AMOUNT_RANGE``), ``paid_on`` not in the future and not older
    than 3 years (422 ``PAID_ON_RANGE``).  ``method``: absent -> ``other`` (old front, one release); ``unknown`` ->
    422 ``METHOD_REQUIRED``.  ``valid_from`` / ``valid_to`` / ``lessons_count`` are accepted and IGNORED (one release).
    The header ``Idempotency-Key: <uuid>``: a second POST with the same key creates nothing and answers 409
    ``DUPLICATE_PAYMENT`` whose detail carries ``payment_id`` of the payment the first call created."""

    amount: int
    paid_on: date
    method: Optional[PaymentMethod] = None
    valid_from: Optional[date] = Field(None, json_schema_extra={"deprecated": True})
    valid_to: Optional[date] = Field(None, json_schema_extra={"deprecated": True})
    lessons_count: Optional[int] = Field(None, ge=1, le=120, json_schema_extra={"deprecated": True})
    note: Optional[str] = Field(None, max_length=500)

    @field_validator("note")
    @classmethod
    def _blank_note(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        return value or None


class PaymentReplace(PaymentCreate):
    reason: Optional[str] = Field(None, max_length=500)


class PaymentVoid(BaseModel):
    reason: str = Field(..., max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_length(cls, value: str) -> str:
        value = (value or "").strip()
        if len(value) < 3:
            raise ValueError("Причина отмены: от 3 до 500 символов")
        return value


class PaymentWarningOut(BaseModel):
    code: PayWarningCode
    message: str


class PaymentCreated(BaseModel):
    """PAY-2: ``allocation`` (where the money went) is always filled by the server."""

    payment: PaymentOut
    allocation: Optional[AllocationSummary] = None
    warnings: List[PaymentWarningOut]
    row: MembershipPaymentRow


class PaymentVoided(BaseModel):
    payment: PaymentOut
    row: MembershipPaymentRow


class QuotePartOut(BaseModel):
    """One period of the centre inside the quoted payment.

    ``lessons`` / ``period_lessons`` are filled for a period prorated by lessons (the group's lessons in
    the covered part / in the whole period); ``None`` for a complete period and for one counted by days.
    """

    from_: date = Field(..., alias="from")
    to: date
    period_from: date
    period_to: date
    lessons: Optional[int] = None
    period_lessons: Optional[int] = None
    amount: int

    model_config = {"populate_by_name": True}


class PaymentQuote(BaseModel):
    """Suggested dates and amount of a payment; nothing is written."""

    mode: PeriodMode
    billing_anchor: BillingAnchor
    billing_day: int
    paid_on: date
    valid_from: date
    valid_to: Optional[date] = None
    lessons_count: Optional[int] = None
    partial: bool
    monthly_fee: Optional[int] = None
    # The price of the membership at the start of the period (the base of the suggestion).  ``monthly_fee`` above is
    # the sum that was actually used: the typed one when the caller sent ``monthly_fee``, else ``price.amount``.
    price: PriceOut = Field(default_factory=PriceOut)
    suggested_amount: Optional[int] = None
    basis: QuoteBasis
    parts: List[QuotePartOut] = Field(default_factory=list)
    explanation: str


class ReceiptOut(BaseModel):
    """Everything the printed receipt shows (no other payment data)."""

    receipt_no: int
    org_name: str
    org_city: Optional[str] = None
    org_phone: Optional[str] = None
    student_name: str
    display_id: Optional[str] = None
    group_name: str
    amount: int
    amount_words: str
    currency: str
    paid_on: date
    # LEGACY (receipts of payments entered before PAY-2); on a new one the SHIM of ``PaymentOut`` for one release (first
    # period reached / paid until) - the new front reads ``allocations`` instead.
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    lessons_count: Optional[int] = None
    partial_period: bool = False
    # PAY-2: the snapshot "where the money went at the moment of the payment" (null on a legacy receipt).
    method: PaymentMethod = "unknown"
    mode: Optional[PeriodMode] = None
    allocations: Optional[List[Allocation]] = None
    debt_before: Optional[int] = None
    debt_after: Optional[int] = None
    advance_after: Optional[int] = None
    paid_until_before: Optional[date] = None
    paid_until_after: Optional[date] = None
    price_snapshot: Optional[SnapshotPrice] = None
    note: Optional[str] = None
    accepted_by_name: Optional[str] = None
    created_at: Optional[str] = None
    voided_at: Optional[str] = None
    void_reason: Optional[str] = None
    replaced_by_receipt_no: Optional[int] = None
    replaces_receipt_no: Optional[int] = None
    replaces_amount: Optional[int] = None  # LOGIC-A: the sum of the payment this one corrects («исправлена: было 450 000»)


# ============================================================================================================
# PAY-2 contract: preview, write-off, refund, statement, receipts report (design §4.1-§4.8)
# ============================================================================================================


class PreviewDue(PayModel):
    """What the student owes today (before the typed amount)."""

    debt: int = 0
    past_debt: int = 0  # unpaid part of the periods that have ended
    current_due: int = 0  # unpaid part of the periods that have begun and still run
    advance: int = 0
    segments: List[Segment] = Field(default_factory=list)  # begun segments with ``unpaid`` > 0, oldest first
    paid_until: Optional[date_type] = None
    days_overdue: Optional[int] = None


class PreviewLessons(PayModel):
    """Lessons mode: what the money buys in lessons."""

    covered_before: int = 0
    covered_after: int = 0
    left_after: int = 0
    unpaid_after: int = 0


class PriceAhead(PayModel):
    """A period the advance reaches whose price differs from today's price (D8: a scheduled price change makes the
    same money buy fewer / more days)."""

    period_from: date_type
    period_to: date_type
    amount: int
    current_amount: Optional[int] = None


class PreviewResult(PayModel):
    allocations: List[Allocation] = Field(default_factory=list)
    debt_after: int = 0
    past_debt_after: int = 0
    current_due_after: int = 0
    advance_after: int = 0
    paid_until_before: Optional[date_type] = None
    paid_until_after: Optional[date_type] = None
    status_after: MembershipStatus = "none"
    days_overdue_after: Optional[int] = None
    partial_credit_after: int = 0
    lessons: Optional[PreviewLessons] = None
    summary_text: str = ""
    price_ahead: List[PriceAhead] = Field(default_factory=list)


class PaymentPreview(PayModel):
    """``GET /org/memberships/{id}/payments/preview``: «что закроет» - nothing is written."""

    as_of: date_type
    mode: PeriodMode = "calendar"
    price: PriceOut = Field(default_factory=PriceOut)
    due: PreviewDue = Field(default_factory=PreviewDue)
    suggested_amount: int = 0  # the whole debt (calendar) / to the end of the package (lessons); 0 - nothing is due
    amount: Optional[int] = None  # the typed amount (echo); null - not typed
    result: Optional[PreviewResult] = None  # null when ``amount`` was not given
    warnings: List[PaymentWarningOut] = Field(default_factory=list)


class WriteoffCreate(BaseModel):
    """``amount`` OR ``all: true`` (the whole debt).  ``reason`` 3..500 characters is mandatory."""

    model_config = ConfigDict(populate_by_name=True)

    amount: Optional[int] = None
    all_debt: bool = Field(False, alias="all")
    reason: str = Field(..., max_length=500)
    effective_on: Optional[date_type] = None  # default: the business today

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return (value or "").strip()


class RecordVoid(BaseModel):
    """Cancel a write-off / a refund: the reason 3..500 characters."""

    reason: str = Field(..., max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_length(cls, value: str) -> str:
        value = (value or "").strip()
        if len(value) < 3:
            raise ValueError("Причина отмены: от 3 до 500 символов")
        return value


class WriteoffOut(PayModel):
    id: str
    membership_id: str
    amount: int
    reason: str
    effective_on: date_type
    created_by: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    voided_at: Optional[str] = None
    voided_by: Optional[str] = None
    voided_by_name: Optional[str] = None
    void_reason: Optional[str] = None


class WriteoffCreated(PayModel):
    writeoff: WriteoffOut
    debt_before: int = 0
    debt_after: int = 0
    row: MembershipPaymentRow


class WriteoffVoided(PayModel):
    writeoff: WriteoffOut
    row: MembershipPaymentRow


class RefundCreate(BaseModel):
    amount: int
    method: Optional[NewPaymentMethod] = None  # mandatory: a missing one is 422 METHOD_REQUIRED
    reason: Optional[str] = Field(None, max_length=500)
    note: Optional[str] = Field(None, max_length=500)
    effective_on: Optional[date_type] = None  # default: the business today


class RefundOut(PayModel):
    id: str
    membership_id: str
    amount: int
    method: PaymentMethod = "other"
    reason: Optional[str] = None
    note: Optional[str] = None
    effective_on: date_type
    created_by: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    voided_at: Optional[str] = None
    voided_by: Optional[str] = None
    voided_by_name: Optional[str] = None
    void_reason: Optional[str] = None


class RefundCreated(PayModel):
    refund: RefundOut
    advance_before: int = 0
    advance_after: int = 0
    row: MembershipPaymentRow


class RefundVoided(PayModel):
    refund: RefundOut
    row: MembershipPaymentRow


# ---- statement ("выписка") ---------------------------------------------------------------------------------


class StatementMembership(PayModel):
    membership_id: str
    student_id: str
    student_name: str
    display_id: Optional[str] = None
    group_id: str
    group_name: str
    teacher_name: Optional[str] = None
    is_active: bool = True


class StatementSummary(PayModel):
    """Always for the whole accounting period up to today, whatever ``from`` / ``to`` the rows were cut by."""

    charged: int = 0
    paid: int = 0  # payments (gross, refunds not taken off)
    transferred_in: int = 0
    written_off: int = 0
    refunded: int = 0
    transferred_out: int = 0
    debt: int = 0
    advance: int = 0
    paid_until: Optional[date_type] = None
    status: MembershipStatus = "none"
    reconciles: bool = True  # charged - paid - transferred_in - written_off + refunded + transferred_out == debt - advance


class StatementDetails(PayModel):
    """The part of a row that depends on its ``type`` (the other fields are null)."""

    segment: Optional[Segment] = None  # charge
    payment_id: Optional[str] = None  # payment
    receipt_no: Optional[int] = None
    method: Optional[PaymentMethod] = None  # payment, refund
    created_by_name: Optional[str] = None  # payment, writeoff, refund
    note: Optional[str] = None
    allocations: List[Allocation] = Field(default_factory=list)  # payment, transfer_in: where the money went
    writeoff_id: Optional[str] = None  # writeoff
    refund_id: Optional[str] = None  # refund
    reason: Optional[str] = None  # writeoff, refund
    from_group_name: Optional[str] = None  # transfer_in
    from_membership_id: Optional[str] = None
    to_group_name: Optional[str] = None  # transfer_out
    to_membership_id: Optional[str] = None
    # LOGIC-A: the correction chain of a payment row.  A correction shows «исправлена: было 450 000»; the voided original
    # (``include_voided``) links to the receipt that replaced it.
    replaces_receipt_no: Optional[int] = None
    replaces_amount: Optional[int] = None
    replaced_by_receipt_no: Optional[int] = None


class StatementRow(PayModel):
    seq: int
    date: date_type
    type: StatementRowType
    title: str
    debit: int = 0  # charged
    credit: int = 0  # money in (payment, transfer in, write-off)
    outflow: int = 0  # money out (refund, transfer out)
    balance_after: Optional[int] = None  # negative = debt, positive = advance; null on a voided row
    balance_text: Optional[str] = None  # «Долг 500 000» | «Аванс 21 000» | «0» (the «Баланс» column)
    voided_at: Optional[str] = None
    void_reason: Optional[str] = None
    voided_by_name: Optional[str] = None
    details: StatementDetails = Field(default_factory=StatementDetails)


class StatementOut(PayModel):
    """``GET /org/memberships/{id}/statement``.  The last row's ``balance_after`` equals ``summary.advance - summary.debt``
    (rows cut by ``from`` / ``to`` keep the running total of the whole history)."""

    membership: StatementMembership
    as_of: date_type
    mode: PeriodMode = "calendar"
    accounting_start: date_type
    summary: StatementSummary
    rows: List[StatementRow] = Field(default_factory=list)
    voided: List[StatementRow] = Field(default_factory=list)  # only with ``include_voided=true``
    upcoming: List[Segment] = Field(default_factory=list)  # future periods that already hold money


# ---- receipts of the centre ("Поступления") ---------------------------------------------------------------


class ReceiptsTotals(PayModel):
    received: int = 0  # money payments entered in the range (transfers are not receipts)
    refunded: int = 0  # refunds dated in the range
    net_received: int = 0  # received - refunded
    refunded_from_received: int = 0  # the part of the REFUNDS that was taken off the payments of the range (LIFO)
    transferred_from_received: int = 0  # the part of the TRANSFERS OUT (money that moved to another group) taken off them
    to_past_debt: int = 0  # net distribution of the range's payments by bucket (relative to each payment's date)
    to_current: int = 0
    to_advance: int = 0
    written_off: int = 0
    payments_count: int = 0


class ReceiptsByMethod(PayModel):
    method: PaymentMethod
    amount: int = 0
    count: int = 0


class ReceiptsGroupRow(PayModel):
    group_id: str
    group_name: str
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None
    received: int = 0
    to_past_debt: int = 0
    to_current: int = 0
    to_advance: int = 0
    written_off: int = 0
    refunded: int = 0
    count: int = 0


class ReceiptsTeacherRow(PayModel):
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None
    received: int = 0
    to_past_debt: int = 0
    to_current: int = 0
    to_advance: int = 0
    written_off: int = 0
    refunded: int = 0
    count: int = 0


class ReceiptsSeriesPoint(PayModel):
    bucket: str  # "2026-10-21" (day) | "2026-10" (month)
    received: int = 0
    by_method: Dict[str, int] = Field(default_factory=dict)
    to_past_debt: int = 0
    to_current: int = 0
    to_advance: int = 0
    written_off: int = 0
    refunded: int = 0
    count: int = 0


class ReceiptsPosition(PayModel):
    """Where the whole centre stands TODAY (not limited to the range; includes students who left and archived ones)."""

    debt_total: int = 0
    past_debt_total: int = 0
    advance_total: int = 0
    memberships_with_debt: int = 0
    students_with_debt: int = 0
    memberships_with_advance: int = 0


class ReconciliationCheck(PayModel):
    code: Literal[
        "received_eq_allocated_plus_refunded", "charged_minus_paid_eq_debt_minus_advance", "transfers_net_zero"
    ]
    left: int
    right: int
    diff: int


class Reconciliation(PayModel):
    ok: bool = True
    checks: List[ReconciliationCheck] = Field(default_factory=list)


class ReceiptsReport(PayModel):
    """``GET /org/payments/receipts``.  Range <= 366 days; ``group_by=day`` <= 92 days (422 ``RANGE_TOO_LONG``)."""

    as_of: date_type
    from_: date_type = Field(..., alias="from")
    to: date_type
    group_by: Literal["day", "month"] = "day"
    totals: ReceiptsTotals
    by_method: List[ReceiptsByMethod] = Field(default_factory=list)
    by_group: List[ReceiptsGroupRow] = Field(default_factory=list)
    by_teacher: List[ReceiptsTeacherRow] = Field(default_factory=list)
    series: List[ReceiptsSeriesPoint] = Field(default_factory=list)
    position: ReceiptsPosition
    reconciliation: Reconciliation

    model_config = ConfigDict(populate_by_name=True, json_schema_serialization_defaults_required=True)


class ReceiptItem(PayModel):
    """One record behind a number of the report (drill-down)."""

    type: ReceiptItemType
    id: str
    date: date_type
    amount: int  # the whole record
    column_amount: int  # the part of it that is in the clicked column
    student_id: str
    student_name: str
    membership_id: str
    group_name: str
    teacher_name: Optional[str] = None
    method: Optional[PaymentMethod] = None
    receipt_no: Optional[int] = None
    allocations: List[Allocation] = Field(default_factory=list)


class ReceiptItems(PayModel):
    items: List[ReceiptItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class BalanceRow(PayModel):
    """A short membership row of «Долги / Авансы» (also memberships of students who left / archived)."""

    membership_id: str
    student_id: str
    student_name: str
    display_id: Optional[str] = None
    group_id: str
    group_name: str
    teacher_id: Optional[str] = None
    teacher_name: Optional[str] = None
    is_active: bool = True
    student_archived: bool = False
    status: MembershipStatus = "none"
    paid_until: Optional[date_type] = None
    debt: int = 0
    past_debt: int = 0
    current_due: int = 0
    advance: int = 0
    refundable: int = 0


class BalancesPage(PayModel):
    items: List[BalanceRow] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


# ---- receipt snapshot (``payment_receipt_snapshots.snapshot``) -----------------------------------------------


class ReceiptSnapshot(PayModel):
    """Where the money went AT THE MOMENT of the payment.  The printed receipt shows it; the statement shows the fact."""

    version: int = 1
    computed_at: str
    business_date: date_type
    amount: int
    method: PaymentMethod = "other"
    allocations: List[Allocation] = Field(default_factory=list)
    debt_before: int = 0
    debt_after: int = 0
    advance_after: int = 0
    paid_until_before: Optional[date_type] = None
    paid_until_after: Optional[date_type] = None
    price: SnapshotPrice = Field(default_factory=SnapshotPrice)
    mode: PeriodMode = "calendar"


class AccountingStartImpact(PayModel):
    """What moving the start of the accounting does (all centre-wide, today): new debts AND changes of the advance / of what
    can be refunded (an earlier start charges more, so advances shrink)."""

    memberships_with_new_debt: int = 0
    debt_delta: int = 0
    memberships_with_changed_advance: int = 0
    advance_delta: int = 0
    refundable_delta: int = 0


class PaymentSettingsChanged(PaymentSettings):
    """``PUT /org/settings/payments``.  ``?dry_run=true`` writes nothing: the CURRENT settings + the impact of the
    requested ``accounting_start_date`` (``impact``); a real write returns the new settings and ``impact`` null."""

    dry_run: bool = False
    impact: Optional[AccountingStartImpact] = None
    # LOGIC-A (F12): with ``dry_run`` and a change of the billing mode: who switches and when (null otherwise).
    regime_impact: Optional["RegimeImpact"] = None


# ================================================================================================================
# LOGIC-A (pack «деньги»): opening balance, pause, leave / archive with money, price impact, regime impact, lesson type.
# FROZEN with ``artifacts/partner_doc_check/logicA_contract.json`` and ``tests/fixtures/logicA``: a change = change the
# spec (``logicA_spec.md``) and these schemas first.
# ================================================================================================================


class LogicAErrorDetail(PayErrorDetail):
    """Error body of the LOGIC-A endpoints: ``PayErrorDetail`` + the lists some codes carry.

    Codes: ``OPENING_RANGE`` / ``PAUSE_RANGE`` / ``PAUSE_LOCKED`` / ``NOTHING_TO_PAY`` / ``PAYMENT_EXCEEDS_DEBT`` (``debt``) /
    ``LESSON_TYPE_RANGE`` / ``METHOD_REQUIRED`` / ``REASON_REQUIRED`` (422); ``OPENING_NOT_ALLOWED`` / ``PAUSE_OVERLAP`` /
    ``PAUSE_OVERLAPS_ATTENDED`` (``dates``) / ``PAUSE_BREAKS_REFUND`` / ``MEMBERSHIP_CLOSED`` / ``BALANCE_CHANGED`` (``fresh``) /
    ``PRICE_BACKDATE_WITH_PAYMENTS`` (``students``) / ``PRICE_MARKER`` / ``PRICE_CONFLICT`` / ``VOID_BREAKS_REFUND`` (409).
    """

    dates: Optional[List[date_type]] = None  # PAUSE_OVERLAPS_ATTENDED
    students: Optional[List["PriceBlockedStudent"]] = None  # PRICE_BACKDATE_WITH_PAYMENTS (first 20)
    fresh: Optional[List["FreshBalance"]] = None  # BALANCE_CHANGED: the numbers as they are now


class LogicAError(PayModel):
    detail: LogicAErrorDetail


class FreshBalance(PayModel):
    membership_id: str
    debt: int = 0
    advance: int = 0
    refundable: int = 0


class PriceBlockedStudent(PayModel):
    membership_id: str
    student_name: str


class ImpactMoney(PayModel):
    """The money effect of a change of one membership (the ledger before / after, same day)."""

    debt_before: int = 0
    debt_after: int = 0
    advance_before: int = 0
    advance_after: int = 0
    paid_until_before: Optional[date_type] = None
    paid_until_after: Optional[date_type] = None
    charged_delta: int = 0
    refundable_before: int = 0
    refundable_after: int = 0


class PausedRange(BaseModel):
    model_config = ConfigDict(populate_by_name=True, json_schema_serialization_defaults_required=True)

    from_: date_type = Field(..., alias="from")
    to: date_type


# ---- opening balance ("остаток на начало") ----------------------------------------------------------


class OpeningBrief(PayModel):
    id: str
    paid_until: Optional[date_type] = None
    debt_amount: Optional[int] = None
    debt_on: Optional[date_type] = None


class OpeningIn(BaseModel):
    """The opening balance of a membership: «оплачено до» and / or «долг X на дату T» (the debt needs its date)."""

    paid_until: Optional[date_type] = None
    debt_amount: Optional[int] = None  # 1..10^10
    debt_on: Optional[date_type] = None  # not after the first day of the stay; default in an import: the day before
    reason: Optional[str] = Field(None, max_length=500)  # m13: required (3..500) when the opening it replaces sits on a membership that has money


class OpeningPut(OpeningIn):
    """``PUT /org/memberships/{id}/opening-balance``: all three empty = void of the live one.  ``reason`` is required
    (3..500) when the membership already has money, write-offs or refunds."""

    note: Optional[str] = Field(None, max_length=300)


class OpeningOut(PayModel):
    id: str
    membership_id: str
    paid_until: Optional[date_type] = None
    debt_amount: Optional[int] = None
    debt_on: Optional[date_type] = None
    note: Optional[str] = None
    source: Literal["import", "manual", "add"] = "manual"
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    voided_at: Optional[str] = None
    void_reason: Optional[str] = None


class OpeningSaved(PayModel):
    """``opening`` null: the call voided the live one (or ``dry_run`` of such a call)."""

    opening: Optional[OpeningOut] = None
    impact: ImpactMoney
    dry_run: bool = False
    row: MembershipPaymentRow


class OpeningVoided(PayModel):
    opening: OpeningOut
    impact: ImpactMoney
    row: MembershipPaymentRow


# ---- pause ------------------------------------------------------------------------------------------


class PauseCreate(BaseModel):
    first_day: date_type
    last_day: date_type
    reason: Optional[str] = Field(None, max_length=300)


class PausePatch(BaseModel):
    first_day: Optional[date_type] = None
    last_day: Optional[date_type] = None
    reason: Optional[str] = Field(None, max_length=300)


class PauseOut(PayModel):
    id: str
    membership_id: str
    first_day: date_type
    last_day: date_type
    days: int
    reason: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    voided_at: Optional[str] = None
    void_reason: Optional[str] = None


class PauseLessonMark(PayModel):
    date: date_type
    marks: Optional[str] = None  # absent | excused | present | late | null (no mark yet)


class PauseLessons(PayModel):
    excluded: List[PauseLessonMark] = Field(default_factory=list)  # lessons that will no longer be counted
    attended: List[PauseLessonMark] = Field(default_factory=list)  # present / late: a pause over them is refused


class PausePayroll(PayModel):
    months: List[str] = Field(default_factory=list)  # "YYYY-MM"
    saved_sheets_changed: int = 0


class PauseSaved(PayModel):
    pause: PauseOut
    impact: Optional[ImpactMoney] = None  # null without access to money
    lessons: PauseLessons
    payroll: Optional[PausePayroll] = None  # the owner only
    dry_run: bool = False
    row: Optional[MembershipPaymentRow] = None  # null on ``dry_run``


class PauseVoided(PayModel):
    pause: PauseOut
    impact: Optional[ImpactMoney] = None
    row: Optional[MembershipPaymentRow] = None


class PauseVoid(RecordVoid):
    """Cancel a pause: the reason 3..500 characters."""


# ---- leave / archive with money ---------------------------------------------------------------------

SettleAction = Literal["keep", "writeoff", "refund"]


class LeaveMoney(PayModel):
    debt: int = 0
    advance: int = 0
    refundable: int = 0
    paid_until: Optional[date_type] = None
    charged_after_leave: int = 0
    individual_price_ends: bool = False


class LeaveMembership(PayModel):
    membership_id: str
    group_id: str
    group_name: str
    money: Optional[LeaveMoney] = None  # null without access to money
    has_open_balance: bool = False
    allowed: List[SettleAction] = Field(default_factory=lambda: ["keep"])


class LeavePreview(PayModel):
    as_of: date_type
    memberships: List[LeaveMembership] = Field(default_factory=list)


class SettleItem(BaseModel):
    membership_id: str
    action: SettleAction = "keep"
    expected_refundable: Optional[int] = None  # refund
    expected_debt: Optional[int] = None  # writeoff
    method: Optional[NewPaymentMethod] = None  # refund: mandatory
    reason: Optional[str] = Field(None, max_length=500)  # writeoff: mandatory (3..500)


class LeaveRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=300)
    settle: List[SettleItem] = Field(default_factory=list, max_length=200)


class SettledOut(PayModel):
    membership_id: str
    action: SettleAction
    amount: int = 0
    refund_id: Optional[str] = None
    writeoff_id: Optional[str] = None


class LeaveResult(PayModel):
    message: str
    settled: List[SettledOut] = Field(default_factory=list)


# ---- price impact (decision 4) ----------------------------------------------------------------------


class PriceImpactItem(PayModel):
    membership_id: str
    student_name: str
    debt_before: int = 0
    debt_after: int = 0
    advance_before: int = 0
    advance_after: int = 0
    paid_until_before: Optional[date_type] = None
    paid_until_after: Optional[date_type] = None
    has_money: bool = False


class PriceImpactMoney(PayModel):
    memberships_with_new_debt: int = 0
    debt_delta: int = 0
    memberships_with_less_advance: int = 0
    advance_delta: int = 0
    days_lost_total: int = 0
    backdated: bool = False
    items: List[PriceImpactItem] = Field(default_factory=list)  # at most 50


# ---- billing mode change (F12) ----------------------------------------------------------------------


class RegimeImpact(PayModel):
    students_affected: int = 0
    first_switch: Optional[date_type] = None
    latest_switch: Optional[date_type] = None
    defer: bool = True


# ---- lesson type of a group (F14) -------------------------------------------------------------------

LessonTypeName = Literal["group", "mini_group", "pair", "individual"]


class LessonTypeRow(PayModel):
    valid_from: date_type
    lesson_type: LessonTypeName
    created_by_name: Optional[str] = None


class GroupLessonTypes(PayModel):
    current: LessonTypeName
    history: List[LessonTypeRow] = Field(default_factory=list)


class LessonTypePut(BaseModel):
    lesson_type: LessonTypeName
    valid_from: Optional[date_type] = None  # default: today


class LessonTypeSheet(PayModel):
    sheet_id: str
    month: str
    teacher_name: Optional[str] = None


class LessonTypeImpact(PayModel):
    stale_payroll_sheets: int = 0
    stale_sheets: List[LessonTypeSheet] = Field(default_factory=list)


class GroupLessonTypeChanged(GroupLessonTypes):
    dry_run: bool = False
    impact: Optional[LessonTypeImpact] = None  # the owner only


for _model in (MembershipPaymentRow, PriceImpact, PaymentSettingsChanged, LogicAErrorDetail, LogicAError, OpeningSaved,
               OpeningVoided, PauseSaved, PauseVoided):
    _model.model_rebuild()
