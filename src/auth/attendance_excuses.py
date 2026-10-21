"""
ALGO - Attendance excuses ("уважительные пропуски").

A teacher (or an administrator) marks a student ``excused`` and must give a
reason.  The reason waits for the administrator's decision (confirm / reject),
who may also attach certificates (JPEG / PNG / PDF, up to 3 files, 2 MB each).

Two entry points:
  * ``plan_save`` / ``apply_save`` - called from
    ``AuthService.save_group_attendance`` inside the same transaction;
  * ``journal_payload`` - the read side of ``GET /org/groups/{id}/attendance``.
Everything else (list, review, reset, files) serves the admin-only router
``attendance_excuses_router.py``.

Privacy: logs carry ids only - never student names, reasons or file content.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.models.auth import (
    AttendanceExcuse,
    AttendanceExcuseFile,
    Group,
    GroupAttendance,
    GroupAttendanceAudit,
    Student,
    StudentGroup,
    StudentPayment,
    User,
)

logger = logging.getLogger(__name__)

EXCUSED = "excused"
REASON_MIN = 3
REASON_MAX = 300
NOTE_MAX = 500
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES_PER_EXCUSE = 3
MAX_PERIOD_DAYS = 93
DECISIONS = ("confirmed", "rejected")
REASON_REQUIRED_MESSAGE = "Укажите причину уважительного пропуска"
# Marks saved before reasons existed carry no excuse row; one is created for
# them on the next save, without asking the client for a reason.
LEGACY_REASON = "Отмечено до введения причин"


def _business_now() -> datetime:
    """Naive business-timezone "now" - the convention of the attendance tables."""
    from .service import BUSINESS_TIMEZONE  # lazy: service imports this module

    return datetime.now(BUSINESS_TIMEZONE).replace(tzinfo=None)


def _business_today() -> date:
    return _business_now().date()


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _iso_day(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


def _mark_before(excuse: "AttendanceExcuse") -> Dict[str, Any]:
    """Audit details for a decision mark that is about to be overwritten or cleared (ROADMAP #39).

    Empty when the excuse carries no mark (a pending one, a decision made before the mark existed): nothing is lost then.
    """
    if excuse.decided_after_deadline is None and excuse.deadline_at_decision is None:
        return {}
    return {
        "decided_after_deadline_before": excuse.decided_after_deadline,
        "deadline_at_decision_before": _iso_day(excuse.deadline_at_decision),
    }


def lesson_key(att: GroupAttendance) -> str:
    """The instance key used by the attendance journal (``date_lessonId``)."""
    return f"{att.lesson_date}_{att.lesson_id or f'lesson_{att.lesson_date}'}"


def _user_name(user: Optional[User]) -> Optional[str]:
    if not user:
        return None
    return f"{user.first_name} {user.last_name}".strip() or None


# ============================================================================
# Errors (mapped to HTTP by the router)
# ============================================================================


class ExcuseError(Exception):
    status_code = 400
    code: Optional[str] = None

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    @property
    def detail(self) -> Any:
        # The front's ``api()`` reads ``{code, message}``; raw uploads read a string.
        return {"code": self.code, "message": self.message} if self.code else self.message


class ExcuseNotFound(ExcuseError):
    status_code = 404


class ExcuseConflict(ExcuseError):
    status_code = 409


class ExcuseNotExcused(ExcuseConflict):
    code = "NOT_EXCUSED"


CHANGED_MESSAGE = "Решение по этому пропуску уже изменил другой администратор. Список обновлён — проверьте и повторите."


class ExcuseChanged(ExcuseConflict):
    """The excuse is not in the status the client decided on (another administrator got there first).

    Carries the CURRENT item so the client can refresh its list without another request.
    """

    code = "EXCUSE_CHANGED"

    def __init__(self, item: Optional[Any] = None) -> None:
        super().__init__(CHANGED_MESSAGE)
        self.item = item

    @property
    def detail(self) -> Any:
        body: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.item is not None:
            body["item"] = self.item.model_dump(mode="json")
        return body


class ExcuseTooLarge(ExcuseError):
    status_code = 413


class ExcuseUnsupportedType(ExcuseError):
    status_code = 415


class ExcuseInvalid(ExcuseError):
    status_code = 422


# ============================================================================
# Response models
# ============================================================================


class IdName(BaseModel):
    id: str
    name: Optional[str] = None


class ExcuseStudent(BaseModel):
    id: str
    full_name: str
    display_id: Optional[str] = None
    # Student / parents contact (the card's «Телефон ученика / родителей»); never money data.
    phone: Optional[str] = None


class ExcuseFileInfo(BaseModel):
    id: str
    filename: Optional[str] = None
    content_type: str
    size_bytes: int


class ExcuseDateDeadline(BaseModel):
    """Confirm by a calendar day (``days_left`` is negative once it has passed)."""

    kind: Literal["date"] = "date"
    date: str
    overdue: bool
    days_left: int


class ExcuseLessonsDeadline(BaseModel):
    """Confirm by the Nth-from-last lesson of the package (N = excuse_deadline_lessons); counts only.

    ``lessons_left > N``: normal; ``== N``: ``due_now`` (the last lesson to confirm);
    ``< N``: ``overdue``.  N is capped at the package size minus one; with N == 0,
    ``lessons_left == 1`` is ``due_now`` and ``0`` is ``overdue``.  ``urgent`` = due now or overdue.
    """

    kind: Literal["lessons"] = "lessons"
    lessons_left: int
    urgent: bool
    due_now: bool
    overdue: bool


class ExcuseItem(BaseModel):
    id: str
    attendance_id: str
    lesson_date: str
    lesson_key: str
    group: IdName
    teacher: Optional[IdName] = None
    student: ExcuseStudent
    reason: str
    reason_by: IdName
    reason_at: Optional[str] = None
    status: str
    reviewed_by: Optional[IdName] = None
    reviewed_at: Optional[str] = None
    review_note: Optional[str] = None
    files: List[ExcuseFileInfo] = Field(default_factory=list)
    # Pending excuses only (a decided one has nothing left to confirm); ``None``
    # also when it could not be worked out - the client then falls back to the month end.
    deadline: Optional[Union[ExcuseDateDeadline, ExcuseLessonsDeadline]] = Field(
        default=None, discriminator="kind"
    )
    # How the student is billed in THIS group: the mode of his latest live payment (``payment_status.billing_mode``),
    # not the centre setting.  ``package`` - a confirmed excuse gives the lesson back to the package; ``calendar`` -
    # nothing is written off; ``None`` - no live payment, so there is no package line.  Never an amount.
    billing_mode: Optional[Literal["calendar", "package"]] = None
    # The decision mark (ROADMAP #39).  A decision AFTER the confirmation deadline is allowed but marked: at the moment of
    # the decision the deadline is worked out exactly as the pending list shows it.  ``deadline_at_decision`` is that
    # deadline as a business day (YYYY-MM-DD, Tashkent; ``None`` for a lessons-package deadline, which has no day);
    # ``decided_after_deadline`` is ``True`` / ``False`` for a decision made after / on time.  Both are ``None`` for a
    # pending excuse and for a decision whose deadline is unknown (made before the mark existed, or not workable).
    # The mark belongs to the DECISION: a repeated decision that only edits the note keeps it (and ``reviewed_by`` /
    # ``reviewed_at``); a changed decision measures again; the journal row keeps the mark it overwrites.
    decided_after_deadline: Optional[bool] = None
    deadline_at_decision: Optional[str] = None


class ExcuseListResponse(BaseModel):
    items: List[ExcuseItem]
    total: int
    pending_total: int


class ExcuseSummaryResponse(BaseModel):
    pending_total: int
    # Lessons still to be closed (started-unfinished + never-started), org-wide.
    # ``None`` = could not be counted right now; clients keep their last value.
    open_lessons_total: Optional[int] = None


STATUS_PATTERN = "^(pending|confirmed|rejected)$"


class ReviewRequest(BaseModel):
    decision: str = Field(..., pattern="^(confirmed|rejected)$")
    note: Optional[str] = Field(None, max_length=NOTE_MAX)
    # Optimistic concurrency: the status the client SAW when it decided.  If the excuse is in another status by the time
    # the row is locked (another administrator decided first), nothing is written and the answer is 409 EXCUSE_CHANGED.
    # Absent = the old behaviour (the decision overwrites).
    expected_status: Optional[str] = Field(None, pattern=STATUS_PATTERN)


class ResetRequest(BaseModel):
    expected_status: Optional[str] = Field(None, pattern=STATUS_PATTERN)


class BulkReviewRequest(ReviewRequest):
    ids: List[UUID] = Field(..., min_length=1, max_length=200)


class BulkSkipped(BaseModel):
    id: str
    reason: str
    # ``EXCUSE_CHANGED`` = the item is not in the ``expected_status`` the client saw; absent for the other reasons.
    code: Optional[str] = None


class BulkReviewResponse(BaseModel):
    updated: int
    skipped: List[BulkSkipped]


# ============================================================================
# Files
# ============================================================================


def sniff_content_type(data: bytes) -> Optional[str]:
    """Content type from the leading bytes - the client's name/type is ignored."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}
_FILENAME_STRIP = re.compile(r'[\x00-\x1f\x7f"\\/:*?<>|]+')


def safe_filename(raw: Optional[str], content_type: str, default: str = "spravka") -> str:
    """Display name only (never a path): no separators or control characters."""
    name = _FILENAME_STRIP.sub("_", (raw or "").replace("\\", "/").split("/")[-1]).strip(" ._")
    if not name:
        name = default
    ext = _EXTENSIONS[content_type]
    if not name.lower().endswith("." + ext) and not (ext == "jpg" and name.lower().endswith(".jpeg")):
        name = f"{name[:190]}.{ext}"
    return name[:200]


# ============================================================================
# Save-time plan (called from AuthService.save_group_attendance)
# ============================================================================


@dataclass
class ExcusePlan:
    create: Dict[str, str] = field(default_factory=dict)  # student_id -> reason
    update: Dict[str, str] = field(default_factory=dict)  # student_id -> new reason
    delete: List[AttendanceExcuse] = field(default_factory=list)


def _clean_reason(raw: Any) -> str:
    reason = (raw or "").strip() if isinstance(raw, str) else ""
    if len(reason) < REASON_MIN or len(reason) > REASON_MAX:
        raise ValueError(
            REASON_REQUIRED_MESSAGE
            if not reason
            else f"Причина должна содержать от {REASON_MIN} до {REASON_MAX} символов"
        )
    return reason


class AttendanceExcuseService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---- write side of the journal ------------------------------------

    def plan_save(
        self,
        att: Optional[GroupAttendance],
        records: Dict[str, str],
        excuse_reasons: Optional[Dict[Any, str]],
    ) -> ExcusePlan:
        """Validate reasons for the new ``records``; performs no writes."""
        reasons = {str(k): v for k, v in (excuse_reasons or {}).items()}
        existing: Dict[str, AttendanceExcuse] = {}
        if att is not None and att.id is not None:
            existing = {
                str(e.student_id): e
                for e in self.db.query(AttendanceExcuse)
                .filter(AttendanceExcuse.attendance_id == att.id)
                .all()
            }
        plan = ExcusePlan()
        before = (att.records or {}) if att is not None else {}
        excused_now = {sid for sid, state in records.items() if state == EXCUSED}
        for sid in sorted(excused_now):
            if sid not in existing:
                if before.get(sid) == EXCUSED and not reasons.get(sid):
                    # Already excused before this release: no transition, so
                    # no reason is demanded.
                    plan.create[sid] = LEGACY_REASON
                else:
                    plan.create[sid] = _clean_reason(reasons.get(sid))
            elif sid in reasons:
                reason = _clean_reason(reasons[sid])
                if reason != existing[sid].reason:
                    plan.update[sid] = reason
        # No longer excused (or the lesson was cancelled): the excuse goes away
        # together with its files.
        plan.delete = [e for sid, e in existing.items() if sid not in excused_now]
        return plan

    def apply_save(self, att: GroupAttendance, plan: ExcusePlan, actor_id: str, now: datetime) -> None:
        """Persist ``plan``; ``att`` must already be flushed (has an id)."""
        for excuse in plan.delete:
            files_removed = (
                self.db.query(func.count(AttendanceExcuseFile.id))
                .filter(AttendanceExcuseFile.excuse_id == excuse.id)
                .scalar()
                or 0
            )
            self._audit(
                att, actor_id, "excuse_removed",
                {
                    "excuse_id": str(excuse.id),
                    "student_id": str(excuse.student_id),
                    "status_before": excuse.status,
                    "files_removed": files_removed,
                },
            )
            self.db.delete(excuse)
        if plan.update:
            rows = (
                self.db.query(AttendanceExcuse)
                .filter(
                    AttendanceExcuse.attendance_id == att.id,
                    AttendanceExcuse.student_id.in_([UUID(s) for s in plan.update]),
                )
                .all()
            )
            for row in rows:
                row.reason = plan.update[str(row.student_id)]
                row.reason_by = actor_id
                row.reason_at = now
                if row.status != "pending":
                    # A confirmed/rejected reason must not be replaced silently:
                    # the new text goes back to the administrator.
                    self._audit(
                        att, actor_id, "excuse_reset",
                        {
                            "excuse_id": str(row.id),
                            "student_id": str(row.student_id),
                            "decision_before": row.status,
                            "decision_after": "pending",
                            "note": row.review_note,
                            "reason_changed": True,
                            **_mark_before(row),
                        },
                    )
                    row.status = "pending"
                    row.reviewed_by = None
                    row.reviewed_at = None
                    row.review_note = None
                    row.decided_after_deadline = None
                    row.deadline_at_decision = None
        for sid, reason in plan.create.items():
            self.db.add(
                AttendanceExcuse(
                    org_id=att.org_id,
                    attendance_id=att.id,
                    student_id=UUID(sid),
                    reason=reason,
                    reason_by=actor_id,
                    reason_at=now,
                    status="pending",
                )
            )

    # ---- read side of the journal -------------------------------------

    def journal_payload(
        self,
        org_id: str,
        attendance_keys: Dict[UUID, str],
        *,
        include_review_note: bool,
        include_billing_mode: bool = False,
    ) -> tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, Dict[str, Any]]]:
        """``excuses`` and ``corrections`` maps keyed by the journal's lesson key.

        The administrator-only parts: ``review_note`` with the decision mark ``decided_after_deadline`` /
        ``deadline_at_decision`` (``include_review_note``) and ``billing_mode`` (``include_billing_mode``, the mode of
        the student's latest live payment in the group, see :class:`ExcuseItem`) - a teacher gets none of them,
        and for him no payment is read."""
        excuses: Dict[str, Dict[str, Dict[str, Any]]] = {}
        corrections: Dict[str, Dict[str, Any]] = {}
        if not attendance_keys:
            return excuses, corrections
        att_ids = list(attendance_keys)

        rows = (
            self.db.query(AttendanceExcuse)
            .filter(AttendanceExcuse.org_id == org_id, AttendanceExcuse.attendance_id.in_(att_ids))
            .all()
        )
        audits = (
            self.db.query(GroupAttendanceAudit)
            .filter(
                GroupAttendanceAudit.org_id == org_id,
                GroupAttendanceAudit.attendance_id.in_(att_ids),
                GroupAttendanceAudit.action == "corrected",
            )
            .order_by(GroupAttendanceAudit.created_at.desc())
            .all()
        )
        latest_audit: Dict[UUID, GroupAttendanceAudit] = {}
        for audit in audits:
            latest_audit.setdefault(audit.attendance_id, audit)

        user_ids = {r.reason_by for r in rows} | {r.reviewed_by for r in rows if r.reviewed_by}
        user_ids |= {a.actor_id for a in latest_audit.values()}
        users = self._users(user_ids)
        with_files = set()
        if rows:
            with_files = {
                excuse_id
                for (excuse_id,) in self.db.query(AttendanceExcuseFile.excuse_id)
                .filter(AttendanceExcuseFile.excuse_id.in_([r.id for r in rows]))
                .distinct()
                .all()
            }

        modes: Dict[tuple, Optional[str]] = {}
        group_of: Dict[UUID, UUID] = {}
        if include_billing_mode and rows:
            group_of = dict(
                self.db.query(GroupAttendance.id, GroupAttendance.group_id)
                .filter(GroupAttendance.id.in_({r.attendance_id for r in rows}))
                .all()
            )
            modes = self._billing_modes(org_id, {(r.student_id, group_of[r.attendance_id]) for r in rows})

        for row in rows:
            key = attendance_keys.get(row.attendance_id)
            if key is None:
                continue
            entry: Dict[str, Any] = {
                "id": str(row.id),
                "status": row.status,
                "reason": row.reason,
                "reason_by_name": _user_name(users.get(row.reason_by)),
                "reason_at": _iso(row.reason_at),
                "reviewed_by_name": _user_name(users.get(row.reviewed_by)) if row.reviewed_by else None,
                "reviewed_at": _iso(row.reviewed_at),
                "has_files": row.id in with_files,
            }
            # The administrator's note and the decision mark (#39) are not for teachers.
            if include_review_note:
                entry["review_note"] = row.review_note
                entry["decided_after_deadline"] = row.decided_after_deadline
                entry["deadline_at_decision"] = _iso_day(row.deadline_at_decision)
            if include_billing_mode:
                entry["billing_mode"] = modes.get((row.student_id, group_of.get(row.attendance_id)))
            excuses.setdefault(key, {})[str(row.student_id)] = entry

        for att_id, audit in latest_audit.items():
            key = attendance_keys.get(att_id)
            if key is None:
                continue
            corrections[key] = {
                "by_name": _user_name(users.get(audit.actor_id)),
                "at": _iso(audit.created_at),
                "reason": audit.correction_reason,
            }
        return excuses, corrections

    def _users(self, ids) -> Dict[UUID, User]:
        ids = {i for i in ids if i}
        if not ids:
            return {}
        return {u.id: u for u in self.db.query(User).filter(User.id.in_(ids)).all()}

    # ---- admin: list / summary ----------------------------------------

    def summary(self, org_id: str) -> ExcuseSummaryResponse:
        return ExcuseSummaryResponse(
            pending_total=self._pending_count(org_id),
            open_lessons_total=self._open_lessons_count(org_id),
        )

    def _open_lessons_count(self, org_id: str) -> Optional[int]:
        """Open lessons for the sidebar badge; never breaks the summary (None on failure)."""
        from .service import AuthService  # lazy: service imports this module

        try:
            return len(
                AuthService(self.db).get_open_attendance_actions(
                    org_id, actor_id=None, is_org_admin=True
                )
            )
        except Exception:
            logger.exception("Could not count open lessons for org %s", org_id)
            try:
                self.db.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            return None

    def _pending_count(self, org_id: str) -> int:
        return (
            self.db.query(func.count(AttendanceExcuse.id))
            .filter(AttendanceExcuse.org_id == org_id, AttendanceExcuse.status == "pending")
            .scalar()
            or 0
        )

    @staticmethod
    def resolve_period(date_from: Optional[str], date_to: Optional[str]) -> tuple[str, str]:
        """Both bounds or neither (the current month); at most 93 days."""
        if bool(date_from) != bool(date_to):
            raise ExcuseInvalid("date_from и date_to передаются вместе")

        def parse(value: str, name: str) -> date:
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ExcuseInvalid(f"{name} должен иметь формат YYYY-MM-DD") from exc
            if parsed.isoformat() != value:
                raise ExcuseInvalid(f"{name} должен иметь формат YYYY-MM-DD")
            return parsed

        if date_from and date_to:
            start, end = parse(date_from, "date_from"), parse(date_to, "date_to")
        else:
            today = _business_today()
            start = today.replace(day=1)
            end = (date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)) - timedelta(days=1)
        if end < start:
            raise ExcuseInvalid("date_to не может быть раньше date_from")
        if (end - start).days + 1 > MAX_PERIOD_DAYS:
            raise ExcuseInvalid(f"Период не может превышать {MAX_PERIOD_DAYS} дня")
        return start.isoformat(), end.isoformat()

    def _base_query(self, org_id: str):
        return (
            self.db.query(AttendanceExcuse, GroupAttendance, Group, Student)
            .join(GroupAttendance, GroupAttendance.id == AttendanceExcuse.attendance_id)
            .join(Group, Group.id == GroupAttendance.group_id)
            .outerjoin(Student, Student.id == AttendanceExcuse.student_id)
            .filter(AttendanceExcuse.org_id == org_id, GroupAttendance.org_id == org_id)
        )

    def list_excuses(
        self,
        org_id: str,
        *,
        status: str = "pending",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        group_id: Optional[UUID] = None,
        teacher_id: Optional[UUID] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ExcuseListResponse:
        start, end = self.resolve_period(date_from, date_to)
        query = self._base_query(org_id).filter(
            GroupAttendance.lesson_date >= start, GroupAttendance.lesson_date <= end
        )
        if group_id:
            query = query.filter(GroupAttendance.group_id == group_id)
        if teacher_id:
            query = query.filter(func.coalesce(GroupAttendance.teacher_id, Group.teacher_id) == teacher_id)
        term = (search or "").strip()
        if term:
            pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.filter(
                or_(
                    func.concat(Student.last_name, " ", Student.first_name).ilike(pattern, escape="\\"),
                    func.concat(Student.first_name, " ", Student.last_name).ilike(pattern, escape="\\"),
                    Student.display_id.ilike(pattern, escape="\\"),
                )
            )
        # The tab counter follows the same period / group / teacher / search.
        pending_total = query.filter(AttendanceExcuse.status == "pending").count()
        if status != "all":
            query = query.filter(AttendanceExcuse.status == status)
        total = query.count()
        rows = (
            query.order_by(
                GroupAttendance.lesson_date.desc(),
                AttendanceExcuse.reason_at.desc(),
                AttendanceExcuse.id,
            )
            .limit(limit)
            .offset(offset)
            .all()
        )
        return ExcuseListResponse(
            items=self._items(rows), total=total, pending_total=pending_total
        )

    def _items(self, rows) -> List[ExcuseItem]:
        if not rows:
            return []
        excuse_ids = [r[0].id for r in rows]
        files: Dict[UUID, List[ExcuseFileInfo]] = {}
        for f in (
            self.db.query(
                AttendanceExcuseFile.id,
                AttendanceExcuseFile.excuse_id,
                AttendanceExcuseFile.filename,
                AttendanceExcuseFile.content_type,
                AttendanceExcuseFile.size_bytes,
            )
            .filter(AttendanceExcuseFile.excuse_id.in_(excuse_ids))
            .order_by(AttendanceExcuseFile.uploaded_at, AttendanceExcuseFile.id)
            .all()
        ):
            files.setdefault(f.excuse_id, []).append(
                ExcuseFileInfo(
                    id=str(f.id), filename=f.filename, content_type=f.content_type, size_bytes=f.size_bytes
                )
            )
        teacher_ids = {a.teacher_id or g.teacher_id for _, a, g, _ in rows}
        user_ids = teacher_ids | {e.reason_by for e, *_ in rows} | {e.reviewed_by for e, *_ in rows}
        users = self._users(user_ids)
        org_id = rows[0][0].org_id
        pairs = {(e.student_id, a.group_id) for e, a, _, _ in rows}
        context = self._payment_context(org_id, pairs)
        deadlines = self._deadlines(rows, context)
        modes = self._modes_of(context, pairs)

        items: List[ExcuseItem] = []
        for excuse, att, group, student in rows:
            teacher_uuid = att.teacher_id or group.teacher_id
            full_name = (
                f"{student.last_name or ''} {student.first_name or ''}".strip() if student else ""
            ) or "Ученик"
            items.append(
                ExcuseItem(
                    id=str(excuse.id),
                    attendance_id=str(att.id),
                    lesson_date=att.lesson_date,
                    lesson_key=lesson_key(att),
                    group=IdName(id=str(group.id), name=group.name),
                    teacher=IdName(id=str(teacher_uuid), name=_user_name(users.get(teacher_uuid)))
                    if teacher_uuid
                    else None,
                    student=ExcuseStudent(
                        id=str(excuse.student_id),
                        full_name=full_name,
                        display_id=student.display_id if student else None,
                        phone=(student.student_phone or None) if student else None,
                    ),
                    reason=excuse.reason,
                    reason_by=IdName(id=str(excuse.reason_by), name=_user_name(users.get(excuse.reason_by))),
                    reason_at=_iso(excuse.reason_at),
                    status=excuse.status,
                    reviewed_by=IdName(id=str(excuse.reviewed_by), name=_user_name(users.get(excuse.reviewed_by)))
                    if excuse.reviewed_by
                    else None,
                    reviewed_at=_iso(excuse.reviewed_at),
                    review_note=excuse.review_note,
                    files=files.get(excuse.id, []),
                    deadline=deadlines.get(excuse.id),
                    billing_mode=modes.get((excuse.student_id, att.group_id)),
                    decided_after_deadline=excuse.decided_after_deadline,
                    deadline_at_decision=_iso_day(excuse.deadline_at_decision),
                )
            )
        return items

    def _payment_context(self, org_id: UUID, pairs, *, propagate: bool = False) -> Optional[SimpleNamespace]:
        """Memberships of the ``(student_id, group_id)`` pairs and what their money pays for, loaded ONCE for the whole page.

        PAY-2: the account of every membership is run by the ledger (``ledger_service``, a fixed number of queries however
        many pairs there are); ``billing_mode.coverage_records`` turns it into the synthetic ``PaymentRecord``s the deadline
        code reads (one per continuous paid range, or one package with the covered lessons).  ``None`` if it could not be
        loaded (both then simply go missing); ``propagate`` lets the error through instead, WITHOUT the rollback (a caller
        holding a row lock rolls back to its own savepoint: see ``_decision_mark``).
        Fields: ``membership_of`` ``{(student, group): membership}``, ``records`` ``{membership: [PaymentRecord]}`` and
        ``modes`` ``{membership: "calendar" | "package" | None}``.
        """
        from . import billing_ledger as bl
        from . import ledger_service as ls
        from . import payment_status as ps

        try:
            membership_of: Dict[tuple, UUID] = {}
            if pairs:
                membership_of = {
                    (row.student_id, row.group_id): row.id
                    for row in self.db.query(StudentGroup.id, StudentGroup.student_id, StudentGroup.group_id)
                    .filter(
                        StudentGroup.student_id.in_({student for student, _ in pairs}),
                        StudentGroup.group_id.in_({group for _, group in pairs}),
                    )
                    .all()
                }
            records: Dict[UUID, list] = {}
            modes: Dict[UUID, Optional[str]] = {}
            if membership_of:
                loader = ls.LedgerLoader(self.db, str(org_id), ps.business_today())
                for entry in loader.run(list(set(membership_of.values()))):
                    records[entry.membership.id] = bl.coverage_records(entry.ledger)
                    has_money = bool(entry.inp.credits)
                    modes[entry.membership.id] = (
                        None if not has_money else ("package" if entry.ledger.mode == bl.LESSONS else "calendar"))
            return SimpleNamespace(membership_of=membership_of, live={}, records=records, modes=modes)
        except Exception:
            if propagate:
                raise
            logger.exception("Could not load the payments of the excuses")
            try:
                self.db.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            return None

    @staticmethod
    def _modes_of(context: Optional[SimpleNamespace], pairs) -> Dict[tuple, Optional[str]]:
        """``{(student, group): "calendar" | "package" | None}`` - the mode of the account (``None``: no money yet)."""
        if context is None:
            return {}
        return {pair: context.modes.get(context.membership_of.get(pair)) for pair in pairs}

    def _billing_modes(self, org_id: UUID, pairs) -> Dict[tuple, Optional[str]]:
        return self._modes_of(self._payment_context(org_id, pairs), pairs)

    def _deadlines(self, rows, context: Optional[SimpleNamespace]) -> Dict[UUID, Dict[str, Any]]:
        """Confirmation deadline of every pending excuse of the page, in a fixed number of queries.

        Memberships and live payments (``context``), the payment settings and (lessons mode only)
        the counted lessons are each loaded once for the whole page.  Only dates
        and lesson counts leave this method; a failure yields no deadline.
        """
        pending = [(e, a, g, s) for e, a, g, s in rows if e.status == "pending"]
        if not pending or context is None:
            return {}
        try:
            return self._pending_deadlines(pending, context)
        except Exception:
            logger.exception("Could not work out excuse deadlines")
            try:
                self.db.rollback()
            except Exception:  # pragma: no cover - defensive
                pass
            return {}

    def _pending_deadlines(
        self, pending, context: SimpleNamespace, today: Optional[date] = None
    ) -> Dict[UUID, Dict[str, Any]]:
        """``today`` - the business day to measure ``overdue`` against (default: now); a decision passes the day of the
        very instant it is recorded at (see ``_apply_review``)."""
        from . import excuse_deadline as dl
        from .payments_service import PaymentService  # lazy: it imports the models only, but is heavy

        org_id = pending[0][0].org_id
        settings = PaymentService(self.db).get_settings(str(org_id))
        today = today or _business_today()

        membership_of, records = context.membership_of, context.records

        # The lessons FIFO is needed only for lessons that no calendar payment covers.
        lesson_dates: Dict[UUID, date] = {}
        lessons_members = []
        seen_members = set()
        for excuse, att, group, student in pending:
            try:
                lesson_dates[excuse.id] = date.fromisoformat(att.lesson_date)
            except (TypeError, ValueError):
                continue
            membership = membership_of.get((excuse.student_id, att.group_id))
            if (
                membership is not None
                and membership not in seen_members
                and dl.needs_lessons_pool(records.get(membership, ()), lesson_dates[excuse.id])
            ):
                seen_members.add(membership)
                lessons_members.append(
                    (SimpleNamespace(id=membership, student_id=excuse.student_id), student, group)
                )
        # ``pools`` are the DATES of the counted lessons (what the FIFO hands out); ``counted_rows`` says WHICH lessons
        # they are - the attendance rows - because a date cannot: a student can have two lessons on one day.
        pools: Dict[UUID, list] = {}
        counted_rows: Dict[UUID, set] = {}
        detail: Dict[UUID, list] = {}
        if lessons_members:
            from . import ledger_service as ls

            since = ls.load_settings(self.db, str(org_id)).accounting_start
            pools = PaymentService(self.db)._counted_lesson_dates(
                str(org_id), lessons_members, {}, since=since, detail_out=detail)
            counted_rows = {
                mid: {attendance_id for _day, _lesson_id, attendance_id in items} for mid, items in detail.items()
            }

        # The lessons of one day are handed to the packages in hand-out order (scheduled start, then lesson id), so
        # which package covers THIS lesson depends on its place among the lessons of its day - by the attendance row.
        ranks: Dict[UUID, int] = {}
        unplaced: Dict[UUID, tuple] = {}  # not counted yet (unfinished / confirmed): its place needs the schedule
        for excuse, att, group, student in pending:
            lesson_date = lesson_dates.get(excuse.id)
            membership = membership_of.get((excuse.student_id, att.group_id))
            same_day = [
                (lesson_id, attendance_id)
                for day, lesson_id, attendance_id in detail.get(membership, ())
                if day == lesson_date
            ]
            attendance_ids = [attendance_id for _, attendance_id in same_day]
            if att.id in attendance_ids:
                ranks[excuse.id] = attendance_ids.index(att.id)
            elif same_day:
                unplaced[excuse.id] = (group, lesson_date, att, same_day)
        if unplaced:
            starts = PaymentService(self.db)._starts_of_days(
                {(group.id, day): group for group, day, _, _ in unplaced.values()}
            )
            for excuse_id, (group, day, att, same_day) in unplaced.items():
                day_starts = starts.get((group.id, day), {})  # a lesson whose time is unknown counts as "before"

                def hand_out_key(lesson_id: str):
                    return day_starts.get(lesson_id, datetime.min), lesson_id

                own = hand_out_key(att.lesson_id or "")
                ranks[excuse_id] = sum(1 for lesson_id, _ in same_day if hand_out_key(lesson_id) < own)

        result: Dict[UUID, Dict[str, Any]] = {}
        for excuse, att, group, student in pending:
            lesson_date = lesson_dates.get(excuse.id)
            if lesson_date is None:
                continue
            membership = membership_of.get((excuse.student_id, att.group_id))
            result[excuse.id] = dl.excuse_deadline(
                lesson_date=lesson_date,
                today=today,
                payments=records.get(membership, ()),
                counted_dates=pools.get(membership, ()),
                # THIS lesson's own entry in the pool: decided by the very rule that built the pool (rule 4.3 -
                # completed, in the roster, a counted mark, no CONFIRMED excuse), by the attendance row.  A confirmed
                # excuse that is being re-reviewed has no entry, even if another lesson of the same day has one.
                lesson_counted=att.id in counted_rows.get(membership, ()),
                lesson_rank=ranks.get(excuse.id),
                deadline_days=settings.excuse_deadline_days,
                deadline_lessons=settings.excuse_deadline_lessons,
            )
        return result

    def _item(self, org_id: str, excuse_id: UUID) -> ExcuseItem:
        rows = self._base_query(org_id).filter(AttendanceExcuse.id == excuse_id).all()
        return self._items(rows)[0]

    # ---- admin: decisions ----------------------------------------------

    def _lock_excuse(self, org_id: str, excuse_id: UUID) -> AttendanceExcuse:
        excuse = (
            self.db.query(AttendanceExcuse)
            .filter(AttendanceExcuse.id == excuse_id, AttendanceExcuse.org_id == org_id)
            .with_for_update()
            .first()
        )
        if not excuse:
            raise ExcuseNotFound("Заявка на уважительный пропуск не найдена")
        return excuse

    def _lesson(self, excuse: AttendanceExcuse) -> GroupAttendance:
        att = self.db.get(GroupAttendance, excuse.attendance_id)
        if not att or str(att.org_id) != str(excuse.org_id):
            raise ExcuseNotFound("Урок не найден")
        return att

    def _audit(
        self, att: GroupAttendance, actor_id: str, action: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Journal row: the lesson state and its marks are unchanged.

        ``details`` says what exactly happened to which excuse; it holds ids,
        statuses and the administrator's note - never a student's name.
        """
        records = dict(att.records or {})
        self.db.add(
            GroupAttendanceAudit(
                attendance_id=att.id,
                org_id=att.org_id,
                group_id=att.group_id,
                actor_id=actor_id,
                action=action,
                before_status=att.status,
                after_status=att.status or "completed",
                before_records=records,
                after_records=records,
                details=details,
            )
        )

    def _decision_mark(
        self, excuse: AttendanceExcuse, att: GroupAttendance, today: date
    ) -> tuple[Optional[date], Optional[bool]]:
        """``(deadline_at_decision, decided_after_deadline)`` of a decision taken on the business day ``today`` (ROADMAP #39).

        ``today`` is the day of the very instant the decision is stored at (``reviewed_at``): one reading of the clock
        for both, so a decision taken around midnight cannot be recorded on one day and measured on the next.

        The deadline is the one the pending list shows - the very same ``_pending_deadlines`` /
        ``excuse_deadline.excuse_deadline`` with the same payment settings and the business day of Tashkent - so the
        mark can never disagree with the «просрочено» hint the administrator saw.  A calendar deadline gives its day and
        ``decided_after_deadline = today > day``; a lessons-package deadline has no day: ``(None, overdue)``.

        ``(None, None)`` = unknown: the deadline could not be worked out.  A decision must not fail because of that (it is
        logged); the reads run inside a SAVEPOINT, so an error rolls back that alone and the row lock of the caller and its
        pending writes stay.  Reads only - nothing is committed or locked here.
        """
        try:
            with self.db.begin_nested():
                row = self._base_query(excuse.org_id).filter(AttendanceExcuse.id == excuse.id).first()
                if row is None:
                    return None, None
                context = self._payment_context(
                    excuse.org_id, {(excuse.student_id, att.group_id)}, propagate=True
                )
                deadline = self._pending_deadlines([tuple(row)], context, today).get(excuse.id)
        except Exception:
            logger.exception("Could not work out the deadline of excuse %s at its decision", excuse.id)
            return None, None
        if not deadline:  # no parsable lesson date
            return None, None
        day = date.fromisoformat(deadline["date"]) if deadline.get("kind") == "date" else None
        return day, bool(deadline["overdue"])

    def _apply_review(
        self, excuse: AttendanceExcuse, att: GroupAttendance, actor_id: str, decision: str, note: Optional[str]
    ) -> None:
        if (att.records or {}).get(str(excuse.student_id)) != EXCUSED:
            raise ExcuseNotExcused("Ученик уже не отмечен как пропустивший по уважительной причине")
        decision_before = excuse.status
        note = (note or "").strip() or None
        if decision == decision_before:
            # The same decision again (the note is edited): nothing was decided now, so the decision stays as it was - its
            # author, its time (``reviewed_by`` / ``reviewed_at``, the «кто и когда принял» of the list) and its mark, which
            # records WHEN THE DECISION WAS TAKEN and must not be re-measured against today's deadline.  Only the note
            # changes; who edited it and when is in the journal row below (its actor and time).
            excuse.review_note = note
            self._audit(
                att, actor_id, "excuse_reviewed",
                {
                    "excuse_id": str(excuse.id),
                    "student_id": str(excuse.student_id),
                    "decision_before": decision_before,
                    "decision_after": decision,
                    "note": excuse.review_note,
                },
            )
            return
        # ONE reading of the clock: the stored time of the decision and the day its deadline is measured against.
        now = _business_now()
        # The mark (#39) is worked out BEFORE the decision is written, from the state the list showed, and under the
        # row lock the caller already holds; a changed decision overwrites the previous mark (kept in the journal row).
        mark_before = _mark_before(excuse)
        deadline_day, after_deadline = self._decision_mark(excuse, att, now.date())
        excuse.status = decision
        excuse.reviewed_by = actor_id
        excuse.reviewed_at = now
        excuse.review_note = note
        excuse.deadline_at_decision = deadline_day
        excuse.decided_after_deadline = after_deadline
        self._audit(
            att, actor_id, "excuse_reviewed",
            {
                "excuse_id": str(excuse.id),
                "student_id": str(excuse.student_id),
                "decision_before": decision_before,
                "decision_after": decision,
                "note": excuse.review_note,
                **mark_before,
            },
        )

    def _check_expected(self, org_id: str, excuse: AttendanceExcuse, expected_status: Optional[str]) -> None:
        """Under the row lock: the excuse must still be in the status the client decided on (if it said which).

        On a mismatch nothing is written: the transaction is rolled back (the lock is released) and the CURRENT item
        goes out with the 409.
        """
        if expected_status is None or excuse.status == expected_status:
            return
        item = self._item(org_id, excuse.id)
        self.db.rollback()
        raise ExcuseChanged(item)

    def review(
        self, org_id: str, excuse_id: UUID, actor_id: str, decision: str, note: Optional[str],
        expected_status: Optional[str] = None,
    ) -> ExcuseItem:
        if decision not in DECISIONS:
            raise ExcuseInvalid("Решение: confirmed или rejected")
        excuse = self._lock_excuse(org_id, excuse_id)
        self._check_expected(org_id, excuse, expected_status)
        self._apply_review(excuse, self._lesson(excuse), actor_id, decision, note)
        self.db.commit()
        logger.info("Attendance excuse %s reviewed by %s: %s", excuse_id, actor_id, decision)
        return self._item(org_id, excuse_id)

    def review_bulk(
        self, org_id: str, ids: List[UUID], actor_id: str, decision: str, note: Optional[str],
        expected_status: Optional[str] = None,
    ) -> BulkReviewResponse:
        if decision not in DECISIONS:
            raise ExcuseInvalid("Решение: confirmed или rejected")
        updated = 0
        skipped: List[BulkSkipped] = []
        for excuse_id in dict.fromkeys(ids):
            try:
                excuse = self._lock_excuse(org_id, excuse_id)
                if expected_status is not None and excuse.status != expected_status:
                    # Another administrator changed it since the client's list: reported per item, nothing is written.
                    self.db.rollback()
                    skipped.append(BulkSkipped(id=str(excuse_id), reason=CHANGED_MESSAGE, code=ExcuseChanged.code))
                    continue
                if excuse.status != "pending":
                    # A stale list must not overwrite another administrator's decision.
                    raise ExcuseConflict("Решение по заявке уже принято")
                self._apply_review(excuse, self._lesson(excuse), actor_id, decision, note)
                self.db.commit()
                updated += 1
            except ExcuseError as exc:
                self.db.rollback()
                skipped.append(BulkSkipped(id=str(excuse_id), reason=exc.message))
        logger.info("Attendance excuses bulk %s by %s: updated=%d skipped=%d", decision, actor_id, updated, len(skipped))
        return BulkReviewResponse(updated=updated, skipped=skipped)

    def reset(
        self, org_id: str, excuse_id: UUID, actor_id: str, expected_status: Optional[str] = None
    ) -> ExcuseItem:
        excuse = self._lock_excuse(org_id, excuse_id)
        self._check_expected(org_id, excuse, expected_status)
        if excuse.status != "pending":
            details = {
                "excuse_id": str(excuse.id),
                "student_id": str(excuse.student_id),
                "decision_before": excuse.status,
                "decision_after": "pending",
                "note": excuse.review_note,
                **_mark_before(excuse),
            }
            excuse.status = "pending"
            excuse.reviewed_by = None
            excuse.reviewed_at = None
            excuse.review_note = None
            excuse.decided_after_deadline = None
            excuse.deadline_at_decision = None
            self._audit(self._lesson(excuse), actor_id, "excuse_reset", details)
            self.db.commit()
            logger.info("Attendance excuse %s reset by %s", excuse_id, actor_id)
        return self._item(org_id, excuse_id)

    # ---- admin: files ----------------------------------------------------

    def add_file(
        self, org_id: str, excuse_id: UUID, actor_id: str, data: bytes, filename: Optional[str]
    ) -> ExcuseFileInfo:
        excuse = self._lock_excuse(org_id, excuse_id)
        if not data:
            raise ExcuseInvalid("Файл пустой")
        if len(data) > MAX_FILE_BYTES:
            raise ExcuseTooLarge("Файл слишком большой (макс. 2 МБ)")
        content_type = sniff_content_type(data)
        if not content_type:
            raise ExcuseUnsupportedType("Допустимы только файлы JPEG, PNG или PDF")
        count = (
            self.db.query(func.count(AttendanceExcuseFile.id))
            .filter(AttendanceExcuseFile.excuse_id == excuse.id)
            .scalar()
            or 0
        )
        if count >= MAX_FILES_PER_EXCUSE:
            raise ExcuseConflict(f"К заявке можно приложить не более {MAX_FILES_PER_EXCUSE} файлов")
        stored = AttendanceExcuseFile(
            org_id=org_id,
            excuse_id=excuse.id,
            filename=safe_filename(filename, content_type),
            content_type=content_type,
            size_bytes=len(data),
            data=data,
            uploaded_by=actor_id,
            uploaded_at=_business_now(),
        )
        self.db.add(stored)
        self.db.flush()
        self._audit(
            self._lesson(excuse), actor_id, "excuse_file_added",
            {
                "excuse_id": str(excuse.id),
                "student_id": str(excuse.student_id),
                "file_id": str(stored.id),
                "filename": stored.filename,
                "size_bytes": stored.size_bytes,
            },
        )
        self.db.commit()
        logger.info("Attendance excuse file %s added to %s by %s", stored.id, excuse_id, actor_id)
        return ExcuseFileInfo(
            id=str(stored.id), filename=stored.filename, content_type=stored.content_type, size_bytes=stored.size_bytes
        )

    def get_file(self, org_id: str, file_id: UUID) -> AttendanceExcuseFile:
        stored = (
            self.db.query(AttendanceExcuseFile)
            .filter(AttendanceExcuseFile.id == file_id, AttendanceExcuseFile.org_id == org_id)
            .first()
        )
        if not stored:
            raise ExcuseNotFound("Файл не найден")
        return stored

    def delete_file(self, org_id: str, file_id: UUID, actor_id: str) -> None:
        stored = self.get_file(org_id, file_id)
        # Serialise with uploads to the same excuse.
        excuse = self._lock_excuse(org_id, stored.excuse_id)
        details = {
            "excuse_id": str(excuse.id),
            "student_id": str(excuse.student_id),
            "file_id": str(stored.id),
            "filename": stored.filename,
            "size_bytes": stored.size_bytes,
        }
        self.db.delete(stored)
        self.db.flush()
        self._audit(self._lesson(excuse), actor_id, "excuse_file_removed", details)
        self.db.commit()
        logger.info("Attendance excuse file %s removed by %s", file_id, actor_id)
