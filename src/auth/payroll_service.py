"""
ALGO - Payroll service (N13 P3).  Owner only; money lives here and in ``payroll_router.py``.

* The maths is ``payroll_engine`` (pure).  The payment rules are NOT repeated here: which lessons
  count and which payment covers them come from ``PaymentService.lesson_coverage`` - the code of the
  status page - and this module only reads attendance to know WHO taught a lesson and what each mark
  was.  A lesson is in a line if and only if the payments module counts it.
* One calculation reads the whole month for all the teachers asked for in a fixed number of queries
  (no query per teacher, group or student).
* A saved sheet is self-contained (``lines`` carry the marks) and never changes by itself.  Every
  change - fee, percent, "include unpaid", recalculation - is a new revision under ``SELECT ... FOR
  UPDATE`` and the ``revision`` the caller saw (409 ``SHEET_REVISION_CONFLICT`` otherwise); the
  history is append-only (``payroll_sheet_revisions``).  ``source_fingerprint`` hashes the data a
  calculation read, so "the data changed" is a comparison of two hashes.
* Logs carry ids and the actor, never amounts.
* The kind of lessons (``groups.lesson_type``) of a line is read from the group when the calculation
  runs, not from the day of the lesson.  Changing it is audited (``group_audit``) and flips ``stale`` of
  the sheets it touches (the kind is part of the data fingerprint), so a saved sheet never changes by
  itself and the owner is told to recalculate.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from uuid import UUID

from sqlalchemy import func, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from src.models.auth import (
    AttendanceExcuse,
    Group,
    GroupAttendance,
    Organization,
    OrganizationMember,
    PayrollSheet,
    PayrollSheetRevision,
    Student,
    StudentGroup,
    User,
)

from . import payment_status as ps
from . import payroll_engine as engine
from .payments_files import person_name, staff_name
from .payments_service import PaymentService
from .payroll_schemas import (
    CalculateRequest,
    CalculateResponse,
    DiffLine,
    GroupBlockOut,
    LessonTypeSuggestion,
    LineOut,
    MonthOverview,
    MonthSheetRef,
    MonthTeacher,
    MonthWarnings,
    PayrollSettings,
    RevisionOut,
    SheetBrief,
    SheetCreate,
    SheetDiff,
    SheetOut,
    SheetPatch,
    SheetWarnings,
)

logger = logging.getLogger(__name__)

MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
FIRST_YEAR = 2020
FINGERPRINT_VERSION = 1
MAX_LISTED_SHEETS = 300


# ============================================================================
# Errors (mapped to HTTP by the router)
# ============================================================================


class PayrollError(Exception):
    status_code = 400
    code: Optional[str] = None

    def __init__(self, message: str, *, code: Optional[str] = None, **extra: Any) -> None:
        self.message = message
        if code:
            self.code = code
        self.extra = extra
        super().__init__(message)

    @property
    def detail(self) -> Any:
        if not self.code:
            return self.message
        return {"code": self.code, "message": self.message, **self.extra}


class PayrollNotFound(PayrollError):
    status_code = 404


class PayrollInvalid(PayrollError):
    status_code = 422


class MonthInvalid(PayrollInvalid):
    code = "MONTH_INVALID"


class PercentRequired(PayrollInvalid):
    code = "PERCENT_REQUIRED"


class RevisionConflict(PayrollError):
    status_code = 409
    code = "SHEET_REVISION_CONFLICT"


class DataChanged(PayrollError):
    """The data moved between the preview the owner looked at and the save: save nothing."""

    status_code = 409
    code = "SHEET_DATA_CHANGED"


# ============================================================================
# Helpers
# ============================================================================


def _utcnow() -> datetime:
    """Naive UTC timestamp (the convention of the other audit-like columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def parse_month(text: str, today: Optional[date] = None) -> Tuple[date, date]:
    """``"2026-09"`` -> (first day, last day).  A malformed month or one that has not begun yet -> 422."""
    match = MONTH_RE.match(text or "")
    if not match:
        raise MonthInvalid("Месяц указывается в формате ГГГГ-ММ, например 2026-09")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12 or year < FIRST_YEAR:
        raise MonthInvalid("Некорректный месяц")
    first = date(year, month, 1)
    today = today or ps.business_today()
    if first > today.replace(day=1):
        raise MonthInvalid("Этот месяц ещё не наступил")
    return first, date(year, month, calendar.monthrange(year, month)[1])


def month_label(first: date) -> str:
    return first.strftime("%Y-%m")


def fingerprint_of(month: date, teacher_id: UUID, lines: List["LineSeed"]) -> str:
    """Stable hash of everything a calculation of one teacher's month read.

    Per line: the membership, the group and its kind of lessons, the fee of the membership, the
    pending-excuse count and every lesson column - date, id, state (counted / excused / unpaid / not
    in group) and the divisor.  It is what the payments module decided about each lesson, so a changed
    mark, excuse decision, payment, fee, teacher snapshot or divisor changes the hash; names, the
    order of the rows and the owner's overrides do not.
    """
    items = []
    for line in lines:
        ident = line.identity
        items.append(
            [
                ident["membership_id"],
                ident["group_id"],
                ident["lesson_type"],
                line.source_fee,
                line.pending_excused,
                sorted(
                    ([m.day.isoformat(), m.lesson_id, m.state, m.divisor or 0] for m in line.marks),
                    key=lambda item: (item[0], item[1], item[2], item[3]),
                ),
            ]
        )
    items.sort(key=lambda item: (item[0], item[1]))
    blob = json.dumps(
        {"v": FINGERPRINT_VERSION, "month": month.isoformat(), "teacher": str(teacher_id), "lines": items},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


# ---- what one calculation reads ------------------------------------------------------


@dataclass
class LineSeed:
    """A line before percent / overrides: identity, the lesson columns and the fee of the membership."""

    identity: Dict[str, Any]
    marks: List[engine.Mark]
    source_fee: Optional[int]
    pending_excused: int


@dataclass
class TeacherSeed:
    teacher_id: UUID
    lessons_count: int = 0
    lines: List[LineSeed] = field(default_factory=list)
    groups_by_type: Dict[str, Set[str]] = field(default_factory=dict)
    open_lessons: int = 0
    fingerprint: str = ""


@dataclass
class MonthSeed:
    first: date
    last: date
    divisor: int
    teachers: Dict[UUID, TeacherSeed] = field(default_factory=dict)
    lessons_without_teacher: int = 0


@dataclass
class _Lesson:
    attendance_id: UUID
    day: date
    lesson_id: str
    records: Dict[str, Any]
    roster: Optional[Set[str]]  # None = a legacy lesson without a snapshot: being marked is enough


@dataclass
class _Change:
    """What the owner decided about one line: ``fee`` is None when the fee of the membership applies."""

    fee: Optional[int] = None
    include_unpaid: bool = False


# ============================================================================
# Service
# ============================================================================


class PayrollService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------ settings

    def _block(self, org_id: str) -> Dict[str, Any]:
        raw = self.db.query(Organization.settings).filter(Organization.id == org_id).scalar()
        block = raw.get("payroll") if isinstance(raw, dict) else None
        return dict(block) if isinstance(block, dict) else {}

    def get_settings(self, org_id: str) -> PayrollSettings:
        block = self._block(org_id)
        try:
            return PayrollSettings(
                divisor=block.get("divisor", engine.DEFAULT_DIVISOR),
                updated_by=block.get("updated_by"),
                updated_at=block.get("updated_at"),
            )
        except ValueError:  # a hand-edited, out-of-range value must not break the page
            logger.warning("Invalid payroll settings stored for org %s; using defaults", org_id)
            return PayrollSettings()

    def divisor(self, org_id: str) -> int:
        return self.get_settings(org_id).divisor

    def update_settings(self, org_id: str, actor_id: str, divisor: int) -> PayrollSettings:
        org = self.db.query(Organization).filter(Organization.id == org_id).with_for_update().first()
        if not org:
            raise PayrollNotFound("Организация не найдена")
        block = dict(((org.settings or {}).get("payroll")) or {})
        block.update({"divisor": divisor, "updated_by": str(actor_id), "updated_at": _utcnow().isoformat()})
        org.settings = {**(org.settings or {}), "payroll": block}
        flag_modified(org, "settings")
        self.db.commit()
        logger.info("payroll settings updated org=%s actor=%s", org_id, actor_id)
        return self.get_settings(org_id)

    # ------------------------------------------------------------- reading

    def _open_lessons(self, org_id: str, first: date, last: date) -> Dict[str, int]:
        """Unfinished lessons of the month per teacher - the very list of "Незакрытые уроки"."""
        from .service import AuthService  # lazy: service is heavy

        try:
            actions = AuthService(self.db).get_open_attendance_actions(
                org_id, actor_id=None, is_org_admin=True
            )
        except Exception:
            logger.exception("Could not count open lessons for org %s", org_id)
            self.db.rollback()  # nothing else has been read yet
            return {}
        out: Dict[str, int] = {}
        low, high = first.isoformat(), last.isoformat()
        for action in actions:
            if action.teacherId and low <= action.lessonDate <= high:
                out[action.teacherId] = out.get(action.teacherId, 0) + 1
        return out

    def _lesson_times(self, groups: Iterable[Group], first: date, last: date) -> Dict[Tuple[UUID, str, str], str]:
        """``{(group, date, lesson_id): "10:00–11:00"}`` from the schedule (overrides and one-off lessons included)."""
        from .service import AuthService  # lazy: service is heavy

        groups = list(groups)
        if not groups:
            return {}
        service = AuthService(self.db)
        preload = service._preload_schedule_data([g.id for g in groups], date_from=first, date_to=last)
        times: Dict[Tuple[UUID, str, str], str] = {}
        for group in groups:
            for occurrence in service._resolved_occurrences_for_group(
                group, date_from=first, date_to=last, preloaded=preload
            ):
                start, end = occurrence.get("startTime"), occurrence.get("endTime")
                if start and end:
                    times[(group.id, occurrence["occurrenceDate"], str(occurrence["id"]))] = f"{start}–{end}"
        return times

    def _collect(
        self,
        org_id: str,
        first: date,
        last: date,
        *,
        teacher_ids: Optional[Set[UUID]] = None,
        with_open: bool = False,
    ) -> MonthSeed:
        """Everything the month's calculation needs, in a fixed number of queries.

        1. (``with_open``) the unfinished lessons;
        2. the completed lessons of the month (their teacher snapshot, marks and roster snapshot);
        3. their groups; 4. the memberships (and students) of those groups;
        5. the excuses of the lessons that have an "excused" mark;
        6. what pays for every counted lesson - ``PaymentService.lesson_coverage``;
        7. the schedule of those groups (lesson times).
        """
        divisor = self.divisor(org_id)
        seed = MonthSeed(first, last, divisor)
        open_by_teacher = self._open_lessons(org_id, first, last) if with_open else {}
        if teacher_ids is not None:
            open_by_teacher = {t: n for t, n in open_by_teacher.items() if t in {str(i) for i in teacher_ids}}

        query = self.db.query(
            GroupAttendance.id,
            GroupAttendance.group_id,
            GroupAttendance.lesson_date,
            GroupAttendance.lesson_id,
            GroupAttendance.records,
            GroupAttendance.roster_snapshot,
            GroupAttendance.teacher_id,
        ).filter(
            GroupAttendance.org_id == org_id,
            GroupAttendance.status == "completed",
            GroupAttendance.lesson_date >= first.isoformat(),
            GroupAttendance.lesson_date <= last.isoformat(),
        )
        if teacher_ids is not None:
            if not teacher_ids:
                return seed
            query = query.filter(GroupAttendance.teacher_id.in_(teacher_ids))
        rows = query.all()

        by_pair: Dict[Tuple[UUID, UUID], List[_Lesson]] = {}
        for row in rows:
            if row.teacher_id is None:
                seed.lessons_without_teacher += 1
                continue
            try:
                day = date.fromisoformat(row.lesson_date)
            except (TypeError, ValueError):
                logger.warning("Skipping lesson %s with malformed date", row.id)
                continue
            roster = None
            if isinstance(row.roster_snapshot, list):
                roster = {
                    str(item.get("student_id"))
                    for item in row.roster_snapshot
                    if isinstance(item, dict) and item.get("student_id")
                }
            records = row.records if isinstance(row.records, dict) else {}
            by_pair.setdefault((row.teacher_id, row.group_id), []).append(
                _Lesson(row.id, day, row.lesson_id or "", records, roster)
            )
            teacher = seed.teachers.setdefault(row.teacher_id, TeacherSeed(row.teacher_id))
            teacher.lessons_count += 1
        if not by_pair:
            for tid, count in open_by_teacher.items():
                self._open_into(seed, tid, count)
            return seed

        group_ids = {gid for _, gid in by_pair}
        groups = {
            g.id: g
            for g in self.db.query(Group).filter(Group.org_id == org_id, Group.id.in_(group_ids)).all()
        }
        wanted_students: Set[str] = set()
        for lessons in by_pair.values():
            for lesson in lessons:
                wanted_students |= lesson.roster if lesson.roster is not None else set(lesson.records)
        student_uuids = []
        for sid in wanted_students:
            try:
                student_uuids.append(UUID(sid))
            except ValueError:
                continue
        memberships: Dict[Tuple[UUID, str], Tuple[StudentGroup, Student]] = {}
        if student_uuids:
            for membership, student in (
                self.db.query(StudentGroup, Student)
                .join(Student, Student.id == StudentGroup.student_id)
                .filter(
                    Student.org_id == org_id,
                    StudentGroup.group_id.in_(group_ids),
                    StudentGroup.student_id.in_(student_uuids),
                )
                .all()
            ):
                memberships[(membership.group_id, str(membership.student_id))] = (membership, student)

        excused_lessons = {
            lesson.attendance_id
            for lessons in by_pair.values()
            for lesson in lessons
            if any(mark == "excused" for mark in lesson.records.values())
        }
        excuse_status: Dict[Tuple[UUID, str], str] = {}
        if excused_lessons:
            for attendance_id, student_id, status in (
                self.db.query(AttendanceExcuse.attendance_id, AttendanceExcuse.student_id, AttendanceExcuse.status)
                .filter(AttendanceExcuse.org_id == org_id, AttendanceExcuse.attendance_id.in_(excused_lessons))
                .all()
            ):
                excuse_status[(attendance_id, str(student_id))] = status

        coverage = PaymentService(self.db).lesson_coverage(
            org_id, [membership.id for membership, _ in memberships.values()], first, last
        )
        times = self._lesson_times([groups[gid] for gid in group_ids if gid in groups], first, last)

        for (teacher_id, group_id), lessons in by_pair.items():
            group = groups.get(group_id)
            if group is None:
                continue
            lessons.sort(key=lambda lesson: (lesson.day, lesson.lesson_id))
            teacher = seed.teachers[teacher_id]
            lesson_type = group.lesson_type or "group"
            teacher.groups_by_type.setdefault(lesson_type, set()).add(str(group.id))
            slots = sorted(
                {times[(group.id, lesson.day.isoformat(), lesson.lesson_id)]
                 for lesson in lessons if (group.id, lesson.day.isoformat(), lesson.lesson_id) in times}
            )
            lesson_time = ", ".join(slots[:3]) + (", …" if len(slots) > 3 else "") if slots else None

            students: Set[str] = set()
            for lesson in lessons:
                students |= lesson.roster if lesson.roster is not None else set(lesson.records)
            for sid in students:
                found = memberships.get((group_id, sid))
                if found is None:
                    continue  # a student that no longer exists: there is nobody to take a fee from
                membership, student = found
                marks: List[engine.Mark] = []
                pending = 0
                for lesson in lessons:
                    in_roster = sid in (lesson.roster if lesson.roster is not None else lesson.records)
                    mark = lesson.records.get(sid)
                    excuse = excuse_status.get((lesson.attendance_id, sid)) if mark == "excused" else None
                    cover = coverage.get((membership.id, lesson.day, lesson.lesson_id))
                    if cover is None:  # the payments module does not count this lesson for the student
                        state = (
                            engine.EXCUSED_CONFIRMED
                            if in_roster and mark == "excused" and excuse == "confirmed"
                            else engine.NOT_IN_GROUP
                        )
                        marks.append(engine.Mark(lesson.day, lesson.lesson_id, state))
                        continue
                    # a package lesson: the size of the package; an unpaid one of a package student: the
                    # size of his latest package; a calendar lesson (and the rest): the centre's divisor
                    lesson_divisor = cover.package_size if cover.package_size else divisor
                    if excuse == "pending":
                        pending += 1
                    if cover.payment_id is None:
                        state = engine.UNPAID
                    elif excuse == "pending":
                        state = engine.EXCUSED_PENDING
                    else:
                        state = engine.COUNTED
                    marks.append(engine.Mark(lesson.day, lesson.lesson_id, state, lesson_divisor))
                teacher.lines.append(
                    LineSeed(
                        identity={
                            "membership_id": str(membership.id),
                            "student_id": str(student.id),
                            "student_name": person_name(student.last_name, student.first_name) or "—",
                            "display_id": student.display_id,
                            "group_id": str(group.id),
                            "group_name": group.name,
                            "lesson_type": lesson_type,
                            "lesson_time": lesson_time,
                        },
                        marks=marks,
                        source_fee=membership.monthly_fee,
                        pending_excused=pending,
                    )
                )

        for tid, teacher in seed.teachers.items():
            teacher.lines.sort(
                key=lambda line: (
                    line.identity["group_name"].casefold(),
                    line.identity["group_id"],
                    line.identity["student_name"].casefold(),
                    line.identity["membership_id"],
                )
            )
            teacher.fingerprint = fingerprint_of(first, tid, teacher.lines)
        for tid, count in open_by_teacher.items():
            self._open_into(seed, tid, count)
        return seed

    @staticmethod
    def _open_into(seed: MonthSeed, teacher_id: str, count: int) -> None:
        try:
            tid = UUID(teacher_id)
        except ValueError:
            return
        teacher = seed.teachers.setdefault(tid, TeacherSeed(tid))
        if not teacher.fingerprint:
            teacher.fingerprint = fingerprint_of(seed.first, tid, [])
        teacher.open_lessons = count

    def _names(self, ids: Iterable[Optional[UUID]]) -> Dict[UUID, Optional[str]]:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        return {
            u.id: staff_name(u.first_name, u.last_name)
            for u in self.db.query(User).filter(User.id.in_(wanted)).all()
        }

    def _suggested(self, org_id: str, teacher_ids: Iterable[UUID]) -> Dict[Tuple[UUID, str], str]:
        """Percent per (teacher, kind of lessons) from the most recently saved sheet that has the kind."""
        ids = list(set(teacher_ids))
        if not ids:
            return {}
        out: Dict[Tuple[UUID, str], str] = {}
        for teacher_id, inputs in (
            self.db.query(PayrollSheet.teacher_id, PayrollSheet.percent_inputs)
            .filter(PayrollSheet.org_id == org_id, PayrollSheet.teacher_id.in_(ids))
            .order_by(PayrollSheet.updated_at.desc(), PayrollSheet.period_month.desc())
            .all()
        ):
            for lesson_type, value in (inputs or {}).items():
                if (teacher_id, lesson_type) not in out:
                    try:
                        out[(teacher_id, lesson_type)] = engine.format_percent(engine.parse_percent(value))
                    except engine.PercentError:
                        continue
        return out

    def _require_teacher(self, org_id: str, teacher_id: UUID) -> User:
        user = (
            self.db.query(User)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .filter(OrganizationMember.org_id == org_id, User.id == teacher_id)
            .first()
        )
        if user is None:
            raise PayrollNotFound("Преподаватель не найден")
        return user

    # ------------------------------------------------------------- building

    @staticmethod
    def _percent_map(items: Iterable[Any], teacher_id: Optional[UUID] = None) -> Dict[str, Decimal]:
        out: Dict[str, Decimal] = {}
        for item in items or ():
            if teacher_id is not None and item.teacher_id is not None and item.teacher_id != teacher_id:
                raise PayrollInvalid("Процент указан для другого преподавателя")
            out[item.lesson_type] = item.percent
        return out

    @staticmethod
    def _missing_percents(teacher: TeacherSeed, percents: Dict[str, Decimal]) -> List[str]:
        return [t for t in engine.LESSON_TYPES if t in teacher.groups_by_type and t not in percents]

    def _render(
        self,
        teacher: TeacherSeed,
        percents: Dict[str, Decimal],
        changes: Dict[str, _Change],
        divisor: int,
    ) -> List[Dict[str, Any]]:
        lines = []
        for seed in teacher.lines:
            change = changes.get(seed.identity["membership_id"], _Change())
            lines.append(
                engine.compute_line(
                    seed.identity,
                    seed.marks,
                    source_fee=seed.source_fee,
                    fee_override=change.fee,
                    include_unpaid=change.include_unpaid,
                    percent=percents.get(seed.identity["lesson_type"]),
                    pending_excused=seed.pending_excused,
                    default_divisor=divisor,
                )
            )
        return lines

    @staticmethod
    def _percent_inputs(lines: List[Dict[str, Any]], percents: Dict[str, Decimal]) -> Dict[str, str]:
        """The percents the sheet used: one per kind of lessons that has a line."""
        present = {line["lesson_type"] for line in lines}
        return {t: engine.format_percent(percents[t]) for t in engine.LESSON_TYPES if t in present and t in percents}

    def _out(
        self,
        *,
        sheet: Optional[PayrollSheet],
        first: date,
        last: date,
        teacher_id: UUID,
        names: Dict[UUID, Optional[str]],
        lines: List[Dict[str, Any]],
        percent_inputs: Dict[str, str],
        divisor_default: int,
        revision: Optional[int],
        fingerprint: str,
        stale: Optional[bool],
        note: Optional[str] = None,
        open_lessons: Optional[int] = None,
    ) -> SheetOut:
        counters = engine.sheet_counters(lines)
        return SheetOut(
            id=str(sheet.id) if sheet is not None else None,
            saved=sheet is not None,
            month=month_label(first),
            period_start=first.isoformat(),
            period_end=last.isoformat(),
            month_open=ps.business_today() <= last,
            teacher_id=str(teacher_id),
            teacher_name=names.get(teacher_id),
            revision=revision,
            percents=percent_inputs,
            divisor_default=divisor_default,
            lines=[LineOut(**line) for line in lines],
            groups=[GroupBlockOut(**block) for block in engine.group_blocks(lines)],
            total_amount=counters["total_amount"],
            counted_total=counters["counted"],
            unpaid_total=counters["unpaid"],
            pending_excused_total=counters["pending_excused"],
            warnings=SheetWarnings(
                open_lessons=open_lessons,
                pending_excused=counters["pending_excused"],
                no_fee=counters["no_fee"],
                unpaid=counters["unpaid"],
            ),
            source_fingerprint=fingerprint,
            stale=stale,
            note=note,
            created_by_name=names.get(sheet.created_by) if sheet is not None else None,
            created_at=_iso(sheet.created_at) if sheet is not None else None,
            updated_by_name=names.get(sheet.updated_by) if sheet is not None else None,
            updated_at=_iso(sheet.updated_at) if sheet is not None else None,
        )

    def _notes(self, sheets: Iterable[PayrollSheet]) -> Dict[UUID, Optional[str]]:
        """The note of the current revision of each sheet, in ONE query."""
        wanted = [(s.id, s.revision) for s in sheets]
        if not wanted:
            return {}
        return {
            sheet_id: note
            for sheet_id, note in self.db.query(PayrollSheetRevision.sheet_id, PayrollSheetRevision.change_note).filter(
                tuple_(PayrollSheetRevision.sheet_id, PayrollSheetRevision.revision).in_(wanted)
            )
        }

    def _saved_out(
        self,
        sheet: PayrollSheet,
        *,
        stale: Optional[bool],
        names: Optional[Dict[UUID, Optional[str]]] = None,
        notes: Optional[Dict[UUID, Optional[str]]] = None,
    ) -> SheetOut:
        first = sheet.period_month
        last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
        names = names or self._names([sheet.teacher_id, sheet.created_by, sheet.updated_by])
        note = (notes if notes is not None else self._notes([sheet])).get(sheet.id)
        return self._out(
            sheet=sheet,
            first=first,
            last=last,
            teacher_id=sheet.teacher_id,
            names=names,
            lines=list(sheet.lines or []),
            percent_inputs=dict(sheet.percent_inputs or {}),
            divisor_default=sheet.divisor_default,
            revision=sheet.revision,
            fingerprint=sheet.source_fingerprint,
            stale=stale,
            note=note,
        )

    # ---------------------------------------------------------------- month

    def month_overview(self, org_id: str, month: str) -> MonthOverview:
        first, last = parse_month(month)
        seed = self._collect(org_id, first, last, with_open=True)
        sheets = {
            s.teacher_id: s
            for s in self.db.query(
                PayrollSheet.id,
                PayrollSheet.teacher_id,
                PayrollSheet.revision,
                PayrollSheet.total_amount,
                PayrollSheet.source_fingerprint,
                PayrollSheet.percent_inputs,
                PayrollSheet.updated_at,
            ).filter(PayrollSheet.org_id == org_id, PayrollSheet.period_month == first)
        }
        teacher_ids = set(seed.teachers) | set(sheets)
        names = self._names(teacher_ids)
        suggestions = self._suggested(org_id, teacher_ids)
        out: List[MonthTeacher] = []
        for tid in teacher_ids:
            teacher = seed.teachers.get(tid)
            sheet = sheets.get(tid)
            # a teacher with unfinished lessons only is listed too: the owner must see what blocks the month
            if (teacher is None or not (teacher.lessons_count or teacher.open_lessons)) and sheet is None:
                continue
            counters = engine.sheet_counters(self._render(teacher, {}, {}, seed.divisor)) if teacher else None
            types = (
                {t: len(ids) for t, ids in teacher.groups_by_type.items()}
                if teacher and teacher.groups_by_type
                else {t: 0 for t in ((sheet.percent_inputs or {}) if sheet else {})}
            )
            fresh = teacher.fingerprint if teacher and teacher.fingerprint else fingerprint_of(first, tid, [])
            out.append(
                MonthTeacher(
                    teacher_id=str(tid),
                    teacher_name=names.get(tid),
                    lessons_count=teacher.lessons_count if teacher else 0,
                    lesson_types=[
                        LessonTypeSuggestion(
                            lesson_type=t,
                            groups=types[t],
                            suggested_percent=suggestions.get((tid, t)),
                        )
                        for t in engine.LESSON_TYPES
                        if t in types
                    ],
                    sheet=(
                        MonthSheetRef(
                            id=str(sheet.id),
                            revision=sheet.revision,
                            total_amount=sheet.total_amount,
                            stale=sheet.source_fingerprint != fresh,
                            updated_at=_iso(sheet.updated_at),
                        )
                        if sheet
                        else None
                    ),
                    warnings=MonthWarnings(
                        open_lessons=teacher.open_lessons if teacher else 0,
                        pending_excused=counters["pending_excused"] if counters else 0,
                        no_fee=counters["no_fee"] if counters else 0,
                        unpaid=counters["unpaid"] if counters else 0,
                    ),
                )
            )
        out.sort(key=lambda t: ((t.teacher_name or "").casefold(), t.teacher_id))
        return MonthOverview(
            month=month_label(first),
            period_start=first.isoformat(),
            period_end=last.isoformat(),
            month_open=ps.business_today() <= last,
            divisor=seed.divisor,
            teachers=out,
            lessons_without_teacher=seed.lessons_without_teacher,
        )

    # ------------------------------------------------------------ calculate

    def calculate(self, org_id: str, month: str, request: CalculateRequest) -> CalculateResponse:
        """Preview of the sheets of a month.  Writes nothing."""
        first, last = parse_month(month)
        wanted = set(request.teacher_ids) if request.teacher_ids else None
        if wanted:
            known = {
                row[0]
                for row in self.db.query(OrganizationMember.user_id).filter(
                    OrganizationMember.org_id == org_id, OrganizationMember.user_id.in_(wanted)
                )
            }
            if known != wanted:
                raise PayrollNotFound("Преподаватель не найден")
        seed = self._collect(org_id, first, last, teacher_ids=wanted, with_open=True)
        targets = [t for t in seed.teachers.values() if t.lines or wanted]
        names = self._names(
            [t.teacher_id for t in targets] + list(wanted or ())
        )
        by_teacher: Dict[UUID, Dict[str, Decimal]] = {}
        for item in request.percents:
            by_teacher.setdefault(item.teacher_id, {})[item.lesson_type] = item.percent
        missing = []
        for teacher in targets:
            for lesson_type in self._missing_percents(teacher, by_teacher.get(teacher.teacher_id, {})):
                missing.append(
                    {
                        "teacher_id": str(teacher.teacher_id),
                        "teacher_name": names.get(teacher.teacher_id),
                        "lesson_type": lesson_type,
                        "lesson_type_label": engine.LESSON_TYPE_LABELS[lesson_type],
                    }
                )
        if missing:
            raise PercentRequired(
                "Укажите процент преподавателя для каждого типа занятий — расчёт без него не запускается",
                missing=missing,
            )
        sheets = []
        order = sorted(
            (t.teacher_id for t in targets) if not wanted else (t for t in wanted),
            key=lambda tid: ((names.get(tid) or "").casefold(), str(tid)),
        )
        for tid in order:
            teacher = seed.teachers.get(tid) or TeacherSeed(tid, fingerprint=fingerprint_of(first, tid, []))
            percents = by_teacher.get(tid, {})
            lines = self._render(teacher, percents, {}, seed.divisor)
            sheets.append(
                self._out(
                    sheet=None,
                    first=first,
                    last=last,
                    teacher_id=tid,
                    names=names,
                    lines=lines,
                    percent_inputs=self._percent_inputs(lines, percents),
                    divisor_default=seed.divisor,
                    revision=None,
                    fingerprint=teacher.fingerprint,
                    stale=None,
                    open_lessons=teacher.open_lessons,
                )
            )
        return CalculateResponse(month=month_label(first), month_open=ps.business_today() <= last, sheets=sheets)

    # ----------------------------------------------------------------- save

    def _lock_sheet(self, org_id: str, sheet_id: UUID, *, lock: bool) -> PayrollSheet:
        query = self.db.query(PayrollSheet).filter(PayrollSheet.id == sheet_id, PayrollSheet.org_id == org_id)
        # populate_existing: after the wait for the lock the row is read again, never taken from the session
        sheet = (query.with_for_update().populate_existing() if lock else query).first()
        if sheet is None:
            raise PayrollNotFound("Ведомость не найдена")
        return sheet

    @staticmethod
    def _conflict(current: int) -> RevisionConflict:
        return RevisionConflict(
            "Ведомость уже изменили. Обновите страницу и повторите правку.", current_revision=current
        )

    @staticmethod
    def _changes_from_body(overrides, known: Optional[Set[str]]) -> Dict[str, Any]:
        """``{membership_id: LineOverride}``; with ``known`` a line that is not on the sheet is a 422."""
        for item in overrides or ():
            if known is not None and str(item.membership_id) not in known:
                raise PayrollInvalid("Такой строки нет в ведомости", code="LINE_NOT_FOUND")
        return {str(item.membership_id): item for item in overrides or ()}

    def save_sheet(self, org_id: str, actor_id: str, body: SheetCreate) -> SheetOut:
        """Save the calculation of a teacher's month; if a sheet exists this is its next revision.

        ``expected_fingerprint`` (the ``source_fingerprint`` of the preview the owner looked at): if the
        data moved since, nothing is saved (409 ``SHEET_DATA_CHANGED``) - the owner saves what he saw.
        On an existing sheet the decisions of its last revision (fee overrides, ticks) stay for the lines
        the request does not mention; what the request says wins, ``monthly_fee: null`` resets a fee.
        """
        first, last = parse_month(body.month)
        self._require_teacher(org_id, body.teacher_id)
        seed = self._collect(org_id, first, last, teacher_ids={body.teacher_id})
        teacher = seed.teachers.get(body.teacher_id) or TeacherSeed(
            body.teacher_id, fingerprint=fingerprint_of(first, body.teacher_id, [])
        )
        self._check_expected(body.expected_fingerprint, teacher.fingerprint)
        percents = self._percent_map(body.percents, body.teacher_id)
        missing = self._missing_percents(teacher, percents)
        if missing:
            raise PercentRequired(
                "Укажите процент преподавателя для каждого типа занятий — расчёт без него не запускается",
                missing=[
                    {"teacher_id": str(body.teacher_id), "lesson_type": t, "lesson_type_label": engine.LESSON_TYPE_LABELS[t]}
                    for t in missing
                ],
            )
        given = self._changes_from_body(body.line_overrides, {s.identity["membership_id"] for s in teacher.lines})
        now = _utcnow()
        actor = UUID(str(actor_id))

        try:
            sheet = (
                self.db.query(PayrollSheet)
                .filter(
                    PayrollSheet.org_id == org_id,
                    PayrollSheet.teacher_id == body.teacher_id,
                    PayrollSheet.period_month == first,
                )
                .with_for_update()
                .populate_existing()
                .first()
            )
            if sheet is not None and body.revision != sheet.revision:
                raise self._conflict(sheet.revision)
            if sheet is None and body.revision is not None:
                raise self._conflict(0)
            changes = self._carried_changes(sheet)
            self._apply_overrides(changes, given)
            lines = self._render(teacher, percents, changes, seed.divisor)
            inputs = self._percent_inputs(lines, percents)
            total = engine.sheet_total(lines)
            if sheet is not None:
                sheet.revision += 1
                sheet.percent_inputs = inputs
                sheet.divisor_default = seed.divisor
                sheet.lines = lines
                sheet.total_amount = total
                sheet.source_fingerprint = teacher.fingerprint
                sheet.updated_by = actor
                sheet.updated_at = now
            else:
                sheet = PayrollSheet(
                    org_id=UUID(str(org_id)),
                    teacher_id=body.teacher_id,
                    period_month=first,
                    percent_inputs=inputs,
                    divisor_default=seed.divisor,
                    lines=lines,
                    total_amount=total,
                    source_fingerprint=teacher.fingerprint,
                    revision=1,
                    created_by=actor,
                    created_at=now,
                    updated_by=actor,
                    updated_at=now,
                )
                self.db.add(sheet)
                self.db.flush()
            self._add_revision(sheet, actor, now, body.note)
            self.db.commit()
        except IntegrityError as exc:  # another request saved the first sheet of this month meanwhile
            self.db.rollback()
            logger.info("payroll sheet save lost a race org=%s teacher=%s", org_id, body.teacher_id)
            current = (
                self.db.query(PayrollSheet.revision)
                .filter(
                    PayrollSheet.org_id == org_id,
                    PayrollSheet.teacher_id == body.teacher_id,
                    PayrollSheet.period_month == first,
                )
                .scalar()
            )
            raise self._conflict(current or 1) from exc
        except PayrollError:
            self.db.rollback()
            raise
        logger.info(
            "payroll sheet saved org=%s sheet=%s revision=%s actor=%s", org_id, sheet.id, sheet.revision, actor_id
        )
        return self._saved_out(sheet, stale=False)

    @staticmethod
    def _check_expected(expected: Optional[str], fresh: str) -> None:
        if expected and expected != fresh:
            raise DataChanged(
                "Данные изменились после расчёта — пересчитайте и проверьте суммы", source_fingerprint=fresh
            )

    @staticmethod
    def _carried_changes(sheet: Optional[PayrollSheet]) -> Dict[str, _Change]:
        """The owner's decisions on the lines of a saved sheet: fee overrides and ticks, by membership."""
        return {
            line["membership_id"]: _Change(
                fee=line.get("monthly_fee") if line.get("fee_overridden") else None,
                include_unpaid=bool(line.get("include_unpaid")),
            )
            for line in (sheet.lines or [] if sheet is not None else [])
        }

    @staticmethod
    def _apply_overrides(changes: Dict[str, _Change], given: Dict[str, Any]) -> None:
        """What a request says about a line wins over what the sheet had; a field it leaves out stays."""
        for mid, item in given.items():
            change = changes.setdefault(mid, _Change())
            if "monthly_fee" in item.model_fields_set:
                change.fee = item.monthly_fee
            if item.include_unpaid is not None:
                change.include_unpaid = item.include_unpaid

    def _add_revision(self, sheet: PayrollSheet, actor: UUID, now: datetime, note: Optional[str]) -> None:
        self.db.add(
            PayrollSheetRevision(
                sheet_id=sheet.id,
                revision=sheet.revision,
                percent_inputs=sheet.percent_inputs,
                lines=sheet.lines,
                total_amount=sheet.total_amount,
                changed_by=actor,
                changed_at=now,
                change_note=(note or "").strip() or None,
            )
        )

    # ---------------------------------------------------------------- patch

    def patch_sheet(self, org_id: str, actor_id: str, sheet_id: UUID, body: SheetPatch) -> SheetOut:
        """A change of a saved sheet = its next revision.

        Without ``recalculate`` the new revision is computed from the SAVED lines (fee, percent,
        "include unpaid"): attendance and payments are not read again, the data fingerprint stays.
        With it the month is read again; the owner's fee overrides and ticks are kept.
        """
        if not (body.percents or body.line_overrides or body.recalculate or (body.note or "").strip()):
            raise PayrollInvalid("Нечего менять", code="NOTHING_TO_CHANGE")
        sheet = self._lock_sheet(org_id, sheet_id, lock=True)
        try:
            if body.revision != sheet.revision:
                raise self._conflict(sheet.revision)
            first = sheet.period_month
            saved_lines: List[Dict[str, Any]] = list(sheet.lines or [])
            percents = {t: engine.parse_percent(v) for t, v in (sheet.percent_inputs or {}).items()}
            for item in body.percents or ():
                if item.teacher_id is not None and item.teacher_id != sheet.teacher_id:
                    raise PayrollInvalid("Процент указан для другого преподавателя")
                percents[item.lesson_type] = item.percent

            if body.recalculate:
                given = self._changes_from_body(body.line_overrides, None)
                last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
                seed = self._collect(org_id, first, last, teacher_ids={sheet.teacher_id})
                teacher = seed.teachers.get(sheet.teacher_id) or TeacherSeed(
                    sheet.teacher_id, fingerprint=fingerprint_of(first, sheet.teacher_id, [])
                )
                missing = self._missing_percents(teacher, percents)
                if missing:
                    raise PercentRequired(
                        "Укажите процент преподавателя для каждого типа занятий — расчёт без него не запускается",
                        missing=[
                            {"teacher_id": str(sheet.teacher_id), "lesson_type": t,
                             "lesson_type_label": engine.LESSON_TYPE_LABELS[t]}
                            for t in missing
                        ],
                    )
                self._check_expected(body.expected_fingerprint, teacher.fingerprint)
                changes = self._carried_changes(sheet)
                fresh_ids = {s.identity["membership_id"] for s in teacher.lines}
                for mid in given:
                    if mid not in fresh_ids:
                        raise PayrollInvalid("Такой строки нет в ведомости", code="LINE_NOT_FOUND")
                self._apply_overrides(changes, given)
                lines = self._render(teacher, percents, changes, seed.divisor)
                fingerprint, divisor_default = teacher.fingerprint, seed.divisor
            else:
                given = self._changes_from_body(body.line_overrides, {line["membership_id"] for line in saved_lines})
                types = {line["lesson_type"] for line in saved_lines}
                for item in body.percents or ():
                    if item.lesson_type not in types:
                        raise PayrollInvalid(
                            "В этой ведомости нет занятий такого типа", code="LESSON_TYPE_NOT_IN_SHEET"
                        )
                lines = []
                for line in saved_lines:
                    item = given.get(line["membership_id"])
                    fee_set = item is not None and "monthly_fee" in item.model_fields_set
                    lines.append(
                        engine.recompute_line(
                            line,
                            percent=percents.get(line["lesson_type"]),
                            fee_override=item.monthly_fee if fee_set else None,
                            reset_fee=bool(fee_set and item.monthly_fee is None),
                            include_unpaid=item.include_unpaid if item is not None else None,
                            default_divisor=sheet.divisor_default,
                        )
                    )
                fingerprint, divisor_default = sheet.source_fingerprint, sheet.divisor_default

            now, actor = _utcnow(), UUID(str(actor_id))
            sheet.revision += 1
            sheet.percent_inputs = self._percent_inputs(lines, percents)
            sheet.divisor_default = divisor_default
            sheet.lines = lines
            sheet.total_amount = engine.sheet_total(lines)
            sheet.source_fingerprint = fingerprint
            sheet.updated_by = actor
            sheet.updated_at = now
            self._add_revision(sheet, actor, now, body.note)
            self.db.commit()
        except PayrollError:
            self.db.rollback()
            raise
        except IntegrityError as exc:
            self.db.rollback()
            raise self._conflict(sheet.revision) from exc
        logger.info(
            "payroll sheet changed org=%s sheet=%s revision=%s actor=%s recalculate=%s",
            org_id, sheet_id, sheet.revision, actor_id, body.recalculate,
        )
        return self._saved_out(sheet, stale=False if body.recalculate else self._is_stale(org_id, sheet))

    # ----------------------------------------------------------------- read

    def _is_stale(self, org_id: str, sheet: PayrollSheet) -> bool:
        first = sheet.period_month
        last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
        seed = self._collect(org_id, first, last, teacher_ids={sheet.teacher_id})
        teacher = seed.teachers.get(sheet.teacher_id)
        fresh = teacher.fingerprint if teacher else fingerprint_of(first, sheet.teacher_id, [])
        return fresh != sheet.source_fingerprint

    def get_sheet(self, org_id: str, sheet_id: UUID) -> SheetOut:
        sheet = self._lock_sheet(org_id, sheet_id, lock=False)
        return self._saved_out(sheet, stale=self._is_stale(org_id, sheet))

    def list_sheets(self, org_id: str, month: Optional[str]) -> List[SheetBrief]:
        query = self.db.query(
            PayrollSheet.id,
            PayrollSheet.teacher_id,
            PayrollSheet.period_month,
            PayrollSheet.revision,
            PayrollSheet.total_amount,
            PayrollSheet.source_fingerprint,
            PayrollSheet.updated_by,
            PayrollSheet.updated_at,
        ).filter(PayrollSheet.org_id == org_id)
        fresh: Dict[UUID, str] = {}
        first = None
        if month:
            first, last = parse_month(month)
            query = query.filter(PayrollSheet.period_month == first)
        rows = query.order_by(PayrollSheet.period_month.desc(), PayrollSheet.updated_at.desc()).limit(
            MAX_LISTED_SHEETS
        ).all()
        if first is not None and rows:
            seed = self._collect(org_id, first, last, teacher_ids={r.teacher_id for r in rows})
            fresh = {tid: t.fingerprint for tid, t in seed.teachers.items()}
        names = self._names([r.teacher_id for r in rows] + [r.updated_by for r in rows])
        return [
            SheetBrief(
                id=str(r.id),
                teacher_id=str(r.teacher_id),
                teacher_name=names.get(r.teacher_id),
                month=month_label(r.period_month),
                revision=r.revision,
                total_amount=r.total_amount,
                stale=(
                    fresh.get(r.teacher_id, fingerprint_of(first, r.teacher_id, [])) != r.source_fingerprint
                    if first is not None
                    else None
                ),
                updated_by_name=names.get(r.updated_by),
                updated_at=_iso(r.updated_at),
            )
            for r in rows
        ]

    def revisions(self, org_id: str, sheet_id: UUID) -> List[RevisionOut]:
        sheet = self._lock_sheet(org_id, sheet_id, lock=False)
        rows = (
            # the lines themselves are not loaded: only how many there are
            self.db.query(
                PayrollSheetRevision.revision,
                PayrollSheetRevision.total_amount,
                PayrollSheetRevision.percent_inputs,
                PayrollSheetRevision.changed_by,
                PayrollSheetRevision.changed_at,
                PayrollSheetRevision.change_note,
                func.jsonb_array_length(PayrollSheetRevision.lines).label("line_count"),
            )
            .filter(PayrollSheetRevision.sheet_id == sheet.id)
            .order_by(PayrollSheetRevision.revision.desc())
            .all()
        )
        names = self._names(r.changed_by for r in rows)
        return [
            RevisionOut(
                revision=r.revision,
                total_amount=r.total_amount,
                percents=dict(r.percent_inputs or {}),
                line_count=r.line_count or 0,
                changed_by=str(r.changed_by),
                changed_by_name=names.get(r.changed_by),
                changed_at=_iso(r.changed_at),
                change_note=r.change_note,
            )
            for r in rows
        ]

    def diff(self, org_id: str, sheet_id: UUID) -> SheetDiff:
        """What a recalculation would change: the saved lines against the current data, with the SAVED
        percents, fee overrides and ticks."""
        sheet = self._lock_sheet(org_id, sheet_id, lock=False)
        first = sheet.period_month
        last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
        seed = self._collect(org_id, first, last, teacher_ids={sheet.teacher_id})
        teacher = seed.teachers.get(sheet.teacher_id) or TeacherSeed(
            sheet.teacher_id, fingerprint=fingerprint_of(first, sheet.teacher_id, [])
        )
        saved = list(sheet.lines or [])
        percents = {t: engine.parse_percent(v) for t, v in (sheet.percent_inputs or {}).items()}
        changes = {
            line["membership_id"]: _Change(
                fee=line.get("monthly_fee") if line.get("fee_overridden") else None,
                include_unpaid=bool(line.get("include_unpaid")),
            )
            for line in saved
        }
        fresh = self._render(teacher, percents, changes, seed.divisor)
        rows = engine.diff_lines(saved, fresh)
        saved_total, fresh_total = engine.sheet_total(saved), engine.sheet_total(fresh)
        return SheetDiff(
            sheet_id=str(sheet.id),
            revision=sheet.revision,
            stale=teacher.fingerprint != sheet.source_fingerprint,
            saved_total=saved_total,
            fresh_total=fresh_total,
            delta_amount=fresh_total - saved_total,
            lines=[DiffLine(**row) for row in rows],
        )

    # --------------------------------------------------------------- export

    def sheets_for_export(self, org_id: str, sheet_id: Optional[UUID], month: Optional[str]) -> Tuple[List[SheetOut], str]:
        """The saved sheets to put into a workbook and its month label (one sheet, or every sheet of a month)."""
        if sheet_id is not None:
            sheet = self._lock_sheet(org_id, sheet_id, lock=False)
            return [self._saved_out(sheet, stale=None)], month_label(sheet.period_month)
        first, _ = parse_month(month or "")
        rows = (
            self.db.query(PayrollSheet)
            .filter(PayrollSheet.org_id == org_id, PayrollSheet.period_month == first)
            .all()
        )
        if not rows:
            raise PayrollNotFound("За этот месяц нет сохранённых ведомостей")
        names = self._names(
            [r.teacher_id for r in rows] + [r.created_by for r in rows] + [r.updated_by for r in rows]
        )
        notes = self._notes(rows)
        outs = [self._saved_out(r, stale=None, names=names, notes=notes) for r in rows]
        outs.sort(key=lambda o: ((o.teacher_name or "").casefold(), o.teacher_id))
        return outs, month_label(first)
