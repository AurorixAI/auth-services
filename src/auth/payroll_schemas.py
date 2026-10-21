"""ALGO - request/response schemas of the payroll API (N13 P3).  snake_case.  Owner only."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

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


# UX-2: the percent of a teacher is a RATE (``payroll_rates``, set in the card of the teacher).  The old ``percents``
# field of the requests is still accepted - the front of the release before sends it - but it is IGNORED: whatever it
# holds (even an invalid value) is not validated and changes nothing.
Ignored = Optional[Any]


class LineOverride(BaseModel):
    """A change of one line (membership) of a sheet.

    ``include_unpaid``: absent leaves it as it is.  ``monthly_fee``: the price is NOT edited on a sheet any more (UX-2,
    owner decision) - a number is refused with 422 ``PRICE_NOT_EDITABLE`` (the price comes from «Цены групп» or from
    the card of the student); ``null`` is accepted and means "no manual price" (drops a manual price of an old sheet).
    """

    membership_id: UUID
    monthly_fee: Optional[Any] = None
    include_unpaid: Optional[bool] = None


def _unique_memberships(items: Optional[List[LineOverride]]) -> None:
    ids = [item.membership_id for item in items or ()]
    if len(ids) != len(set(ids)):
        raise ValueError("Строка ведомости указана дважды")


class CalculateRequest(BaseModel):
    percents: Ignored = None  # deprecated, ignored: the rates of the month come from ``payroll_rates``
    teacher_ids: Optional[List[UUID]] = Field(None, max_length=500)


class SheetCreate(BaseModel):
    """Save a sheet; if the teacher already has one for the month this is its next revision."""

    month: str = Field(..., pattern=r"^\d{4}-\d{2}$")
    teacher_id: UUID
    percents: Ignored = None  # deprecated, ignored: the rates of the month come from ``payroll_rates``
    line_overrides: List[LineOverride] = Field(default_factory=list, max_length=5000)
    note: Optional[str] = Field(None, max_length=MAX_NOTE)
    # The revision the caller saw; required (and must be current) when the sheet already exists.
    revision: Optional[int] = Field(None, ge=1)
    # ``source_fingerprint`` of the preview the caller looked at: if the data moved since, nothing is saved.
    expected_fingerprint: Optional[str] = Field(None, max_length=128)

    @model_validator(mode="after")
    def _check(self):
        _unique_memberships(self.line_overrides)
        return self


class SheetPatch(BaseModel):
    revision: int = Field(..., ge=1)
    percents: Ignored = None  # deprecated, ignored: the rates of the month come from ``payroll_rates``
    line_overrides: Optional[List[LineOverride]] = Field(None, max_length=5000)
    recalculate: bool = False
    # With ``recalculate``: the ``source_fingerprint`` of the preview the caller looked at (409 if it moved).
    expected_fingerprint: Optional[str] = Field(None, max_length=128)
    note: Optional[str] = Field(None, max_length=MAX_NOTE)

    @model_validator(mode="after")
    def _check(self):
        _unique_memberships(self.line_overrides)
        return self


# ---- sheet ------------------------------------------------------------------------


class MarkOut(BaseModel):
    date: str
    lesson_id: str
    mark: Optional[int] = None  # 1 counted, 0 not counted (confirmed excuse), null - not in the group
    state: str  # counted | excused_confirmed | excused_pending | not_in_group | unpaid
    divisor: Optional[int] = None
    # ROADMAP #105: the price of the student ON THE DAY of this lesson (only on a lesson that carries money, and only
    # on a sheet calculated after #105) and where it came from: group | override | none.
    price: Optional[int] = None
    price_source: Optional[str] = None


class WarningOut(BaseModel):
    code: str
    message: str


class PriceStepOut(BaseModel):
    """The price of a line changed inside the month: ``date`` is the first lesson day with this price."""

    date: str
    amount: Optional[int] = None  # null - no price was set


class LineOut(BaseModel):
    membership_id: str
    student_id: str
    student_name: str
    display_id: Optional[str] = None
    group_id: str
    group_name: str
    lesson_type: str
    lesson_time: Optional[str] = None
    # The price of the line (ROADMAP #105): the effective price on the last lesson that carries money (an overwrite on
    # the sheet - ``fee_overridden`` - replaces the price of every lesson).  ``source_fee`` is the same price before the
    # overwrite.  The amount of the line is the sum over its lessons, each at the price of ITS day (``marks[].price``).
    monthly_fee: Optional[int] = None
    source_fee: Optional[int] = None
    fee_overridden: bool = False
    price_source: Optional[str] = None  # group | override ("индивидуальная цена") | none
    price_changed: bool = False  # the price changed inside the month; ``price_steps`` lists the changes
    price_steps: List[PriceStepOut] = Field(default_factory=list)
    no_price_lessons: Optional[int] = None  # lessons of the line that have no price (null on a sheet saved before #105)
    unpaid_due: Optional[int] = None  # unpaid lessons that cost something (null on a sheet saved before #105)
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
    # UX-2: the rate in force for the month per kind of lessons the teacher held in it (``{"group": "50"}``; a kind
    # without a rate is absent) and the kinds without a rate - the sheet cannot be calculated until they are set.
    rates: Dict[str, str] = Field(default_factory=dict)
    missing_rates: List[str] = Field(default_factory=list)
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


class RateChangeOut(BaseModel):
    lesson_type: str
    saved_percent: str
    fresh_percent: Optional[str] = None
    message: str


class SheetDiff(BaseModel):
    sheet_id: str
    revision: int
    stale: bool
    # The FRESH fingerprint of the calculation this diff compared against: send it back as
    # ``expected_fingerprint`` with PATCH ``recalculate`` and the owner saves exactly the diff he saw.
    source_fingerprint: str
    saved_total: int
    fresh_total: int
    delta_amount: int
    # UX-2: why the sheet is different besides the lines: the rate of the sheet's month changed
    # («Ставка «Группа»: 50% → 45%»); ``rate_changes`` is the same in numbers (``fresh_percent`` null - no rate).
    reasons: List[str] = Field(default_factory=list)
    rate_changes: List["RateChangeOut"] = Field(default_factory=list)
    lines: List[DiffLine]


# ---- rates (UX-2) -----------------------------------------------------------------


class RateOut(BaseModel):
    """The rate in force for the month asked: ``valid_from`` is the first day of the month it began."""

    percent: str
    valid_from: str
    set_by_name: Optional[str] = None  # null for a rate transferred by the migration
    set_at: Optional[str] = None


class RateNextOut(BaseModel):
    percent: str
    valid_from: str


class TeacherRates(BaseModel):
    teacher_id: str
    teacher_name: Optional[str] = None
    lesson_types: List[str] = Field(default_factory=list)
    rates: Dict[str, Optional[RateOut]] = Field(default_factory=dict)  # a kind without a rate: null
    next: Dict[str, RateNextOut] = Field(default_factory=dict)  # only kinds that have a future change


class RatesResponse(BaseModel):
    month: str
    items: List[TeacherRates]


class RatePut(BaseModel):
    """Set the rates of a teacher from a month.  Everything is validated by the service (Russian messages)."""

    valid_from: Any = None  # "YYYY-MM"
    percents: Any = None  # {"group": "50", ...}
    note: Any = None


class RateHistoryRow(BaseModel):
    lesson_type: str
    percent: str
    valid_from: str
    set_by_name: Optional[str] = None
    set_at: Optional[str] = None
    note: Optional[str] = None
