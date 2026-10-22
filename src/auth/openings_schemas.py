"""ALGO - OPENING-BULK: schemas of «Остатки на начало» (mass entry of the opening balances).  snake_case.

The FROZEN contract of the pack (``artifacts/partner_doc_check/openings_contract.json``, ``tests/fixtures/openings``).  Spec:
``artifacts/partner_doc_check/opening_bulk_spec.md`` (the sections «Решения ведущего» and «Решение владельца» are binding).  A change needs a
change of the spec and of this file first, then ``scripts/openings_export_contract.py``.
"""
from __future__ import annotations

from datetime import date as date_type
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from .payments_schemas import PayErrorDetail, PayModel, PeriodMode, PriceOut

OpeningsState = Literal["none", "pending", "done", "skipped"]
ReasonNotEditable = Literal["transfer", "free"]
ItemStatus = Literal["ok", "noop", "error"]
ItemKind = Literal["paid_until", "debt", "both", "cleared", "unchanged", "error"]
MatchedBy = Literal["membership_id", "student_group", "name"]
RowStatus = Literal["ok", "noop", "error", "skipped"]


# ---- the table ------------------------------------------------------------------------------------------------------


class OpeningValue(PayModel):
    """The live opening balance of a membership: «оплачено до» and / or «долг на дату»."""

    paid_until: Optional[date_type] = None
    debt_amount: Optional[int] = None
    debt_on: Optional[date_type] = None


class OpeningStudentRef(PayModel):
    id: str
    name: str
    display_id: Optional[str] = None
    class_level: Optional[int] = None


class OpeningGroupRef(PayModel):
    id: str
    name: str


class OpeningMembershipRow(PayModel):
    """One membership of the table.  ``version`` = the id of the live opening (``-`` when there is none): send it back with the item - a
    change made by somebody else in between gives 409 ``STALE_ITEMS``.  ``floor`` = ``L``: «оплачено до» is not earlier and «долг на дату» is not
    later than it (the front never repeats the rule).  ``missing``: a membership that came before the accounting start and has neither an
    opening nor money - the rows the checklist counts."""

    membership_id: str
    student: OpeningStudentRef
    group: OpeningGroupRef
    mode: PeriodMode
    price: PriceOut
    enrolled_on: date_type
    status: Literal["active", "left"] = "active"
    left_on: Optional[date_type] = None
    paused: bool = False
    opening: Optional[OpeningValue] = None
    version: str
    has_money_records: bool = False
    missing: bool = False
    floor: date_type
    needs_review: bool = False  # the stored opening is outside the range after the accounting start moved («оплачено до» before L, a debt after L)
    only_debt: bool = False  # a student who left before the accounting start: only «долг на начало» (paid_until -> OPENING_RANGE)
    editable: bool = True
    reason_not_editable: Optional[ReasonNotEditable] = None


class OpeningGroupBlock(PayModel):
    id: str
    name: str
    teacher_name: Optional[str] = None
    total: int = 0
    with_opening: int = 0
    missing: int = 0
    memberships: List[OpeningMembershipRow]


class OpeningTotals(PayModel):
    """Centre-wide, whatever the filters are (the progress bar and the banner): ``memberships`` = the tracked (active) ones, ``with_opening`` = those
    that have a live opening, ``missing`` = see ``OpeningMembershipRow.missing``.  ``left_available`` = memberships of students who left before the
    accounting start (``include_left=1`` shows them).  ``shown`` = memberships in ``groups``."""

    memberships: int = 0
    with_opening: int = 0
    missing: int = 0
    left_available: int = 0
    shown: int = 0
    needs_review: int = 0  # openings that the move of the accounting start made inconsistent (never changed silently)


class OpeningsSummary(PayModel):
    """``GET /org/opening-balances/summary``: no rows (the banner of «Должники», the checklist of the settings)."""

    accounting_start: date_type
    state: OpeningsState = "none"
    totals: OpeningTotals


class OpeningsList(PayModel):
    as_of: date_type
    accounting_start: date_type
    state: OpeningsState = "none"
    period_mode: PeriodMode = "calendar"
    paid_until_max: date_type
    totals: OpeningTotals
    groups: List[OpeningGroupBlock]


class OpeningsStateIn(BaseModel):
    """The owner / an admin with the right to money ends the checklist: ``done`` («Готово») or ``skipped`` («Не нужно»).  ``pending`` is set by the
    release script only (``scripts/set_openings_pending.py``)."""

    state: Literal["done", "skipped"]


# ---- the batch ------------------------------------------------------------------------------------------------------


