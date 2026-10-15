"""ALGO - request/response schemas of the payroll API (N13 P3).  snake_case.  Owner only."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator

from . import payroll_engine as engine

MAX_MONEY = engine.MAX_FEE
MAX_NOTE = 300

LessonType = Literal["group", "mini_group", "pair", "individual"]


# ---- settings ---------------------------------------------------------------------


class PayrollSettings(BaseModel):
    divisor: int = Field(engine.DEFAULT_DIVISOR, ge=engine.MIN_DIVISOR, le=engine.MAX_DIVISOR)
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class PayrollSettingsUpdate(BaseModel):
    divisor: int = Field(..., ge=engine.MIN_DIVISOR, le=engine.MAX_DIVISOR)


# ---- requests ---------------------------------------------------------------------


class PercentInput(BaseModel):
    """The percent of a teacher for a kind of lessons.  ``percent`` is a number or a decimal string."""

    teacher_id: Optional[UUID] = None
    lesson_type: LessonType
    percent: Decimal

    @field_validator("percent", mode="before")
    @classmethod
    def _percent(cls, value: Any) -> Decimal:
        try:
            return engine.parse_percent(value)
        except engine.PercentError as exc:
            raise ValueError(str(exc)) from exc


def _percent_list(value: Any) -> Any:
    """``{"group": "50"}`` (the form of ``percent_inputs``) is accepted as ``[{lesson_type, percent}]``."""
    if isinstance(value, dict):
        return [{"lesson_type": key, "percent": percent} for key, percent in value.items()]
    return value


Percents = Annotated[List[PercentInput], BeforeValidator(_percent_list), Field(max_length=400)]


def _no_duplicates(percents: Optional[List[PercentInput]]) -> None:
    seen = set()
    for item in percents or ():
        key = (item.teacher_id, item.lesson_type)
        if key in seen:
            raise ValueError("Процент для преподавателя и типа занятий указан дважды")
        seen.add(key)


class LineOverride(BaseModel):
    """A change of one line (membership) of a sheet.

    ``monthly_fee``: a number sets the fee on the sheet, ``null`` returns to the fee of the membership,
    absent leaves it as it is.  ``include_unpaid``: absent leaves it as it is.
    """

    membership_id: UUID
    monthly_fee: Optional[int] = Field(None, ge=0, le=MAX_MONEY)
    include_unpaid: Optional[bool] = None


def _unique_memberships(items: Optional[List[LineOverride]]) -> None:
    ids = [item.membership_id for item in items or ()]
    if len(ids) != len(set(ids)):
        raise ValueError("Строка ведомости указана дважды")


class CalculateRequest(BaseModel):
    percents: Percents = Field(default_factory=list)
    teacher_ids: Optional[List[UUID]] = Field(None, max_length=500)

    @model_validator(mode="after")
    def _check(self):
        _no_duplicates(self.percents)
        if any(item.teacher_id is None for item in self.percents):
            raise ValueError("В процентах укажите teacher_id")
        return self


class SheetCreate(BaseModel):
    """Save a sheet; if the teacher already has one for the month this is its next revision."""

    month: str = Field(..., pattern=r"^\d{4}-\d{2}$")
    teacher_id: UUID
    percents: Percents = Field(default_factory=list)
    line_overrides: List[LineOverride] = Field(default_factory=list, max_length=5000)
    note: Optional[str] = Field(None, max_length=MAX_NOTE)
    # The revision the caller saw; required (and must be current) when the sheet already exists.
    revision: Optional[int] = Field(None, ge=1)

    @model_validator(mode="after")
    def _check(self):
        _no_duplicates(self.percents)
        _unique_memberships(self.line_overrides)
        return self


class SheetPatch(BaseModel):
    revision: int = Field(..., ge=1)
    percents: Optional[Percents] = None
    line_overrides: Optional[List[LineOverride]] = Field(None, max_length=5000)
    recalculate: bool = False
    note: Optional[str] = Field(None, max_length=MAX_NOTE)

    @model_validator(mode="after")
    def _check(self):
        _no_duplicates(self.percents)
        _unique_memberships(self.line_overrides)
        return self


# ---- sheet ------------------------------------------------------------------------


class MarkOut(BaseModel):
    date: str
    lesson_id: str
    mark: Optional[int] = None  # 1 counted, 0 not counted (confirmed excuse), null - not in the group
    state: str  # counted | excused_confirmed | excused_pending | not_in_group | unpaid
    divisor: Optional[int] = None


class WarningOut(BaseModel):
    code: str
    message: str


class LineOut(BaseModel):
    membership_id: str
    student_id: str
    student_name: str
    display_id: Optional[str] = None
    group_id: str
    group_name: str
    lesson_type: str
    lesson_time: Optional[str] = None
    monthly_fee: Optional[int] = None
    source_fee: Optional[int] = None
    fee_overridden: bool = False
    percent: Optional[str] = None
    divisor: Optional[int] = None
    marks: List[MarkOut] = Field(default_factory=list)
    counted: int = 0
    unpaid: int = 0
    pending_excused: int = 0
    include_unpaid: bool = False
    amount: int = 0
    warnings: List[WarningOut] = Field(default_factory=list)


class BlockDate(BaseModel):
    date: str
    lesson_id: str


class GroupBlockOut(BaseModel):
    group_id: str
    group_name: str
    lesson_type: str
    lesson_time: Optional[str] = None
    percent: Optional[str] = None
    dates: List[BlockDate] = Field(default_factory=list)
    line_count: int = 0
    counted: int = 0
    unpaid: int = 0
    total_amount: int = 0


class SheetWarnings(BaseModel):
    open_lessons: Optional[int] = None  # unfinished lessons of the month; null when not looked up
    pending_excused: int = 0
    no_fee: int = 0
    unpaid: int = 0


class SheetOut(BaseModel):
    """A saved sheet (``id`` set) or a preview (``saved`` false, nothing is written)."""

    id: Optional[str] = None
    saved: bool = False
    month: str
    period_start: str
    period_end: str
    month_open: bool
    teacher_id: str
    teacher_name: Optional[str] = None
    revision: Optional[int] = None
    percents: Dict[str, str] = Field(default_factory=dict)
    divisor_default: int = engine.DEFAULT_DIVISOR
    lines: List[LineOut] = Field(default_factory=list)
    groups: List[GroupBlockOut] = Field(default_factory=list)
    total_amount: int = 0
    counted_total: int = 0
    unpaid_total: int = 0
    pending_excused_total: int = 0
    warnings: SheetWarnings = Field(default_factory=SheetWarnings)
    source_fingerprint: str = ""
    stale: Optional[bool] = None  # saved: the data changed after the sheet was saved
    note: Optional[str] = None
    created_by_name: Optional[str] = None
    created_at: Optional[str] = None
    updated_by_name: Optional[str] = None
    updated_at: Optional[str] = None


class SheetBrief(BaseModel):
    id: str
    teacher_id: str
    teacher_name: Optional[str] = None
    month: str
    revision: int
    total_amount: int
    stale: Optional[bool] = None
    updated_by_name: Optional[str] = None
    updated_at: Optional[str] = None


class CalculateResponse(BaseModel):
    month: str
    month_open: bool
    sheets: List[SheetOut]


# ---- month ------------------------------------------------------------------------


class LessonTypeSuggestion(BaseModel):
    lesson_type: str
    groups: int = 0  # groups of this kind the teacher taught in the month
    suggested_percent: Optional[str] = None  # from the last saved sheet of this teacher for this kind


class MonthSheetRef(BaseModel):
    id: str
    revision: int
    total_amount: int
    stale: bool
    updated_at: Optional[str] = None


class MonthWarnings(BaseModel):
    open_lessons: int = 0
    pending_excused: int = 0
    no_fee: int = 0
    unpaid: int = 0


class MonthTeacher(BaseModel):
    teacher_id: str
    teacher_name: Optional[str] = None
    lessons_count: int = 0  # completed lessons the teacher held in the month
    lesson_types: List[LessonTypeSuggestion] = Field(default_factory=list)
    sheet: Optional[MonthSheetRef] = None
    warnings: MonthWarnings = Field(default_factory=MonthWarnings)


class MonthOverview(BaseModel):
    month: str
    period_start: str
    period_end: str
    month_open: bool
    divisor: int
    teachers: List[MonthTeacher]
    lessons_without_teacher: int = 0  # completed lessons whose teacher snapshot is empty: nobody is paid for them


# ---- history, diff ----------------------------------------------------------------


class RevisionOut(BaseModel):
    revision: int
    total_amount: int
    percents: Dict[str, str] = Field(default_factory=dict)
    line_count: int = 0
    changed_by: Optional[str] = None
    changed_by_name: Optional[str] = None
    changed_at: Optional[str] = None
    change_note: Optional[str] = None


class DiffBrief(BaseModel):
    monthly_fee: Optional[int] = None
    percent: Optional[str] = None
    counted: int = 0
    unpaid: int = 0
    pending_excused: int = 0
    amount: int = 0


class DiffLine(BaseModel):
    membership_id: str
    student_name: Optional[str] = None
    group_name: Optional[str] = None
    status: Literal["changed", "added", "removed"]
    changes: List[str] = Field(default_factory=list)
    saved: Optional[DiffBrief] = None
    fresh: Optional[DiffBrief] = None
    delta_amount: int = 0


class SheetDiff(BaseModel):
    sheet_id: str
    revision: int
    stale: bool
    saved_total: int
    fresh_total: int
    delta_amount: int
    lines: List[DiffLine]
