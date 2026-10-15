"""ALGO - request/response schemas of the payments API (N13 P2).  snake_case."""
from __future__ import annotations

from datetime import date
from datetime import date as date_type
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

MAX_MONEY = 10_000_000_000

PeriodMode = Literal["calendar", "lessons"]
BillingAnchor = Literal["payment_date", "fixed_day"]
QuoteBasis = Literal["full", "lessons", "days", "package", "custom", "none"]
MembershipStatus = Literal["none", "free", "expired", "ends_today", "expiring", "active"]


class PaymentSettings(BaseModel):
    period_mode: PeriodMode = "calendar"
    # N13 P2.2.  ``payment_date``: a calendar period starts on the payment date (05.10 -> 04.11).
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
    valid_from: date
    valid_to: Optional[date] = None
    lessons_count: Optional[int] = None
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


class PaymentStatusPage(BaseModel):
    items: List[MembershipPaymentRow]
    total: int
    limit: int
    offset: int
    as_of: str


class FeeUpdate(BaseModel):
    monthly_fee: Optional[int] = Field(None, ge=0, le=MAX_MONEY)


class PaymentCreate(BaseModel):
    amount: int = Field(..., ge=0, le=MAX_MONEY)
    paid_on: date
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    lessons_count: Optional[int] = Field(None, ge=1, le=120)
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
    code: Literal[
        "overlaps_previous", "gap_before", "period_over_62_days", "paid_over_60_days_ago",
        "short_period_full_amount",
    ]
    message: str


class PaymentCreated(BaseModel):
    payment: PaymentOut
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
    valid_from: date
    valid_to: Optional[date] = None
    lessons_count: Optional[int] = None
    partial_period: bool = False
    note: Optional[str] = None
    accepted_by_name: Optional[str] = None
    created_at: Optional[str] = None
    voided_at: Optional[str] = None
    void_reason: Optional[str] = None
    replaced_by_receipt_no: Optional[int] = None
    replaces_receipt_no: Optional[int] = None