class OpeningBatchItem(BaseModel):
    """One membership of a batch: the item REPLACES the whole opening of the membership (the same as ``PUT .../opening-balance``).

    * ``paid_until`` / ``debt_amount`` + ``debt_on`` (a debt without a date gets ``max(first_day, accounting_start) - 1``);
    * ``lessons_left`` (a lessons-mode centre only, instead of ``paid_until``): the server turns «осталось K уроков» into «оплачено до» = the date of
      the K-th lesson of the group from ``max(first_day, accounting_start)`` (K = 0: nothing is left = «оплачено до» the day before the start, a reviewed row);
    * ``clear`` = true: remove the live opening;
    * nothing at all = error ``EMPTY_ITEM``.
    ``paid_until`` = the day before the accounting start means «nothing owed at the start»: it is stored as a REAL opening (a reviewed row, so the
    row leaves «не заполнено»); the ledger gives the same debt and charges with or without it.  ``lessons_left`` = 0 is the same."""

    membership_id: str
    version: str = Field(..., max_length=64)
    paid_until: Optional[date_type] = None
    debt_amount: Optional[int] = None
    debt_on: Optional[date_type] = None
    lessons_left: Optional[int] = None
    clear: bool = False


class OpeningBatchPreviewRequest(BaseModel):
    items: List[OpeningBatchItem]  # 1..2000 (TOO_MANY_ITEMS)
    reason: Optional[str] = Field(None, max_length=500)  # one reason for every row that has money records (3..500)


class OpeningBatchRequest(OpeningBatchPreviewRequest):
    idempotency_key: UUID
    digest: str = Field(..., max_length=64)  # from the preview of the same items


class OpeningItemState(PayModel):
    """The ledger of a membership as it stands (``before``) / will stand (``after``)."""

    opening: Optional[OpeningValue] = None
    debt: int = 0
    advance: int = 0
    paid_until: Optional[date_type] = None
    status: str = "none"
    lessons_left: Optional[int] = None


class OpeningLessons(PayModel):
    """``lessons_left`` -> «оплачено до»: the text is for the preview («осталось 8 уроков → оплачено до 14.11»)."""

    lessons_left: int
    paid_until: Optional[date_type] = None
    text: str


class OpeningWarning(PayModel):
    """Soft: ``PAID_UNTIL_AT_START`` (a reviewed «nothing owed» row), ``PAID_UNTIL_FAR`` (> today + 120), ``DEBT_LARGE`` (> 12 prices), ``NO_PRICE``,
    ``HAS_MONEY`` (the row has money records: a reason is needed), ``CLEARS_EXISTING`` (removes a live opening)."""

    code: str
    message: str


class OpeningItemResult(PayModel):
    """The verdict of one item.  Error codes: ``NOT_FOUND`` ``STUDENT_ARCHIVED`` ``MEMBERSHIP_CLOSED`` ``DUPLICATE_ITEM`` ``STALE_VERSION`` ``EMPTY_ITEM``
    ``CLEAR_WITH_VALUES`` ``OPENING_RANGE`` ``AMOUNT_RANGE`` ``OPENING_NOT_ALLOWED`` (a transfer) ``FREE_MEMBERSHIP`` ``VOID_BREAKS_REFUND``
    ``LESSONS_NOT_APPLICABLE`` ``LESSONS_RANGE`` ``LESSONS_AND_PAID_UNTIL``.  ``target`` = what will be stored (after the defaults, the lessons
    conversion); null for an error."""

    membership_id: str
    student_name: Optional[str] = None
    group_name: Optional[str] = None
    status: ItemStatus
    kind: ItemKind
    code: Optional[str] = None
    message: Optional[str] = None
    changed: bool = False
    target: Optional[OpeningValue] = None
    lessons: Optional[OpeningLessons] = None
    before: Optional[OpeningItemState] = None
    after: Optional[OpeningItemState] = None
    has_money_records: bool = False
    needs_reason: bool = False
    warnings: List[OpeningWarning] = []


class OpeningBatchSummary(PayModel):
    """Counts by kind (an item with both «оплачено до» and a debt is in both counts)."""

    items: int = 0
    changed: int = 0
    paid_until: int = 0
    debt: int = 0
    cleared: int = 0
    unchanged: int = 0
    errors: int = 0
    debt_total_added: int = 0


