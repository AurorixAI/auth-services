"""ALGO - request/response schemas of the payments API (N13 P2).  snake_case."""
from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

MAX_MONEY = 10_000_000_000

PeriodMode = Literal["calendar", "lessons"]
MembershipStatus = Literal["none", "free", "expired", "ends_today", "expiring", "active"]


class PaymentSettings(BaseModel):
    period_mode: PeriodMode = "calendar"
    lessons_per_payment: int = Field(12, ge=1, le=60)
    expiring_days: int = Field(3, ge=0, le=14)
    expiring_lessons: int = Field(3, ge=0, le=10)
    admin_can_manage: bool = False
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class PaymentSettingsUpdate(BaseModel):
    """PATCH semantics: a field that is absent (or null) stays as it was."""

    period_mode: Optional[PeriodMode] = None
    lessons_per_payment: Optional[int] = Field(None, ge=1, le=60)
    expiring_days: Optional[int] = Field(None, ge=0, le=14)
    expiring_lessons: Optional[int] = Field(None, ge=0, le=10)
    admin_can_manage: Optional[bool] = None


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
    code: Literal["overlaps_previous", "gap_before", "period_over_62_days", "paid_over_60_days_ago"]
    message: str


class PaymentCreated(BaseModel):
    payment: PaymentOut
    warnings: List[PaymentWarningOut]
    row: MembershipPaymentRow


class PaymentVoided(BaseModel):
    payment: PaymentOut
    row: MembershipPaymentRow
