"""ALGO - request/response schemas of the payments API (N13 P2).  snake_case."""
from __future__ import annotations

from datetime import date
from datetime import date as date_type
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

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
    ``valid_from`` - the day the row that gave the price started.  The sum means "Цена в месяц" (calendar
    periods) or "Цена пакета" (lesson packages) as the centre's billing mode says.
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
                           "opening_debt", "opening_paid_until", "pause", "recalc"]  # LOGIC-A: the opening balance and a pause (info row, no money)
ReceiptColumn = Literal["received", "to_past_debt", "to_current", "to_advance", "written_off", "refunded"]
ReceiptItemType = Literal["payment", "writeoff", "refund"]
PayWarningCode = Literal[
    "price_missing", "horizon_capped", "large_overpay", "student_archived", "left_debt_only", "price_change_ahead",
    "paid_over_60_days_ago",
    "payment_repeated",      # Money-1 B9 (ДЫРА-12): the same sum was entered for this student a moment ago (a warning, never a ban)
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
    unpaid: Optional[int] = None  # PAYMENT_EXCEEDS_DEBT: the most that can be accepted from a student who left (everything he owes)
    refundable: Optional[int] = None  # REFUND_EXCEEDS_ADVANCE: the most that can be returned
    payment_id: Optional[str] = None  # DUPLICATE_PAYMENT: the payment the same Idempotency-Key created first


class PayError(PayModel):
    detail: PayErrorDetail


class SegmentLessons(PayModel):
    """A period prorated by the lessons of the group: lessons in the charged part / in the whole period."""

    in_part: int
    in_period: int


class SegmentPart(PayModel):
    """PAY-DUE 10.11: the unpaid part of ONE segment by name (``debt`` / ``due``); a package segment may hold both (the debt first): the statement and the Excel read the same field."""

    name: Literal["debt", "due"]
    unpaid: int
    due_until: Optional[date_type] = None
    lessons: Optional[int] = None


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
    recalculated: int = 0  # PAY-F: deductions for the lessons the centre cancelled that lie on the segment
    unpaid: int = 0  # charge - paid - written_off - recalculated
    remaining: int = 0  # LOGIC-A: the same as ``unpaid`` under the name the month header uses (charge - paid - written_off - recalculated)
    covered_days: int = 0  # floor(money * billable_days / charge); a day is paid only when paid in full
    covered_through: Optional[date_type] = None  # last paid day of the segment (null - none)
    partial_credit: int = 0  # money "inside" the first unpaid day; counts towards the next payment
    state: SegmentState = "open"
    # relative to ``as_of`` (today): the period has ended / is running / has not begun (same rule as ``Allocation.bucket``)
    bucket: SegmentBucket = "current"
    parts: List[SegmentPart] = Field(default_factory=list)   # PAY-DUE 10.11: the named unpaid part(s) of this segment (empty when nothing is unpaid)


class Allocation(PayModel):
    """A piece of one payment in one segment ("where the money went")."""

    segment_id: str
    period_from: date_type
    period_to: date_type
    amount: int
    days: int  # days this piece adds to the segment's paid days (the pieces of all payments sum to covered_days)
    bucket: AllocationBucket  # relative to the DATE OF THE PAYMENT: ended before it / running / starts after it
    closes: bool  # after this piece the segment is paid in full
    label: str  # «Погашен долг за 15.09–14.10» | «На текущий период 15.10–14.11» | «Аванс на 15.11–14.12»
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
    unpaid_before: int = 0      # everything unpaid (a debt and the sum «к оплате»), before / after the payment
    unpaid_after: int = 0
    advance_after: int = 0
    paid_until_before: Optional[date_type] = None


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
    # PAY-DUE: the term of payment.  ``pay_due_days`` (calendar modes): the N-th day of a period without a pause is the last day of «К оплате», later the unpaid sum is a «Долг» (0 = a debt from the first day);
    # ``pay_due_lessons`` (packages): how many of the newest unpaid lessons are still «К оплате».  They change only the NAME of an unpaid sum, never a sum.  Only the owner reads and writes them here;
    # the two numbers (read only) also come with ``GET /org/payments/summary`` for the roles that see the money.
    pay_due_days: int = Field(5, ge=0, le=14)
    pay_due_lessons: int = Field(2, ge=0, le=4)
    pay_due_example: Optional["PayDueExample"] = None      # the sentences of the example under the term (the owner's GET and the dry run: for the REQUESTED values); computed by the server, not a stored setting
    # An excuse must be confirmed this many days before the end of the paid
    # period, or when this many lessons are left in a lessons package.
    excuse_deadline_days: int = Field(5, ge=0, le=30)
    excuse_deadline_lessons: int = Field(2, ge=0, le=12)
    admin_can_manage: bool = False
    # PAY-F.  «Уроки, отменённые центром, при помесячной оплате»: false = не пересчитывать (как раньше), true = уменьшать долг сразу (перерасчёт).
    # A change acts for the lessons dated from the day it was made (``recalc_history``); it never rewrites the past.
    centre_cancel_recalc: bool = False
    # Money-1 B8: the sentence of the setting, written by the server (the screen shows it verbatim): «уменьшит долг сразу»
    recalc_hint: Optional[str] = None
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
    pay_due_days: Optional[int] = Field(None, ge=0, le=14)
    pay_due_lessons: Optional[int] = Field(None, ge=0, le=4)
    excuse_deadline_days: Optional[int] = Field(None, ge=0, le=30)
    excuse_deadline_lessons: Optional[int] = Field(None, ge=0, le=12)
    admin_can_manage: Optional[bool] = None
    centre_cancel_recalc: Optional[bool] = None
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
    restored_from_receipt_no: Optional[int] = None   # Money-2 T7: the payment is the RESTORATION of the cancelled payment with this receipt number (``replaces_payment_id`` names it); null = an ordinary payment or correction
    restorable: bool = False                         # Money-2 T7: a CANCELLED payment of this centre that nothing replaced yet - the button «Восстановить оплату» of the statement reads it (the write refuses with the same rule)
    replaced_by_receipt_no: Optional[int] = None     # Money-2 T7: a cancelled payment: the receipt number of the payment that stands in its place (a correction or a restoration)
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


class UpcomingOut(PayModel):
    """PAY-TAILS Б1.4 / Б5.3: the FORECAST of a charge that has not begun (never a part of any sum): ``from`` - the first day of the period, ``amount`` - what the ledger will charge for it (null for a caller without the right to
    money: the date only), ``lessons_text`` («2 урока из 8 в ноябре»), ``due_until`` - the last day of the term (null: «долг с первого дня»), ``text`` - the sentence of the message after adding («Начисление с 24.11: 120 000 сум»),
    ``sub`` - «платить до 28.11» / «долг с первого дня», ``terms_note`` - «сегодня ученик ещё числился в «Математике 7»» when another group moved the day."""

    model_config = ConfigDict(populate_by_name=True, json_schema_serialization_defaults_required=True)

    from_: date_type = Field(..., alias="from")
    amount: Optional[int] = None
    lessons_text: Optional[str] = None
    due_until: Optional[date_type] = None
    text: Optional[str] = None
    sub: Optional[str] = None
    terms_note: Optional[str] = None


class UnpaidFields(BaseModel):
    """PAY-DUE 10.2: how a membership's unpaid sum is NAMED - one meaning on every row (the status list, the balances, the student card): ``debt_amount`` the part whose term has passed (a «Долг»), ``due_amount`` the
    part still within its term («К оплате», until ``due_until``); ``overdue_days`` (K) the age of the oldest debt part (null without a debt; 0 = not overdue yet: a student who left before the term); packages:
    ``due_lessons`` {used, limit} and ``debt_lessons``.  The texts are the server's: ``debt_text`` / ``debt_sub`` and ``due_text`` / ``due_sub`` (which of the two is the main one is the choice of the tab: a view,
    not a rule); ``state`` / ``state_text`` the WORST state of the membership (debt, to_pay, expiring, paused, paid, free, none) for the one label."""

    debt_amount: int = 0
    due_amount: int = 0
    due_until: Optional[date_type] = None
    overdue_days: Optional[int] = None
    due_lessons: Optional[Dict[str, int]] = None
    debt_lessons: int = 0
    debt_text: Optional[str] = None
    debt_sub: Optional[str] = None
    due_text: Optional[str] = None
    due_sub: Optional[str] = None
    debt_also_text: Optional[str] = None      # the second line of a row that holds BOTH sums: «и долг 600 000 сум · просрочено 45 дней» (null: no second sum)
    due_also_text: Optional[str] = None       # «и к оплате 231 000 сум до 24.11» (a package: «и к оплате 100 000 сум · 2 урока · до 24.11»)
    state: Literal["debt", "to_pay", "expiring", "paused", "paid", "free", "none", "starts", "settled", "no_charges"] = "none"
    state_text: Optional[str] = None          # the words of the worst state for EVERY state: «Долг ...», «К оплате ...», «Оплачено до ДД.ММ», «Прощено до ДД.ММ», «Осталось K урока», «Аванс N сум», «На паузе до ДД.ММ», «Бесплатно», «Нет цены», «Начнётся ДД.ММ», «Расчёт закрыт», «Расчётов нет»
    # PAY-TAILS Б5.4: ``starts`` - the charge has not begun («Начнётся 24.11» / «Уроков ещё не было»); ``settled`` - a closed membership with nothing left to pay or to give back («Расчёт закрыт»); ``no_charges`` - a closed
    # membership that never charged anything and never held money («Расчётов нет»).  ``state_sub``: the quiet line of the state (a closed membership «ушёл 28.10»; «из них прощено 300 000 сум»; the forecast «≈ 120 000 сум»).
    # ``covered_by``: what covered the last covered period - money / forgiven / recalc / opening (the word «Прощено до» is not the green of «Оплачено до»); null where nothing is covered.
    state_sub: Optional[str] = None
    covered_by: Optional[Literal["money", "forgiven", "recalc", "opening"]] = None
    left_on: Optional[date_type] = None      # the day a closed membership ended (null for an open one)
    upcoming: Optional[UpcomingOut] = None   # PAY-TAILS Б1.4: the forecast of a charge that has not begun (state ``starts``); never a part of a sum


class MembershipPaymentRow(UnpaidFields):
    # PAY-TAILS Б5.5: the words of the TAB «Аванс» only (``state_text`` / ``state_sub`` are the same on every tab): «Аванс 37 000 сум» and «ушёл 28.10» / «оплачено до 31.12»; null on the other tabs
    tab_text: Optional[str] = None
    tab_sub: Optional[str] = None
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
    # PAY-F: «хватит до ~ДД.ММ» - the day of the last paid lesson by the schedule (a package only; null = no forecast: a month mode, the package is over, no schedule)
    paid_through_forecast: Optional[date] = None
    last_payment: Optional[PaymentOut] = None
    # PAY-2 (always present).  ``paid_until`` may be inside a period (money buys days).  ``debt`` = what the periods
    # that have begun cost minus the money; ``advance`` = money beyond that; never both > 0.  ``partial_credit`` - money
    # inside the first unpaid day.  ``lessons_unpaid`` - held lessons not covered by the money (lessons mode).
    # ``refundable`` - how much of the advance can be returned; ``writeoff_allowed`` - debt > 0 and the caller may.
    unpaid: int = 0             # everything unpaid (= ``debt_amount + due_amount``): a sum, never a name (the word «debt» means only the state after the term); the name is in ``state``
    advance: int = 0
    partial_credit: int = 0
    price_missing: bool = False
    lessons_unpaid: int = 0
    refundable: int = Field(0, description="Устарело: равно advance; экраны читают advance", json_schema_extra={"deprecated": True})
    writeoff_allowed: bool = False
    # LOGIC-A.  ``paused``: today lies inside a live pause (status ``paused``); ``opening``: the live "opening balance" of
    # the membership; ``price_ends_on_leave``: the individual price of the membership was ended by a departure.
    paused: Optional["PausedRange"] = None
    opening: Optional["OpeningBrief"] = None
    price_ends_on_leave: bool = False
    # PAY-DUE (10.2): ONE meaning of the unpaid sum - the fields of ``UnpaidFields`` (the names and the words come from ``pay_due`` and the server's texts; the screen only shows them).
    # ``status`` / ``days_overdue`` of a row that owes something are not the name of the sum: ``status`` is ``active`` there and ``days_overdue`` null (they stay for «Заканчивается оплата» at a zero balance).
    # The tabs are not exclusive: a row with both a debt and a sum within its term is on ``debtors`` and on ``to_pay``; ``expiring`` / ``active`` / ``paused`` are for a row that owes nothing.
    debt_age_bucket: Optional[Literal["to_30", "31_60", "61_90", "over_90"]] = None      # Money-2 T18: the basket of ``overdue_days`` (null without a debt)


class DebtAgeBasket(BaseModel):
    """Money-2 T18: one basket of the age of the debt (``overdue_days``): distinct students, memberships and the sum of ``debt_amount``."""

    bucket: Literal["to_30", "31_60", "61_90", "over_90"]
    students: int = 0
    memberships: int = 0
    debt_total: int = 0


class PaymentsBuckets(BaseModel):
    """DISTINCT STUDENTS per tab of the payments page (the numbers on the tabs); a student with both sums is in ``debtors`` and in ``to_pay``.  The count of ROWS (memberships) of a tab is ``PaymentsSummary.bucket_rows``."""

    debtors: int = 0
    to_pay: int = 0
    expiring: int = 0
    active: int = 0
    paused: int = 0
    advance: int = 0            # PAY-TAILS Б1.1: DISTINCT students with an advance (the tab «Аванс»: open and closed memberships alike)


class PayDueExample(BaseModel):
    """PAY-DUE: the example under the setting of the term, in the words of the server: ``period`` - «Период с 01.12 — платить до 05.12 включительно» (the next period of the centre; 0 days - «Неоплата — долг с первого дня»),
    ``package`` - «Третий неоплаченный урок или 15-й день после урока — уже долг»; ``text`` - the one of the current mode of the centre."""

    period: str
    package: str
    text: str


class DueNearest(BaseModel):
    """PAY-DUE 10.2: the nearest term among the sums that are «К оплате»: the day and the words for the phrase «ближайший срок ...» (no preposition: ``сегодня`` / ``завтра, 21.11`` / ``24.11``)."""

    date: date_type
    text: str


class PaymentsSummary(BaseModel):
    """PAY-DUE 10.2: ONE meaning of the unpaid sum.  ``debt_total`` / ``debtors`` are the parts whose term has passed (``debt_amount`` of the rows), ``due_total`` / ``due_students`` the parts still within
    their term; the tabs overlap (a student with both sums is counted in both), so the number of a tab = the number of such students and the sum of its rows = its card = the part of the centre.  ``debt_total +
    due_total`` = everything unpaid (the sum of ``ledger.debt`` of the same memberships, ``scope = all``)."""

    as_of: str
    ends_today: int
    expiring: int
    none: int
    active: int  # the "paid" tab: statuses active + free
    free: int
    tracked_total: int
    period_mode: PeriodMode
    lessons_per_payment: int
    debt_total: int = 0                 # Σ ``debt_amount`` of the centre (memberships of students who left / archived included)
    debtors: int = 0                    # DISTINCT students with ``debt_amount`` > 0
    due_total: int = 0                  # Σ ``due_amount``
    due_students: int = 0               # DISTINCT students with ``due_amount`` > 0 (a debtor with such a sum too is counted)
    due_nearest: Optional[DueNearest] = None
    advance_total: int = 0
    students_with_advance: int = 0
    advance_left_students: int = 0      # PAY-TAILS Б5.5: DISTINCT students whose advance sits on a CLOSED membership (the toggle «Ушедшие» of the tab «Аванс»)
    advance_text: str = ""              # the sub-line of the card «Аванс», the words of the server: «5 учеников · 4 ушли из группы» / «5 учеников» / «» at zero
    memberships_with_debt: int = 0
    accounting_start_date: Optional[date_type] = None
    paused: int = 0  # LOGIC-A: memberships whose today is inside a pause
    buckets: "PaymentsBuckets" = Field(default_factory=lambda: PaymentsBuckets())
    bucket_rows: "PaymentsBuckets" = Field(default_factory=lambda: PaymentsBuckets())      # the same five tabs counted by memberships (the rows of the list)
    price_missing: int = 0              # the students outside the tabs (a warning line)
    debt_ages: List["DebtAgeBasket"] = Field(default_factory=list)       # Money-2 T18: always the four baskets, in order (by ``overdue_days``, the sum of ``debt_amount``)
    students_paused: int = 0  # DISTINCT students with a membership on pause today (== the students of the «На паузе» tab); ``buckets.paused`` counts memberships
    pay_due_days: int = 5           # PAY-DUE: the term of the centre, READ ONLY here (the roles with the right to the money read it; the settings of it are the owner's)
    pay_due_lessons: int = 2
    pay_due_package_max_days: int = 14        # read only: an unpaid lesson stops being «к оплате» this many days after its date whatever the setting is


class PaymentStatusPage(BaseModel):
    items: List[MembershipPaymentRow]
    total: int                  # memberships (rows) of the filter
    limit: int
    offset: int
    as_of: str
    students: int = 0           # PAY-DUE 10.2: DISTINCT students of the filter - the number of the tab
    amount_total: int = 0       # the sum of the tab's part over ALL the rows of the filter (``debtors``: ``debt_amount``; ``to_pay``: ``due_amount``; ``advance``: ``advance``; the other tabs: 0)
    left_students: int = 0      # PAY-TAILS Б5.5, tab ``advance``: DISTINCT students with an advance on a CLOSED membership among the filter (the other filters of the request kept, ``left_only`` not)
    left_count: int = 0         # PAY-TAILS Б1.2, ``bucket=left_open`` of a group: the number of its closed memberships that still have a settlement (the number of the band «Ушли из группы»)


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
    price_label: str  # «Цена в месяц» (calendar) | «Цена пакета» (lessons)
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
    price_label: str  # «Цена в месяц» (calendar) | «Цена пакета» (lessons)
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
    # Money-1 B2: present only when this is the FIRST price of the group (the day it really acts from and why).
    first_price: Optional["FirstPriceInfo"] = None
    # Money-1 V3: what the person should know BEFORE saving: ``[{"code": "price_unusual", "text": "Вы уверены: 3 000 000 сум? Сейчас 300 000 сум"}]``
    warnings: List[Dict[str, str]] = Field(default_factory=list)
    # Money-1 V3: a correction that released ``students`` blocked by their payments (``correction=true``)
    correction: Optional[Dict[str, Any]] = None


class GroupPriceUpdate(BaseModel):
    amount: StrictInt = Field(..., ge=0, le=MAX_MONEY)   # R3 2: ``true`` / "500000" are 422
    valid_from: Optional[date_type] = None  # default: today (Tashkent); a past day is allowed
    note: Optional[str] = Field(None, max_length=200)
    confirm_unusual: bool = False  # Money-1 V3: the sum is more than twice / less than a half of the price in force (or zero) and the person confirmed it
    correction: bool = False  # Money-1 V3: a typo corrected backdated although the students have payments (with ``correction_reason``)
    correction_reason: Optional[str] = Field(None, max_length=200)


class GroupPriceChanged(BaseModel):
    """``PUT /org/groups/{id}/price``: the new state + the impact; with ``?dry_run=1`` only the impact."""

    dry_run: bool = False
    money_effects: Optional[Dict[str, Any]] = None  # Money-1 V9: the saved sheets of the teachers whose pay the price moves (the sums only for the owner)
    teacher_share: Optional[Dict[str, str]] = None  # Money-1 V9: {"text"} - the pay of the teachers follows the price
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
    money_effects: Optional[Dict[str, Any]] = None  # Money-1 V9 (a write of the price): the saved sheets of the teacher whose pay the price moves
    teacher_share: Optional[Dict[str, str]] = None  # Money-1 V9: {"text"} - the pay of the teacher follows the real price of the student


class PriceOverrideUpdate(BaseModel):
    """``amount`` a sum: the individual price from ``valid_from`` (``reason`` 3..200 characters is mandatory);
    ``amount`` null: from that day the price of the group again (``reason`` optional)."""

    amount: Optional[StrictInt] = Field(None, ge=0, le=MAX_MONEY)   # R3 2
    valid_from: Optional[date_type] = None
    reason: Optional[str] = Field(None, max_length=200)
    confirm_unusual: bool = False  # Money-1 V3
    correction: bool = False
    correction_reason: Optional[str] = Field(None, max_length=200)


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

    amount: StrictInt  # MN-42: ``true`` / "100" / 1.5 are 422, never coerced
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
    reason: Optional[str] = Field(None, max_length=500)  # Money-1 V6: mandatory (3..500) - 422 REASON_REQUIRED without it


class PaymentRestore(BaseModel):
    """Money-2 T7: «Восстановить оплату» - the reason is mandatory (3..500)."""

    reason: str = Field(..., max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_length(cls, value: str) -> str:
        value = (value or "").strip()
        if len(value) < 3:
            raise ValueError("Причина восстановления: от 3 до 500 символов")
        return value


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
    money_effects: Optional[Dict[str, Any]] = None  # Money-1 A3: the saved payroll sheets whose pay the operation moves (``money_effects.run_with_effects``)
    dry_run: bool = False                            # Money-1 R3 #15: the answer of ``?dry_run=true`` - nothing was written, the ids are provisional
    new_debts: List[Dict[str, Any]] = Field(default_factory=list)  # Money-1 Г-4б: a correction to a SMALLER sum leaves a debt in the other groups of the student - [{membership_id, group_id, group_name, amount (the growth), total (the debt after)}], as in ``PaymentVoided``
    advance_drops: List[Dict[str, Any]] = Field(default_factory=list)  # G3M С3: [{membership_id, group_id, group_name, amount (the fall of the advance), total (the advance after)}] - the OTHER groups that lose money without a debt; the front shows only ``money_effects.text``
    other_gains: List[Dict[str, Any]] = Field(default_factory=list)  # Money-2 T7: the OTHER groups whose account grows - [{membership_id, group_id, group_name, debt_fell, debt (after), advance_grew, advance (after)}]: a restored payment brings back the money that went to them on a transfer


class PaymentVoided(BaseModel):
    payment: PaymentOut
    row: MembershipPaymentRow
    money_effects: Optional[Dict[str, Any]] = None  # Money-1 A3: the saved payroll sheets whose pay the operation moves (``money_effects.run_with_effects``)
    dry_run: bool = False                            # Money-1 R3 #15: the answer of ``?dry_run=true`` - nothing was written, the ids are provisional
    new_debts: List[Dict[str, Any]] = Field(default_factory=list)  # Money-1 Г-4: [{membership_id, group_id, group_name, amount (the growth), total (the debt after)}] - the debt that appears in the OTHER groups of the student (the money had gone there on a transfer)
    advance_drops: List[Dict[str, Any]] = Field(default_factory=list)  # G3M С3: [{membership_id, group_id, group_name, amount (the fall of the advance), total (the advance after)}] - the OTHER groups that lose money without a debt; the front shows only ``money_effects.text``


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


class RecalcOut(PayModel):
    """PAY-F: one deduction «Перерасчёт: урок ДД.ММ отменён центром» (derived from the cancelled lesson, never stored)."""

    lesson_date: date_type
    lesson_id: str
    amount: int  # positive: it comes OFF the payment
    text: str  # «−46 000 сум: урок 21.10 отменён центром»


class ReceiptState(PayModel):
    """Money-1 V5: the account of the membership on the day the receipt is ISSUED (not the snapshot of the day of the payment)."""

    as_of: date
    debt: int = 0
    advance: int = 0
    paid_until: Optional[date] = None


class ReceiptOut(BaseModel):
    """Everything the printed receipt shows (no other payment data)."""

    receipt_no: int
    org_name: str
    org_city: Optional[str] = None
    org_phone: Optional[str] = None
    student_name: str
    display_id: Optional[str] = None
    group_name: str
    # IA fix pack (additive): where a receipt opened by a bare link links onward (the student card, the account of the membership, the group)
    student_id: str
    membership_id: str
    group_id: str
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
    recalc: List[RecalcOut] = Field(default_factory=list)  # PAY-F: «−46 000 сум: урок 21.10 отменён центром»
    money_rows: List[MoneyRow] = Field(default_factory=list)  # Б11.35 М1: the layout of the payment by periods (the words and sums of the server; the same lines the window «Внести оплату» showed); empty on a legacy receipt
    note: Optional[str] = None
    accepted_by_name: Optional[str] = None
    created_at: Optional[str] = None
    voided_at: Optional[str] = None
    void_reason: Optional[str] = None
    # Money-1 V5 (additive): the moment of issue (naive UTC), the account on that day, «копия» for every print after the first, the note of a cancelled payment, the line that this is not a fiscal cheque
    issued_at: Optional[str] = None
    state_on_issue: Optional[ReceiptState] = None
    receipt_state_text: Optional[str] = None      # the ONE state of the receipt on the day of issue (``state_on_issue.as_of``), a whole phrase without a date: «не оплачено 100 000 сум, оплачено до 14.11» / «оплачено 100 000 из 420 000 сум · к оплате 320 000 сум до 05.11» / «расчёт закрыт»
    is_copy: bool = False
    print_count: int = 0
    void_note: Optional[str] = None
    disclaimer: str = "Не является фискальным чеком"
    replaced_by_receipt_no: Optional[int] = None
    replaces_receipt_no: Optional[int] = None
    replaces_amount: Optional[int] = None  # LOGIC-A: the sum of the payment this one corrects («исправлена: было 450 000»)


# ============================================================================================================
# PAY-2 contract: preview, write-off, refund, statement, receipts report (design §4.1-§4.8)
# ============================================================================================================


class PreviewDue(PayModel):
    """What the student owes today (before the typed amount), named by ``pay_due`` (10.2: the old ``past_debt`` / ``current_due`` / ``days_overdue`` are gone)."""

    unpaid: int = 0             # everything unpaid (``debt_amount + due_amount``)
    debt_amount: int = 0
    due_amount: int = 0
    due_until: Optional[date_type] = None
    overdue_days: Optional[int] = None
    debt_text: Optional[str] = None
    debt_sub: Optional[str] = None
    due_text: Optional[str] = None
    due_sub: Optional[str] = None
    advance: int = 0
    segments: List[Segment] = Field(default_factory=list)  # begun segments with ``unpaid`` > 0, oldest first
    paid_until: Optional[date_type] = None


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


class MoneyRow(PayModel):
    """Б11.33 п. 2: one line of «Куда пойдут N сум» - the layout of the receipt («Куда пошли деньги») by the periods, from the oldest; the words are the server's."""

    label: str                              # «Сентябрь» / «Сентябрь, остаток» / «Декабрь 2025» / «15.10–14.11» / «Пакет № 3» / «Аванс на январь» / «Октябрь – декабрь» (closed periods in a row)
    amount: int                             # how much of THIS payment went into the line
    of: Optional[int] = None                # how much of the period was unpaid before the payment (only for ``part``: «508 000 из 550 000»)
    state: Literal["closed", "part", "advance"]
    periods: int = 1                        # how many periods the line folds (closed ones in a row with no «остаток»)


class RestRow(PayModel):
    label: str
    amount: int


class PreviewResult(PayModel):
    allocations: List[Allocation] = Field(default_factory=list)
    unpaid_after: int = 0       # everything unpaid after the payment (a sum; the names are ``debt_amount_after`` / ``due_amount_after``)
    debt_amount_after: int = 0
    due_amount_after: int = 0
    due_until_after: Optional[date_type] = None
    overdue_days_after: Optional[int] = None
    advance_after: int = 0
    paid_until_before: Optional[date_type] = None
    status_after: MembershipStatus = "none"
    partial_credit_after: int = 0
    lessons: Optional[PreviewLessons] = None
    summary_text: str = ""
    after_text: str = ""        # the state after the payment in the words of the server: «Останется: долг X сум · к оплате Y сум» / «Оплачено до ДД.ММ» / «Осталось K урока»
    price_ahead: List[PriceAhead] = Field(default_factory=list)
    money_rows: List[MoneyRow] = Field(default_factory=list)       # Б11.33 п. 2: where the typed sum goes, by periods (the same function as the receipt)
    debt_rest: List[RestRow] = Field(default_factory=list)         # what stays a DEBT after the payment, by periods (the sum = ``debt_amount_after``)
    due_after: int = 0                                             # what stays «к оплате» within the term after the payment (= ``due_amount_after``)
    due_rest: List[RestRow] = Field(default_factory=list)          # the same by periods (the sum = ``due_after``)


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
    recalc: List[RecalcOut] = Field(default_factory=list)  # PAY-F: what is already deducted from this student's account (lessons the centre cancelled)


class WriteoffCreate(BaseModel):
    """``amount`` OR ``all: true`` (the whole debt).  ``reason`` 3..500 characters is mandatory."""

    model_config = ConfigDict(populate_by_name=True)

    amount: Optional[StrictInt] = None  # MN-42
    all_debt: bool = Field(False, alias="all")
    reason: str = Field(..., max_length=500)
    effective_on: Optional[date_type] = None  # default: the business today
    expected_debt: Optional[StrictInt] = None  # Money-1 V1: the debt the person SAW; another one now -> 409 BALANCE_CHANGED (null: no check)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        return (value or "").strip()


class RecordVoid(BaseModel):
    """Cancel a write-off / a refund: the reason 3..500 characters.  ``expected_debt`` (a write-off) / ``expected_advance`` (a refund): the sum the person SAW (Money-1 V1; null: no check)."""

    reason: str = Field(..., max_length=500)
    expected_debt: Optional[StrictInt] = Field(None, description="Отмена прощения: ТЕКУЩИЙ долг ученика ДО отмены - то, что человек видит сейчас (`row.debt`), а не «долг станет X». Другое число сейчас - 409 BALANCE_CHANGED. null - без проверки.")
    expected_advance: Optional[StrictInt] = Field(None, description="Отмена возврата: ТЕКУЩИЙ аванс ученика ДО отмены (`row.advance`), а не «аванс станет X». null - без проверки.")

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
    used: Optional[int] = None      # PAY-TAILS Р5: the part of ``amount`` (what was entered) that covered a charge; null when unknown (a cancelled record)
    text: Optional[str] = None      # «использовано N сум» - only when ``used`` < ``amount`` (the server's words, the screen prints them)


class WriteoffCreated(PayModel):
    writeoff: WriteoffOut
    unpaid_before: int = 0      # everything unpaid before / after the forgiveness
    unpaid_after: int = 0
    closes: Optional[Dict[str, str]] = None  # Money-1 V9: the period the forgiveness closes: {"from", "to", "label"}; null - it closes nothing
    row: MembershipPaymentRow
    money_effects: Optional[Dict[str, Any]] = None  # Money-1 A3: the saved payroll sheets whose pay the operation moves (``money_effects.run_with_effects``)
    dry_run: bool = False                            # Money-1 R3 #15: the answer of ``?dry_run=true`` - nothing was written, the ids are provisional


class WriteoffVoided(PayModel):
    writeoff: WriteoffOut
    row: MembershipPaymentRow
    money_effects: Optional[Dict[str, Any]] = None  # Money-1 A3: the saved payroll sheets whose pay the operation moves (``money_effects.run_with_effects``)
    dry_run: bool = False                            # Money-1 R3 #15: the answer of ``?dry_run=true`` - nothing was written, the ids are provisional


class RefundCreate(BaseModel):
    amount: StrictInt  # MN-42
    method: Optional[NewPaymentMethod] = None  # mandatory: a missing one is 422 METHOD_REQUIRED
    reason: Optional[str] = Field(None, max_length=500)
    note: Optional[str] = Field(None, max_length=500)
    effective_on: Optional[date_type] = None  # default: the business today
    expected_refundable: Optional[StrictInt] = None  # Money-1 V1: the sum to return the person SAW; another one now -> 409 BALANCE_CHANGED (null: no check)


class RefundOut(PayModel):
    id: str
    membership_id: str
    receipt_no: Optional[int] = None  # Money-1 V6: the number of the receipt of the refund («В-n»); null for a refund older than the numbers
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
    money_effects: Optional[Dict[str, Any]] = None  # Money-1 A3: the saved payroll sheets whose pay the operation moves (``money_effects.run_with_effects``)
    dry_run: bool = False                            # Money-1 R3 #15: the answer of ``?dry_run=true`` - nothing was written, the ids are provisional


class RefundVoided(PayModel):
    refund: RefundOut
    row: MembershipPaymentRow
    money_effects: Optional[Dict[str, Any]] = None  # Money-1 A3: the saved payroll sheets whose pay the operation moves (``money_effects.run_with_effects``)
    dry_run: bool = False                            # Money-1 R3 #15: the answer of ``?dry_run=true`` - nothing was written, the ids are provisional


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


class StatementSummary(UnpaidFields, PayModel):
    """Always for the whole accounting period up to today, whatever ``from`` / ``to`` the rows were cut by."""

    charged: int = 0
    paid: int = 0  # payments (gross, refunds not taken off)
    transferred_in: int = 0
    written_off: int = 0
    recalculated: int = 0  # PAY-F: the deductions for the lessons the centre cancelled
    refunded: int = 0
    transferred_out: int = 0
    unpaid: int = 0               # everything unpaid (``debt_amount + due_amount``)
    advance: int = 0
    paid_until: Optional[date_type] = None
    status: MembershipStatus = "none"
    reconciles: bool = True  # charged - paid - transferred_in - written_off - recalculated + refunded + transferred_out == debt - advance


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
    recorded_on: Optional[date_type] = None  # refund, writeoff: the business day the record was ENTERED (``date`` is the day it is dated)
    amount: Optional[int] = None  # writeoff: what was ENTERED (the row's ``credit`` is what was USED)
    used: Optional[int] = None  # writeoff: the part that covered a charge (PAY-TAILS Р5)
    text: Optional[str] = None  # writeoff: «использовано N сум», only when ``used`` < ``amount``
    from_group_name: Optional[str] = None  # transfer_in
    from_membership_id: Optional[str] = None
    to_group_name: Optional[str] = None  # transfer_out
    to_membership_id: Optional[str] = None
    recalc: Optional[RecalcOut] = None  # PAY-F: recalc
    # LOGIC-A: the correction chain of a payment row.  A correction shows «исправлена: было 450 000»; the voided original
    # (``include_voided``) links to the receipt that replaced it.
    replaces_receipt_no: Optional[int] = None
    replaces_amount: Optional[int] = None
    replaced_by_receipt_no: Optional[int] = None


class OpenPart(PayModel):
    """PAY-DUE 10.11 (lead 20.11, decision 6): one named part of what is open on a statement row (``paid`` / ``due`` / ``debt`` / ``advance``) with its words."""

    name: Literal["paid", "forgiven", "closed", "due", "debt", "advance"]
    amount: int
    text: str


class StatementRow(PayModel):
    seq: int
    date: date_type
    type: StatementRowType
    title: str
    debit: int = 0  # charged
    credit: int = 0  # money in (payment, transfer in, write-off)
    outflow: int = 0  # money out (refund, transfer out)
    balance_after: Optional[int] = None  # negative = debt, positive = advance; null on a voided row
    balance_text: Optional[str] = None  # «Долг 500 000 сум» | «Аванс 21 000 сум» | «0»: the RUNNING total (the screen reads it; PAY-TAILS Б1.6: with the unit, in the words of the dictionary); the column reads ``open_*``
    text: Optional[str] = None  # PAY-TAILS Б1.5: the quiet line of a record - a forgiveness: «использовано 35 000 сум» (only when less was used than entered; the same words as ``details.text``)
    # PAY-DUE 10.11: what is open on the row ITSELF today.  A charge: the unpaid part of THIS charge named by ``pay_due`` (``paid`` = «Оплачено», ``due`` = «К оплате N», ``debt`` = «Долг N»); a payment: ``advance``
    # («Аванс N») while a part of it is not laid on the charges yet, else null; a transfer, a refund, a write-off: null.
    open_amount: Optional[int] = None
    open_name: Optional[Literal["paid", "forgiven", "closed", "due", "debt", "advance"]] = None
    open_text: Optional[str] = None
    open_sub: Optional[str] = None  # the term of the row in words: «срок был 05.11» (debt) / «до 24.11» / «до сегодня» (к оплате), both joined by « · »; null for a paid charge, the opening balance and rows with no unpaid part
    # the same split by name: ONE element, or TWO when the unpaid part of one charge holds both names (a package: some lessons a debt, the newest ones «к оплате»; the debt first).  ``open_name`` is the worst of them
    # (the tone), ``open_amount`` their sum, ``open_text`` the words of both («Долг 50 000 сум · к оплате 100 000 сум»).  The equalities of 10.11 are summed over ``open_parts``.
    open_parts: List[OpenPart] = Field(default_factory=list)
    voided_at: Optional[str] = None
    void_reason: Optional[str] = None
    voided_by_name: Optional[str] = None
    details: StatementDetails = Field(default_factory=StatementDetails)


class StatementForecast(PayModel):
    """The forecast row of a statement (a charge that has not begun): the day, the title («Ноябрь 2026 — неполный месяц (с 24.11, 2 урока из 8)»), the sub-line («прогноз: начислится 120 000 сум»)."""

    date: date_type
    title: str
    sub: str


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
    forecast: Optional[StatementForecast] = None  # PAY-TAILS Б1.4: ONE grey row «прогноз» of the charge that has not begun; in no row, no total, no equality
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
    """Where the whole centre stands TODAY (not limited to the range; includes students who left and archived ones), by the names of ``pay_due``."""

    debt_total: int = 0                       # Σ ``debt_amount``
    due_total: int = 0                        # Σ ``due_amount``
    debtors: int = 0                          # DISTINCT students with ``debt_amount`` > 0
    due_students: int = 0
    advance_total: int = 0
    memberships_with_debt: int = 0
    memberships_with_advance: int = 0
    debt_text: Optional[str] = None           # «Долг 600 000 сум» / null at zero: the words of the server, the screen glues nothing
    due_text: Optional[str] = None            # «К оплате 231 000 сум» / null at zero


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


class BalanceRow(UnpaidFields, PayModel):
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
    unpaid: int = 0               # everything unpaid (``debt_amount + due_amount``)
    advance: int = 0
    refundable: int = Field(0, description="Устарело: равно advance; экраны читают advance", json_schema_extra={"deprecated": True})


class BalancesPage(PayModel):
    items: List[BalanceRow] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0
    # the sums of ALL the rows of the filter (not of the page)
    debt_total: int = 0         # Σ ``debt_amount``
    due_total: int = 0          # Σ ``due_amount``


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
    recalc: List[RecalcOut] = Field(default_factory=list)  # PAY-F: the deductions for cancelled lessons this payment took into account
    money_rows: List[MoneyRow] = Field(default_factory=list)  # Б11.35 М1: «Куда пошли деньги» by periods, the SAME function as the probe of the window (``payments_views.money_rows``); empty on a snapshot made before it


class AccountingStartImpact(PayModel):
    """What moving the start of the accounting does (all centre-wide, today): new debts AND changes of the advance / of what
    can be refunded (an earlier start charges more, so advances shrink)."""

    memberships_with_new_debt: int = 0
    debt_delta: int = 0
    memberships_with_changed_advance: int = 0
    advance_delta: int = 0
    refundable_delta: int = Field(0, description="Устарело: равно advance; экраны читают advance", json_schema_extra={"deprecated": True})
    openings_to_review: int = 0  # OPENING-BULK: openings whose «оплачено до» is before the day preceding the new start (kept as they are; review them)


class PaymentSettingsChanged(PaymentSettings):
    """``PUT /org/settings/payments``.  ``?dry_run=true`` writes nothing: the CURRENT settings + the impact of the
    requested ``accounting_start_date`` (``impact``); a real write returns the new settings and ``impact`` null."""

    dry_run: bool = False
    impact: Optional[AccountingStartImpact] = None
    # LOGIC-A (F12): with ``dry_run`` and a change of the billing mode: who switches and when (null otherwise).
    regime_impact: Optional["RegimeImpact"] = None
    # PAY-DUE: with ``dry_run`` and a new term: how many students change name by it (null when the term does not change).  Nothing is written.
    pay_due_impact: Optional["PayDueImpact"] = None


class PayDueImpact(PayModel):
    """``to_due``: students who are debtors now and would not be (their unpaid sum becomes «К оплате»); ``to_debt``: the other way round.  ``texts``: the words for the line before saving (none at zero)."""

    to_due: int = 0
    to_debt: int = 0
    texts: List[str] = Field(default_factory=list)


# ================================================================================================================
# LOGIC-A (pack «деньги»): opening balance, pause, leave / archive with money, price impact, regime impact, lesson type.
# FROZEN with ``artifacts/partner_doc_check/logicA_contract.json`` and ``tests/fixtures/logicA``: a change = change the
# spec (``logicA_spec.md``) and these schemas first.
# ================================================================================================================


class LogicAErrorDetail(PayErrorDetail):
    """Error body of the LOGIC-A endpoints: ``PayErrorDetail`` + the lists some codes carry.

    Codes: ``OPENING_RANGE`` / ``PAUSE_RANGE`` / ``PAUSE_LOCKED`` / ``NOTHING_TO_PAY`` / ``PAYMENT_EXCEEDS_DEBT`` (``unpaid``) /
    ``LESSON_TYPE_RANGE`` / ``METHOD_REQUIRED`` / ``REASON_REQUIRED`` (422); ``OPENING_NOT_ALLOWED`` / ``PAUSE_OVERLAP`` /
    ``PAUSE_OVERLAPS_ATTENDED`` (``dates``) / ``PAUSE_BREAKS_REFUND`` / ``PAUSE_CLOSED_BY_LEAVE`` / ``MEMBERSHIP_CLOSED`` / ``BALANCE_CHANGED`` (``fresh``) /
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
    refundable: int = Field(0, description="Устарело: равно advance; экраны читают advance", json_schema_extra={"deprecated": True})


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
    refundable_before: int = Field(0, description="Устарело: равно advance; экраны читают advance", json_schema_extra={"deprecated": True})
    refundable_after: int = Field(0, description="Устарело: равно advance; экраны читают advance", json_schema_extra={"deprecated": True})


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
    debt_amount: Optional[StrictInt] = None  # 1..10^10; R3 2: ``true`` / "100000" are 422
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
    warnings: List[Dict[str, str]] = Field(default_factory=list)  # Money-1 R3 Д4: ``[{"code": "OPENING_OVER_PAYMENTS", "message": "У ученика уже есть оплаты на N сум — покрытие станет до ДД.ММ"}]``


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
    days_locked: bool = Field(False, description="На паузе стоит действующая обрезка уходом / переводом / архивом: дни изменить нельзя (Б11.33 п. 1)")
    days_locked_text: Optional[str] = Field(None, description="Готовая фраза причины (та же, что в отказе 409 PAUSE_CLOSED_BY_LEAVE); null, когда days_locked ложь")


class PauseLessonMark(PayModel):
    date: date_type
    marks: Optional[str] = None  # absent | excused | present | late | null (no mark yet)


class PauseLessons(PayModel):
    excluded: List[PauseLessonMark] = Field(default_factory=list)  # lessons that will no longer be counted
    attended: List[PauseLessonMark] = Field(default_factory=list)  # present / late: a pause over them is refused


class PausePayroll(PayModel):
    months: List[str] = Field(default_factory=list)  # "YYYY-MM"
    saved_sheets_changed: int = 0


class PauseOtherGroups(PayModel):
    """Money-2 T9: what a pause (made, changed, cancelled) does in the OTHER groups of the student - the ones tied to this stay by a transfer (the sum that went there depends on the days of this pause).
    Same shape as the answer of a cancelled payment: the groups where the advance falls / a debt appears, the groups that gain; ``other_groups_text`` is the sentence for the window (the server's)."""

    new_debts: List[Dict[str, Any]] = Field(default_factory=list)
    advance_drops: List[Dict[str, Any]] = Field(default_factory=list)
    other_gains: List[Dict[str, Any]] = Field(default_factory=list)
    other_groups_text: Optional[str] = None


class PauseSaved(PauseOtherGroups):
    pause: PauseOut
    impact: Optional[ImpactMoney] = None  # null without access to money
    lessons: PauseLessons
    payroll: Optional[PausePayroll] = None  # the owner only
    dry_run: bool = False
    row: Optional[MembershipPaymentRow] = None  # null on ``dry_run``


class PauseVoided(PauseOtherGroups):
    pause: PauseOut
    impact: Optional[ImpactMoney] = None
    row: Optional[MembershipPaymentRow] = None
    dry_run: bool = False


class PauseVoid(RecordVoid):
    """Cancel a pause: the reason 3..500 characters."""


# ---- leave / archive with money ---------------------------------------------------------------------

SettleAction = Literal["keep", "writeoff", "refund"]


class LeaveMoney(PayModel):
    unpaid: int = 0                            # everything unpaid after the action (a debt and the sum «к оплате»: the one name of «всё неоплаченное», 17.4; ``expected_unpaid`` of the settlement is checked against it)
    debt_amount: int = 0                       # PAY-DUE 10.5: the part that is a DEBT after the action (the state AFTER it: the student in the centre or not)
    due_amount: int = 0                        # the part that stays «к оплате» (the student is still in the centre, the term goes on)
    due_until: Optional[date_type] = None
    becomes_debt: int = 0                      # the part that is «к оплате» now and the action turns into a debt
    advance: int = 0
    refundable: int = Field(0, description="Устарело: равно advance; экраны читают advance", json_schema_extra={"deprecated": True})
    paid_until: Optional[date_type] = None
    charged_after_leave: int = 0
    individual_price_ends: bool = False


class RecalcLineOut(PayModel):
    """PAY-TAILS Б1.3: one line of «что было -> что станет» of a closing: the charge of a period that falls (``Ноябрь: 600 000 → 415 000 сум``, ``basis_text`` - the grey line under it), the forgiveness that falls with it
    (``Прощено: 600 000 → 415 000 сум``) or a period paid ahead that will not begin (``Декабрь не начнётся: 300 000 сум останутся авансом`` - the MONEY laid on it).  ``done``: the sentence is in the past tense (the answer)."""

    label: str
    before: Optional[int] = None
    after: Optional[int] = None
    basis_text: Optional[str] = None
    text: str
    done: bool = False


class RecalcCutOut(PayModel):
    """The block «что было -> что станет» (PAY-TAILS Б6.5: always this object, never a bare list): the first two periods as lines and the number of the rest (``more``, 0 when all are shown: «и ещё N периодов»)."""

    lines: List[RecalcLineOut] = Field(default_factory=list)
    more: int = 0


class LeaveMembership(PayModel):
    membership_id: str
    group_id: str
    group_name: str
    money: Optional[LeaveMoney] = None  # null without access to money
    has_open_balance: bool = False
    allowed: List[SettleAction] = Field(default_factory=lambda: ["keep"])
    effects: Optional[Dict[str, Any]] = None  # Money-2 T3: what the closing ends / leaves behind (``leave_effects.build``); sums only with the right to money
    # PAY-TAILS Б1.3 / Б5.1-Б5.2 (sums: only with the right to money): what the closing does to the charges of THIS group (a list of lines, or ``{lines, more}`` when cut at two periods; null: no change)
    # and to the OTHER groups of the student (the day that was paid in this group is not paid twice there): sentences of the server with the money in them
    recalc: Optional[RecalcCutOut] = None
    others: List[str] = Field(default_factory=list)


class LeavePreview(PayModel):
    as_of: date_type
    memberships: List[LeaveMembership] = Field(default_factory=list)


class SettleItem(BaseModel):
    membership_id: str
    action: SettleAction = "keep"
    expected_refundable: Optional[StrictInt] = None  # refund (R3 2)
    expected_unpaid: Optional[StrictInt] = None  # writeoff (R3 2): everything unpaid the person SAW; PAY-TAILS Б5.1: sent for EVERY membership (``keep`` too) by a caller with the right to money
    expected_advance: Optional[StrictInt] = None  # PAY-TAILS Б5.1: the advance the person SAW (every membership)
    expected_as_of: Optional[date_type] = None  # PAY-TAILS Б5.1: the business day of the probe; another day at the action is 409 BALANCE_CHANGED (the numbers of the day may be the same, the words about the other groups are not)
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
    effects: Optional[Dict[str, Any]] = None  # Money-2 T3: the same structure as the preview, after the choices of ``settle`` (a refunded advance no longer «stays»)
    recalc: Optional[RecalcCutOut] = None      # PAY-TAILS Б1.3: the lines of the probe, done (past tense); sums only with the right to money
    others: List[str] = Field(default_factory=list)


# ---- price impact (decision 4) ----------------------------------------------------------------------


class WhatIfLessons(PayModel):
    """The package view of an account in a window: paid lessons, used, left (the lessons mode only)."""

    covered: int = 0
    used: int = 0
    left: int = 0
    package_size: int = 0


class NextPeriod(PayModel):
    """Money-1 R3 №4: the first period of the student that begins on or after the day the change reaches him, and what it costs before -> after (a student who pays in time shows no
    difference in his account that day, but the next period costs another sum)."""

    period_from: date_type
    period_to: date_type
    amount_before: int
    amount_after: int
    difference: int


class PriceImpactItem(PayModel):
    """One student of a window (Money-1 B3): the account before -> after on the day the change reaches him, and the sentence for the screen (``text``)."""

    membership_id: str
    student_name: str
    debt_before: int = 0
    debt_after: int = 0
    advance_before: int = 0
    advance_after: int = 0
    paid_until_before: Optional[date_type] = None
    paid_until_after: Optional[date_type] = None
    has_money: bool = False
    charged_before: int = 0
    charged_after: int = 0
    lessons_before: Optional[WhatIfLessons] = None      # a package: the paid lessons before / after (null: a calendar mode)
    lessons_after: Optional[WhatIfLessons] = None
    next_period: Optional[NextPeriod] = None            # R3 №4 (null: the next period costs the same / a package)
    text: str = ""


class OverrideStudent(PayModel):
    """A student whose individual price does not follow the group (Money-1 B3 / B4 of the devil): the window lists him apart with the difference from the new group price."""

    membership_id: str
    student_name: str
    price: int
    group_price_before: Optional[int] = None
    group_price_after: Optional[int] = None
    difference_after: Optional[int] = None      # price - the new group price (negative: a discount stays whatever the group does)
    reason: Optional[str] = None


class FirstPriceInfo(PayModel):
    """Money-1 B2: the first price of a group acts from the start of the running period of each student."""

    requested_valid_from: date_type
    valid_from: date_type
    moved: bool = False
    students_without_price: int = 0
    text: str = ""


class PriceImpactMoney(PayModel):
    memberships_with_new_debt: int = 0
    debt_delta: int = 0
    memberships_with_less_advance: int = 0
    advance_delta: int = 0
    days_lost_total: int = 0
    backdated: bool = False
    as_of: Optional[date_type] = None     # Money-1 B3: the day the accounts are read on (the day the change reaches the students)
    items_total: int = 0                  # every student who moves; ``items`` holds the first 200
    truncated: bool = False
    text: str = ""
    lessons_lost_total: int = 0           # R3 money R-5: the paid lessons of the packages that the change takes away
    next_period_students: int = 0         # R3 №4: how many students pay in time and have a dearer / cheaper NEXT period; ``next_period_delta_total`` = Σ of the difference of that period
    next_period_delta_total: int = 0
    items: List[PriceImpactItem] = Field(default_factory=list)
    override_students: List[OverrideStudent] = Field(default_factory=list)
    override_total: int = 0               # R3 №16: the list is cut at 200 like ``items``, and says so
    override_truncated: bool = False


# ---- billing mode change (F12) ----------------------------------------------------------------------


class RegimeItem(PriceImpactItem):
    """One student of the window of the billing mode / the day of the shared month (Money-1 B3): the account on the last of the first ``horizon_days`` days of the new way, before -> after."""

    switch_on: Optional[date_type] = None             # the first day the new way counts for him (the running period is never cut)
    as_of: Optional[date_type] = None                 # the day his accounts are compared on (``switch_on`` + ``horizon_days`` - 1); null with ``switch_on`` null (a package in progress)
    price_amount: Optional[int] = None                # the SAME number of his price: it means another thing in another mode (ДЫРА-02)
    price_unit_before: Optional[str] = None           # «за месяц» / «за общий месяц с 10-го числа» / «за пакет из 12 уроков»
    price_unit_after: Optional[str] = None
    per_lesson_before: Optional[int] = None           # ≈ sum per lesson (the price over the lessons of a month by the schedule / of a package): an APPROXIMATION, the text says «≈»; null: not told
    per_lesson_after: Optional[int] = None
    charged_before: Optional[int] = None              # null: the forecast is impossible (the group has no schedule) - the screen shows no sum, only the sentence
    charged_after: Optional[int] = None


class RegimeOverride(PayModel):
    """A student with an individual price: his number stays what it is, in the new mode it means another thing (B4 of the devil: they are listed apart, not hidden)."""

    membership_id: str
    student_name: str
    price: int
    reason: Optional[str] = None
    text: str = ""


class RegimeImpact(PayModel):
    students_affected: int = 0
    first_switch: Optional[date_type] = None
    latest_switch: Optional[date_type] = None
    defer: bool = True
    # Money-1 B3 (ДЫРА-02 / ДЫРА-06): every student who switches, before -> after, in sums and lessons
    horizon_days: int = 30                            # the accounts are compared on the last of this many days from the switch day of each student
    pending_package_students: int = 0                 # R3 code №1: students with a package IN PROGRESS - the new way begins when it ends (``switch_on`` null); the dates ``first_switch`` / ``latest_switch`` are 9999-12-31 for them, as before
    items_total: int = 0
    truncated: bool = False
    charged_before_total: Optional[int] = 0           # null: no forecast for some student (the group has no schedule) - no total is shown. Σ CHARGED IN the first ``horizon_days`` days of the new way (not the history since the accounting began), the students with a switch day
    charged_after_total: Optional[int] = 0
    text: str = ""
    items: List[RegimeItem] = Field(default_factory=list)
    override_students: List[RegimeOverride] = Field(default_factory=list)
    override_total: int = 0
    override_truncated: bool = False


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


class RefundReceiptOut(PayModel):
    """Money-1 V6: what the printed «Квитанция о возврате» shows (``GET /org/refunds/{id}/receipt``)."""

    refund_id: str
    receipt_no: Optional[int] = None
    receipt_label: Optional[str] = None  # «В-12»
    org_name: str
    org_city: Optional[str] = None
    org_phone: Optional[str] = None
    student_name: str
    display_id: Optional[str] = None
    group_name: str
    amount: int
    amount_words: str
    method: PaymentMethod = "other"
    method_label: str
    reason: Optional[str] = None
    note: Optional[str] = None
    effective_on: date_type
    created_at: Optional[str] = None
    accepted_by_name: Optional[str] = None  # who gave the money
    voided_at: Optional[str] = None
    voided_by_name: Optional[str] = None
    void_reason: Optional[str] = None
    void_note: Optional[str] = None  # «Возврат отменён 05.11»
    disclaimer: str  # «Не является фискальным чеком»
    signature_line: str  # the line for the signature of the receiver