class OpeningBatchImpact(PayModel):
    """The effect on the debts - the same dry run as ``previewPaymentSettings`` (the ledger of every touched membership before / after, one
    cheap pass).  The scope is the touched memberships: the items and the memberships linked to them by a transfer (``indirect_changed``)."""

    memberships_changed: int = 0
    memberships_with_new_debt: int = 0
    memberships_with_changed_advance: int = 0
    debt_total_before: int = 0
    debt_total_after: int = 0
    debt_delta: int = 0
    debtors_before: int = 0
    debtors_after: int = 0
    advance_delta: int = 0
    refundable_delta: int = 0
    indirect_changed: int = 0


class OpeningBatchPreview(PayModel):
    """``POST /org/opening-balances/batch/preview``: nothing is written.  ``ok`` = no item is an error (the batch can be applied once the reason is
    given: ``needs_reason`` lists the memberships that have money records).  ``digest`` = the hash of the normalised plan and its effect: the apply
    repeats the plan under the locks and refuses (409 ``PREVIEW_STALE``) when it came out differently."""

    ok: bool
    digest: str
    accounting_start: date_type
    summary: OpeningBatchSummary
    impact: OpeningBatchImpact
    needs_reason: List[str] = []
    items: List[OpeningItemResult]


class OpeningBatchApplied(PayModel):
    """``POST /org/opening-balances/batch``: all or nothing.  ``replayed`` = the same ``idempotency_key`` came again (the same answer; nothing is
    written twice).  ``state`` = ``payments.openings_state`` after the write."""

    batch_id: str
    replayed: bool = False
    summary: OpeningBatchSummary
    impact: OpeningBatchImpact
    changed: int = 0
    state: OpeningsState = "none"


class OpeningFresh(PayModel):
    membership_id: str
    version: str
    opening: Optional[OpeningValue] = None


class OpeningsErrorDetail(PayErrorDetail):
    """Error body of the opening-balance endpoints (``PayErrorDetail`` + lists).  Codes: ``STALE_ITEMS`` (409, ``membership_ids`` + ``fresh``),
    ``PREVIEW_STALE`` (409, ``preview`` = the fresh one), ``IDEMPOTENCY_MISMATCH`` (409), ``CONCURRENT_EDIT`` (409), ``BATCH_INVALID`` (422,
    ``items`` = the items that are errors), ``REASON_REQUIRED`` (422, ``membership_ids``), ``TOO_MANY_ITEMS`` / ``TOO_MANY_ROWS`` /
    ``TOO_MANY_CHANGES`` / ``FILE_TOO_LARGE`` / ``FILE_UNREADABLE`` / ``SHEET_MISSING`` (422), ``NOT_FOUND`` (404, ``membership_ids``); 403 plain."""

    membership_ids: Optional[List[str]] = None
    fresh: Optional[List[OpeningFresh]] = None
    items: Optional[List[OpeningItemResult]] = None
    preview: Optional[OpeningBatchPreview] = None


class OpeningsError(PayModel):
    detail: OpeningsErrorDetail


# ---- Excel ----------------------------------------------------------------------------------------------------------


class OpeningsImportRow(PayModel):
    """One data row of the file.  ``matched_by``: ``membership_id`` (column K) -> ``student_group`` (column J + the group) -> ``name`` (surname + name
    + class + group, exactly one).  Row error codes: ``UNKNOWN`` ``AMBIGUOUS`` ``ID_MISMATCH`` ``DUPLICATE`` ``BAD_DATE`` ``BAD_AMOUNT`` ``FORMULA``
    ``ROW_CONFLICT`` and the item codes of ``OpeningItemResult``."""

    row: int
    membership_id: Optional[str] = None
    matched_by: Optional[MatchedBy] = None
    student_name: Optional[str] = None
    group_name: Optional[str] = None
    status: RowStatus
    code: Optional[str] = None
    message: Optional[str] = None
    kind: Optional[ItemKind] = None


class OpeningsImportFile(PayModel):
    rows_read: int = 0
    matched: int = 0
    changed: int = 0
    unchanged: int = 0
    errors: int = 0
    unknown: int = 0
    missing_in_file: int = 0  # live memberships of the table that the file does not mention (they stay as they were)


class OpeningsImportPreview(OpeningBatchPreview):
    """``POST /org/opening-balances/import/preview`` (multipart ``file``): the preview of the rows that are fine (the same shape as
    ``batch/preview``) + what happened to every row.  Rows with an error are NOT in ``batch_items`` (the rest applies).  ``batch_items`` are ready
    for ``POST /org/opening-balances/batch`` (with this ``digest``): the file is never uploaded again."""

    file: OpeningsImportFile
    rows: List[OpeningsImportRow]
    batch_items: List[OpeningBatchItem]
