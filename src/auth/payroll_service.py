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
* The price of a lesson is the effective price of the student ON THE BUSINESS DAY OF THAT LESSON (ROADMAP #105,
  ``pricing.py``): the history of the groups and of the students' exceptions is loaded once per calculation (two
  queries) and resolved in memory for every lesson.  The data fingerprint keeps its shape for a line whose price
  did not change inside the month (so sheets saved before #105 stay "not stale"); a line whose price changed adds
  the steps of the price.  A price changed after a sheet was saved - also backdated - changes the fingerprint of
  the months it touches, so the saved sheet turns "данные изменились"; the sheet itself never changes by itself.
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
from datetime import date, datetime, timedelta, timezone
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
    PayrollDivisor,
    PayrollSheet,
    PayrollSheetRevision,
    Student,
    StudentGroup,
    User,
)

from . import lesson_types
from . import payment_status as ps
from . import payroll_engine as engine
from . import payroll_rates
from . import pricing
from .payments_files import person_name, staff_name
from .payments_service import PaymentService
from .payroll_schemas import (
    DivisorRow,
    PayoutState,
    CalculateRequest,
    CalculateResponse,
    DiffLine,
    RateChangeOut,
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
RATE_REQUIRED = "Не задана ставка преподавателя: укажите её в «Сотрудниках»"
PRICE_NOT_EDITABLE = "Цена меняется в «Оплаты → Цены групп» или в карточке ученика"


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


class DivisorRange(PayrollInvalid):
    code = "DIVISOR_RANGE"


class FingerprintRequired(PayrollInvalid):
    code = "FINGERPRINT_REQUIRED"


class PercentRequired(PayrollInvalid):
    code = "PERCENT_REQUIRED"


class PriceNotEditable(PayrollInvalid):
    code = "PRICE_NOT_EDITABLE"


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

    Per line: the membership, the group and its kind of lessons, the price of the line on its reference day
    (``source_fee``), the pending-excuse count and every lesson column - date, id, state (counted / excused /
    unpaid / not in group) and the divisor; when the price CHANGED inside the line's lessons, also the steps of
    the price (the first day of every new price).  It is what the payments module decided about each lesson, so
    a changed mark, excuse decision, payment, price (also a backdated one), teacher snapshot or divisor changes
    the hash; names, the order of the rows and the owner's overrides do not.  A price that is the same for all the
    lessons of a line adds nothing to the item, so the hash of a sheet saved before #105 is still the hash of the
    same data.
    """
    items = []
    for line in lines:
        ident = line.identity
        item = [
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
        steps = engine.price_steps(line.marks)
        if len(steps) > 1:
            item.append(steps)
        items.append(item)
    items.sort(key=lambda item: (item[0], item[1], item[2]))
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
    """A line before percent / overrides: identity, the lesson columns (each with the price of its day) and the
    price of the line on its reference day."""

    identity: Dict[str, Any]
    marks: List[engine.Mark]
    source_fee: Optional[int]
    pending_excused: int
    price_source: Optional[str] = None


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

    def _divisor_rows(self, org_id: str) -> List[Tuple[date, int, datetime]]:
        """``(valid_from, divisor, created_at)`` of the history, oldest first; on one ``valid_from`` the later row wins."""
        rows = (
            self.db.query(PayrollDivisor.valid_from, PayrollDivisor.divisor, PayrollDivisor.created_at)
            .filter(PayrollDivisor.org_id == org_id)
            .order_by(PayrollDivisor.valid_from, PayrollDivisor.created_at, PayrollDivisor.id)
            .all()
        )
        return [(r[0], int(r[1]), r[2]) for r in rows]

    @staticmethod
    def _divisor_in(rows: List[Tuple[date, int, datetime]], first: date, fallback: int = engine.DEFAULT_DIVISOR) -> int:
        """The divisor of the month that begins on ``first``: the last row with ``valid_from <= first`` (a later one wins a tie)."""
        value = None
        for valid_from, divisor, _ in rows:
            if valid_from <= first:
                value = divisor
        return value if value is not None else fallback

    def get_settings(self, org_id: str) -> PayrollSettings:
        block = self._block(org_id)
        rows = self._divisor_rows(org_id)
        current_month = ps.business_today().replace(day=1)
        try:
            history: Dict[str, int] = {}
            for valid_from, divisor, _ in rows:
                history[month_label(valid_from)] = divisor           # the later row of the same month wins
            in_force = self._divisor_in(rows, current_month, block.get("divisor", engine.DEFAULT_DIVISOR))
            since = max((d for d, _, _ in rows if d <= current_month), default=None)
            return PayrollSettings(
                divisor=in_force,
                valid_from=month_label(since) if since else None,
                updated_by=block.get("updated_by"),
                updated_at=block.get("updated_at"),
                history=[DivisorRow(valid_from=m, divisor=d) for m, d in sorted(history.items())],
            )
        except ValueError:  # a hand-edited, out-of-range value must not break the page
            logger.warning("Invalid payroll settings stored for org %s; using defaults", org_id)
            return PayrollSettings()

    def divisor(self, org_id: str) -> int:
        return self.get_settings(org_id).divisor

    def divisor_at(self, org_id: str, first: date) -> int:
        """The divisor of the month that begins on ``first`` (LOGIC-A, F7)."""
        return self._divisor_in(self._divisor_rows(org_id), first, self._block(org_id).get("divisor", engine.DEFAULT_DIVISOR))

    def update_settings(self, org_id: str, actor_id: str, divisor: int, valid_from: Optional[str] = None) -> PayrollSettings:
        """The divisor acts from the 1st day of ``valid_from`` (a month "YYYY-MM", default: this month); earlier months keep theirs."""
        if not isinstance(divisor, int) or isinstance(divisor, bool) or not engine.MIN_DIVISOR <= divisor <= engine.MAX_DIVISOR:
            raise DivisorRange(f"Делитель: от {engine.MIN_DIVISOR} до {engine.MAX_DIVISOR}")
        today = ps.business_today()
        if valid_from:
            match = MONTH_RE.match(valid_from)
            if not match or not 1 <= int(match.group(2)) <= 12:
                raise MonthInvalid("Месяц указывается в формате ГГГГ-ММ, например 2026-09")
            month = date(int(match.group(1)), int(match.group(2)), 1)
            if month < date(2020, 1, 1):
                raise MonthInvalid("Делитель можно задать не раньше января 2020")
        else:
            month = today.replace(day=1)
        org = self.db.query(Organization).filter(Organization.id == org_id).with_for_update().first()
        if not org:
            raise PayrollNotFound("Организация не найдена")
        if self._divisor_at_locked(org_id, month, org) == divisor:      # the same divisor from the same month: nothing to write
            self.db.rollback()
            return self.get_settings(org_id)
        if not self._divisor_rows(org_id):   # an organisation without a history (made after the migration): the divisor so far is "always"
            before = (org.settings or {}).get("payroll", {}).get("divisor", engine.DEFAULT_DIVISOR)
            self.db.add(PayrollDivisor(org_id=UUID(str(org_id)), valid_from=date(2020, 1, 1),
                                       divisor=before if isinstance(before, int) and 1 <= before <= 31 else engine.DEFAULT_DIVISOR,
                                       note="the divisor before the history", created_at=_utcnow() - timedelta(seconds=1)))
        self.db.add(PayrollDivisor(org_id=UUID(str(org_id)), valid_from=month, divisor=divisor, created_by=UUID(str(actor_id)),
                                   created_at=_utcnow()))
        self.db.flush()
        block = dict(((org.settings or {}).get("payroll")) or {})
        block.update({"divisor": self._divisor_in(self._divisor_rows(org_id), today.replace(day=1)),   # the mirror of the current month
                      "updated_by": str(actor_id), "updated_at": _utcnow().isoformat()})
        org.settings = {**(org.settings or {}), "payroll": block}
        flag_modified(org, "settings")
        self.db.commit()
        logger.info("payroll divisor set org=%s actor=%s from=%s", org_id, actor_id, month)
        return self.get_settings(org_id)

    def _divisor_at_locked(self, org_id: str, first: date, org: Organization) -> int:
        block = (org.settings or {}).get("payroll") or {}
        return self._divisor_in(self._divisor_rows(org_id), first, block.get("divisor", engine.DEFAULT_DIVISOR))

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
        group_books: Optional[Dict[UUID, pricing.GroupBook]] = None,
        type_rows: Optional[Dict[UUID, List[Tuple[date, str]]]] = None,
        override_books: Optional[Dict[UUID, "pricing.OverrideBook"]] = None,
    ) -> MonthSeed:
        """Everything the month's calculation needs, in a fixed number of queries.

        1. (``with_open``) the unfinished lessons;
        2. the completed lessons of the month (their teacher snapshot, marks and roster snapshot);
        3. their groups; 4. the memberships (and students) of those groups;
        5. the excuses of the lessons that have an "excused" mark;
        6. what pays for every counted lesson - ``PaymentService.lesson_coverage``;
        7. the schedule of those groups (lesson times);
        8. the price history of those groups and memberships (two queries) - every lesson is priced in memory.

        ``group_books`` replaces the price history of some groups ("what if", see ``sheets_changed_by_group_price``); ``type_rows``
        the history of the lesson types of some groups (``sheets_changed_by_lesson_type``).

        LOGIC-A: the divisor is the one of the MONTH (F7); the price of a lesson is the price of the period / package that carries it
        (F1: what the student is charged - ``LessonCover.price``); the kind of lessons is the one of the DAY of the lesson (F14), so the
        line of a student splits in two when the group changed its type inside the month.
        """
        divisor = self.divisor_at(org_id, first)
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
        books = pricing.load_books(self.db, group_ids, [membership.id for membership, _ in memberships.values()])
        if group_books:
            books = books.with_group_books(group_books)
        if override_books:
            books = books.with_override_books(override_books)
        times = self._lesson_times([groups[gid] for gid in group_ids if gid in groups], first, last)
        types = lesson_types.load_book(self.db, groups.values())
        for gid, rows_ in (type_rows or {}).items():
            types = types.with_group(gid, rows_)

        for (teacher_id, group_id), lessons in by_pair.items():
            group = groups.get(group_id)
            if group is None:
                continue
            lessons.sort(key=lambda lesson: (lesson.day, lesson.lesson_id))
            teacher = seed.teachers[teacher_id]
            month_types = {types.type_at(group.id, lesson.day) for lesson in lessons}
            split = len(month_types) > 1
            for month_type in month_types:
                teacher.groups_by_type.setdefault(month_type, set()).add(str(group.id))
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
                mark_type: List[str] = []
                pending_by_type: Dict[str, int] = {}
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
                        mark_type.append(types.type_at(group.id, lesson.day))
                        continue
                    # a package lesson: the size of the package; an unpaid one of a package student: the
                    # size of his latest package; a calendar lesson (and the rest): the centre's divisor
                    lesson_divisor = cover.package_size if cover.package_size else divisor
                    if excuse == "pending":
                        pending += 1
                        pending_by_type[types.type_at(group.id, lesson.day)] = pending_by_type.get(types.type_at(group.id, lesson.day), 0) + 1
                    if not cover.paid:
                        state = engine.UNPAID
                    elif excuse == "pending":
                        state = engine.EXCUSED_PENDING
                    else:
                        state = engine.COUNTED
                    # F1: the base is the price of the PERIOD (package) that carries the lesson - what the student is charged;
                    # a lesson before the first charged day (accounting start / "paid until" / transfer day) has the price of its day
                    if cover.price is not None:
                        price_amount, price_source = cover.price.amount, cover.price.source
                    elif cover.cover_source == "before_start":
                        day_price = books.price(membership.id, membership.group_id, lesson.day)
                        price_amount, price_source = day_price.amount, "day"
                    else:
                        price_amount, price_source = None, "none"
                    cover_label = "unpaid" if not cover.paid else (
                        cover.cover_source if cover.cover_source in ("money", "writeoff", "before_start") else "money")
                    marks.append(
                        engine.Mark(
                            lesson.day, lesson.lesson_id, state, lesson_divisor, price_amount, True, price_source, cover_label
                        )
                    )
                    mark_type.append(types.type_at(group.id, lesson.day))
                by_type: Dict[str, List[engine.Mark]] = {}
                for mark_item, kind in zip(marks, mark_type):
                    by_type.setdefault(kind, []).append(mark_item)
                kinds = sorted(by_type, key=engine.LESSON_TYPES.index)
                if split:  # a type in which the student has no lesson at all gives no line (he was not in the group then)
                    real = [k for k in kinds if any(m.state != engine.NOT_IN_GROUP for m in by_type[k])]
                    kinds = real or kinds[:1]
                for kind in kinds:
                    kind_marks = by_type[kind]
                    # the price of the line on its reference day: the last lesson that carries money, else the price of the last day
                    money_marks = [m for m in kind_marks if m.carries_money and m.priced]
                    if money_marks:
                        last_money = max(money_marks, key=lambda m: (m.day, m.lesson_id))
                        ref_amount, ref_source = last_money.price, last_money.price_source
                        if ref_source == "day":   # a lesson before the accounting: the line still says whose price it was (group / exception)
                            ref_source = books.price(membership.id, membership.group_id, last_money.day).source
                    else:
                        reference = books.price(membership.id, membership.group_id, kind_marks[-1].day)
                        ref_amount, ref_source = reference.amount, reference.source
                    teacher.lines.append(
                        LineSeed(
                            identity={
                                "line_key": f"{membership.id}:{kind}" if split else str(membership.id),
                                "membership_id": str(membership.id),
                                "student_id": str(student.id),
                                "student_name": person_name(student.last_name, student.first_name) or "—",
                                "display_id": student.display_id,
                                "group_id": str(group.id),
                                "group_name": group.name,
                                "lesson_type": kind,
                                "lesson_time": lesson_time,
                            },
                            marks=kind_marks,
                            source_fee=ref_amount,
                            pending_excused=pending_by_type.get(kind, 0),
                            price_source=ref_source,
                        )
                    )

        for tid, teacher in seed.teachers.items():
            teacher.lines.sort(
                key=lambda line: (
                    line.identity["group_name"].casefold(),
                    line.identity["group_id"],
                    line.identity["student_name"].casefold(),
                    line.identity["membership_id"],
                    line.identity["lesson_type"],
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
    def _missing_percents(teacher: TeacherSeed, percents: Dict[str, Decimal]) -> List[str]:
        return [t for t in engine.LESSON_TYPES if t in teacher.groups_by_type and t not in percents]

    @staticmethod
    def _rate_required(missing: List[Dict[str, Any]]) -> PercentRequired:
        return PercentRequired(RATE_REQUIRED, missing=missing)

    @staticmethod
    def _has_manual_fee(lines: Optional[List[Dict[str, Any]]]) -> bool:
        """A line of the sheet carries a manual price (an old sheet): a recalculation would drop it (UX-2), so the sheet
        is "changed" - the same answer in the overview, the list, the sheet and the diff.  A JSON check, nothing is rendered."""
        return any(isinstance(line, dict) and line.get("fee_overridden") for line in lines or ())

    def _stale_by_rates(self, saved: Dict[str, str], book: payroll_rates.RateBook, teacher_id: UUID, first: date) -> bool:
        """The rate in force for the sheet's month is not the percent the sheet was calculated with."""
        return bool(saved) and bool(payroll_rates.rate_changes(saved, book.percents_at(teacher_id, first)))

    def _render(
        self,
        teacher: TeacherSeed,
        percents: Dict[str, Decimal],
        changes: Dict[str, _Change],
        divisor: int,
    ) -> List[Dict[str, Any]]:
        lines = []
        for seed in teacher.lines:
            change = changes.get(seed.identity["line_key"]) or changes.get(seed.identity["membership_id"]) or _Change()      # m12: the line first, then the membership
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
                    price_source=seed.price_source,
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
        payout: Optional[PayoutState] = None,
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
            lines=[LineOut(**{**line, "line_key": line.get("line_key") or line["membership_id"]}) for line in lines],
            groups=[GroupBlockOut(**block) for block in engine.group_blocks(lines)],
            total_amount=counters["total_amount"],
            counted_total=counters["counted"],
            unpaid_total=counters["unpaid"],
            pending_excused_total=counters["pending_excused"],
            written_off_total=counters["written_off"],
            payout=payout,
            warnings=SheetWarnings(
                open_lessons=open_lessons,
                pending_excused=counters["pending_excused"],
                no_fee=counters["no_fee"],
                unpaid=counters["unpaid"],
                unpaid_students=counters["unpaid_students"],
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
        payout: Optional[PayoutState] = None,
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
            payout=payout,
        )

    def _fresh_total(self, org_id: str, sheet: PayrollSheet, seed: Optional[MonthSeed] = None, book=None) -> Optional[int]:
        """What the sheet would total if it were recalculated now (rates of its month, the ticks of its last revision); ``None`` when it
        cannot be (a rate is missing, a line carries a manual price of an old sheet) - the target is then the saved total."""
        if self._has_manual_fee(sheet.lines):
            return None
        first = sheet.period_month
        last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
        seed = seed or self._collect(org_id, first, last, teacher_ids={sheet.teacher_id})
        teacher = seed.teachers.get(sheet.teacher_id) or TeacherSeed(sheet.teacher_id, fingerprint=fingerprint_of(first, sheet.teacher_id, []))
        book = book or payroll_rates.load_book(self.db, org_id, [sheet.teacher_id])
        percents = book.percents_at(sheet.teacher_id, first)
        if self._missing_percents(teacher, percents):
            return None
        return engine.sheet_total(self._render(teacher, percents, self._carried_changes(sheet), seed.divisor))

    def payout_state(self, org_id: str, sheet: PayrollSheet, seed: Optional[MonthSeed] = None, book=None) -> PayoutState:
        from .payroll_payouts import PayrollPayoutService

        return PayrollPayoutService(self.db).state_for(org_id, sheet, self._fresh_total(org_id, sheet, seed, book))

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
                PayrollSheet.lines,                      # m8: the carried ticks come from this row, no ``db.get`` per sheet
                PayrollSheet.lines.contains([{"fee_overridden": True}]).label("manual_fee"),
            ).filter(PayrollSheet.org_id == org_id, PayrollSheet.period_month == first)
        }
        from .payroll_payouts import PayrollPayoutService, state_of

        payouts = PayrollPayoutService(self.db).payouts_of(org_id, month=first)
        teacher_ids = set(seed.teachers) | set(sheets)
        names = self._names(teacher_ids)
        suggestions = self._suggested(org_id, teacher_ids)
        book = payroll_rates.load_book(self.db, org_id, teacher_ids)
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
            in_force = book.percents_at(tid, first)
            payout = None
            if sheet is not None:
                fresh_total = None
                if not sheet.manual_fee:
                    t_seed = teacher or TeacherSeed(tid, fingerprint=fresh)
                    if not self._missing_percents(t_seed, in_force):
                        fresh_total = engine.sheet_total(self._render(t_seed, in_force, self._carried_changes(sheet), seed.divisor))
                payout = state_of(saved_total=int(sheet.total_amount), fresh_total=fresh_total, items=payouts.get((tid, first), []))
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
                    rates={t: engine.format_percent(in_force[t]) for t in engine.LESSON_TYPES if t in types and t in in_force},
                    missing_rates=[t for t in engine.LESSON_TYPES if t in types and t not in in_force],
                    sheet=(
                        MonthSheetRef(
                            id=str(sheet.id),
                            revision=sheet.revision,
                            total_amount=sheet.total_amount,
                            stale=sheet.source_fingerprint != fresh
                            or bool(sheet.manual_fee)
                            or self._stale_by_rates(dict(sheet.percent_inputs or {}), book, tid, first),
                            updated_at=_iso(sheet.updated_at),
                        )
                        if sheet
                        else None
                    ),
                    payout=payout,
                    warnings=MonthWarnings(
                        open_lessons=teacher.open_lessons if teacher else 0,
                        pending_excused=counters["pending_excused"] if counters else 0,
                        no_fee=counters["no_fee"] if counters else 0,
                        unpaid=counters["unpaid"] if counters else 0,
                        unpaid_students=counters["unpaid_students"] if counters else 0,
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
        # the percents of the request (an old front still sends them) are ignored: the rate of the MONTH decides
        book = payroll_rates.load_book(self.db, org_id, [t.teacher_id for t in targets] + list(wanted or ()))
        by_teacher: Dict[UUID, Dict[str, Decimal]] = {
            t.teacher_id: book.percents_at(t.teacher_id, first) for t in targets
        }
        for tid in wanted or ():
            by_teacher.setdefault(tid, book.percents_at(tid, first))
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
            raise self._rate_required(missing)
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
    def _changes_from_body(overrides, known: Optional[Dict[str, List[str]]]) -> Dict[str, Any]:
        """``{line key or membership_id: LineOverride}``; with ``known`` (``_line_keys``) a line that is not on the sheet is a 422.

        A price on a sheet is not edited any more (UX-2): ``monthly_fee`` with a value is a 422."""
        for item in overrides or ():
            if item.monthly_fee is not None:
                raise PriceNotEditable(PRICE_NOT_EDITABLE)
            if known is not None and (
                str(item.membership_id) not in known or (item.line_key is not None and item.line_key not in known[str(item.membership_id)])
            ):
                raise PayrollInvalid("Такой строки нет в ведомости", code="LINE_NOT_FOUND")
        return {(item.line_key or str(item.membership_id)): item for item in overrides or ()}

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
        percents = payroll_rates.load_book(self.db, org_id, [body.teacher_id]).percents_at(body.teacher_id, first)
        missing = self._missing_percents(teacher, percents)
        if missing:
            raise self._rate_required(
                [
                    {"teacher_id": str(body.teacher_id), "lesson_type": t, "lesson_type_label": engine.LESSON_TYPE_LABELS[t]}
                    for t in missing
                ]
            )
        keys = self._line_keys(s.identity for s in teacher.lines)
        given = self._changes_from_body(body.line_overrides, keys)
        self._check_expected(body.expected_fingerprint, teacher.fingerprint)
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
            self._apply_overrides(changes, given, keys)
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
        return self._saved_out(sheet, stale=False, payout=self.payout_state(org_id, sheet))

    @staticmethod
    def _check_expected(expected: Optional[str], fresh: str) -> None:
        """LOGIC-A (F14): saving a calculation needs the fingerprint of the preview the owner looked at (422 ``FINGERPRINT_REQUIRED``);
        if the data moved since, nothing is saved (409 ``SHEET_DATA_CHANGED``)."""
        if not expected:
            raise FingerprintRequired("Откройте расчёт заново: сохранение без проверки данных невозможно")
        if expected != fresh:
            raise DataChanged(
                "Данные изменились после расчёта — пересчитайте и проверьте суммы", source_fingerprint=fresh
            )

    @staticmethod
    def _carried_changes(sheet: Optional[PayrollSheet]) -> Dict[str, _Change]:
        """The owner's decisions on the lines of a saved sheet that are carried to the next revision: the ticks
        "include unpaid", by membership.  A manual price of a line (an old sheet) is NOT carried (UX-2): the price
        of a new revision is the price from «Цены групп» / the card of the student."""
        return {
            (line.get("line_key") or line["membership_id"]): _Change(include_unpaid=bool(line.get("include_unpaid")))
            for line in (sheet.lines or [] if sheet is not None else [])
        }

    @staticmethod
    def _line_keys(lines) -> Dict[str, List[str]]:
        """``{membership_id: [line keys]}`` of the lines of a sheet (``lines``: dicts or seeds' identities)."""
        out: Dict[str, List[str]] = {}
        for line in lines:
            out.setdefault(line["membership_id"], []).append(line.get("line_key") or line["membership_id"])
        return out

    @staticmethod
    def _apply_overrides(changes: Dict[str, _Change], given: Dict[str, Any], keys: Optional[Dict[str, List[str]]] = None) -> None:
        """What a request says about a line wins over what the sheet had; a field it leaves out stays.

        m12: an override with a ``line_key`` is for that line only; one without it is for every line of the membership (``keys`` =
        ``_line_keys``: ``{membership_id: [line keys]}``), and the more specific one is applied last."""
        keys = keys or {}
        for key, item in sorted(given.items(), key=lambda kv: kv[1].line_key is not None):
            for target in ([item.line_key] if item.line_key else keys.get(key) or [key]):
                change = changes.setdefault(target, _Change())
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
        With it the month is read again with the rate of the sheet's month; the owner's ticks are kept, a manual
        price of an old sheet is not (the price comes from «Цены групп» / the card of the student).  Known limit
        (accepted): ``expected_fingerprint`` guards the DATA only - a rate changed between the diff and this call is
        not caught, the recalculation takes the rate in force now and the owner sees the new numbers in the answer.
        """
        if not (body.line_overrides or body.recalculate or (body.note or "").strip()):
            raise PayrollInvalid("Нечего менять", code="NOTHING_TO_CHANGE")
        sheet = self._lock_sheet(org_id, sheet_id, lock=True)
        try:
            if body.revision != sheet.revision:
                raise self._conflict(sheet.revision)
            first = sheet.period_month
            saved_lines: List[Dict[str, Any]] = list(sheet.lines or [])
            # the percents of the body (an old front sends them) are ignored.  A recalculation takes the rate of the
            # sheet's month; a change without it (a tick) is computed from the saved lines with the saved percents
            percents = {t: engine.parse_percent(v) for t, v in (sheet.percent_inputs or {}).items()}

            if body.recalculate:
                given = self._changes_from_body(body.line_overrides, None)
                last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
                seed = self._collect(org_id, first, last, teacher_ids={sheet.teacher_id})
                teacher = seed.teachers.get(sheet.teacher_id) or TeacherSeed(
                    sheet.teacher_id, fingerprint=fingerprint_of(first, sheet.teacher_id, [])
                )
                percents = payroll_rates.load_book(self.db, org_id, [sheet.teacher_id]).percents_at(
                    sheet.teacher_id, first
                )
                missing = self._missing_percents(teacher, percents)
                if missing:
                    raise self._rate_required(
                        [
                            {"teacher_id": str(sheet.teacher_id), "lesson_type": t,
                             "lesson_type_label": engine.LESSON_TYPE_LABELS[t]}
                            for t in missing
                        ]
                    )
                changes = self._carried_changes(sheet)
                keys = self._line_keys(s.identity for s in teacher.lines)
                self._changes_from_body(body.line_overrides, keys)           # LINE_NOT_FOUND for a line (or line key) that is not on the fresh sheet
                self._check_expected(body.expected_fingerprint, teacher.fingerprint)
                self._apply_overrides(changes, given, keys)
                lines = self._render(teacher, percents, changes, seed.divisor)
                fingerprint, divisor_default = teacher.fingerprint, seed.divisor
            else:
                given = self._changes_from_body(body.line_overrides, self._line_keys(saved_lines))
                lines = []
                for line in saved_lines:
                    item = given.get(line.get("line_key") or line["membership_id"]) or given.get(line["membership_id"])      # m12: the line first
                    fee_reset = item is not None and "monthly_fee" in item.model_fields_set  # null: no manual price
                    lines.append(
                        engine.recompute_line(
                            line,
                            percent=percents.get(line["lesson_type"]),
                            fee_override=None,
                            reset_fee=bool(fee_reset),
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
        return self._saved_out(sheet, stale=False if body.recalculate else self._is_stale(org_id, sheet),
                               payout=self.payout_state(org_id, sheet))

    # ----------------------------------------------------------------- read

    def _is_stale(self, org_id: str, sheet: PayrollSheet, seed: Optional[MonthSeed] = None, book=None) -> bool:
        first = sheet.period_month
        last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
        seed = seed or self._collect(org_id, first, last, teacher_ids={sheet.teacher_id})
        teacher = seed.teachers.get(sheet.teacher_id)
        fresh = teacher.fingerprint if teacher else fingerprint_of(first, sheet.teacher_id, [])
        if fresh != sheet.source_fingerprint or self._has_manual_fee(sheet.lines):
            return True
        book = book or payroll_rates.load_book(self.db, org_id, [sheet.teacher_id])
        return self._stale_by_rates(dict(sheet.percent_inputs or {}), book, sheet.teacher_id, first)

    def get_sheet(self, org_id: str, sheet_id: UUID) -> SheetOut:
        sheet = self._lock_sheet(org_id, sheet_id, lock=False)
        first = sheet.period_month
        seed = self._collect(org_id, first, date(first.year, first.month, calendar.monthrange(first.year, first.month)[1]), teacher_ids={sheet.teacher_id})
        book = payroll_rates.load_book(self.db, org_id, [sheet.teacher_id])     # m8: ONE collection of the month serves the stale flag and the payout state
        return self._saved_out(sheet, stale=self._is_stale(org_id, sheet, seed, book), payout=self.payout_state(org_id, sheet, seed, book))

    def list_sheets(self, org_id: str, month: Optional[str]) -> List[SheetBrief]:
        query = self.db.query(
            PayrollSheet.id,
            PayrollSheet.teacher_id,
            PayrollSheet.period_month,
            PayrollSheet.revision,
            PayrollSheet.total_amount,
            PayrollSheet.source_fingerprint,
            PayrollSheet.percent_inputs,
            PayrollSheet.lines.contains([{"fee_overridden": True}]).label("manual_fee"),
            PayrollSheet.updated_by,
            PayrollSheet.updated_at,
        ).filter(PayrollSheet.org_id == org_id)
        fresh: Dict[UUID, str] = {}
        book = None
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
            book = payroll_rates.load_book(self.db, org_id, {r.teacher_id for r in rows})
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
                    or bool(r.manual_fee)
                    or self._stale_by_rates(dict(r.percent_inputs or {}), book, r.teacher_id, first)
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
        """What a recalculation would change: the saved lines against the current data - with the rate in force
        for the sheet's month (UX-2), the prices of «Цены групп» / of the students (a manual price of an old sheet is
        dropped, the line says so) and the ticks "include unpaid" of the sheet."""
        sheet = self._lock_sheet(org_id, sheet_id, lock=False)
        first = sheet.period_month
        last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
        seed = self._collect(org_id, first, last, teacher_ids={sheet.teacher_id})
        teacher = seed.teachers.get(sheet.teacher_id) or TeacherSeed(
            sheet.teacher_id, fingerprint=fingerprint_of(first, sheet.teacher_id, [])
        )
        saved = list(sheet.lines or [])
        percents = payroll_rates.load_book(self.db, org_id, [sheet.teacher_id]).percents_at(sheet.teacher_id, first)
        changes = self._carried_changes(sheet)
        fresh = self._render(teacher, percents, changes, seed.divisor)
        rows = engine.diff_lines(saved, fresh)
        self._note_dropped_fees(rows, saved, fresh)
        rate_rows = payroll_rates.rate_changes(dict(sheet.percent_inputs or {}), percents)
        saved_total, fresh_total = engine.sheet_total(saved), engine.sheet_total(fresh)
        return SheetDiff(
            sheet_id=str(sheet.id),
            revision=sheet.revision,
            stale=teacher.fingerprint != sheet.source_fingerprint
            or bool(rate_rows)
            or self._has_manual_fee(saved),
            source_fingerprint=teacher.fingerprint,
            saved_total=saved_total,
            fresh_total=fresh_total,
            delta_amount=fresh_total - saved_total,
            reasons=[payroll_rates.rate_reason(t, before, now) for t, before, now in rate_rows],
            rate_changes=[
                RateChangeOut(
                    lesson_type=t,
                    saved_percent=engine.format_percent(before),
                    fresh_percent=engine.format_percent(now) if now is not None else None,
                    message=payroll_rates.rate_reason(t, before, now),
                )
                for t, before, now in rate_rows
            ],
            lines=[DiffLine(**row) for row in rows],
        )

    @staticmethod
    def _note_dropped_fees(rows: List[Dict[str, Any]], saved: List[Dict[str, Any]], fresh: List[Dict[str, Any]]) -> bool:
        """A manual price of a saved line is not carried to a recalculation: a changed line says where the price
        comes from now («цена из «Цен групп» вместо ручной 450 000 сум»).  True when there is such a line."""
        manual = {line["membership_id"]: line for line in saved if line.get("fee_overridden")}
        source = {line["membership_id"]: line for line in fresh}
        found = False
        for row in rows:
            old = manual.get(row["membership_id"])
            new = source.get(row["membership_id"])
            if old is None or new is None or "цена в месяц" not in row["changes"]:
                continue
            was = f"{int(old['monthly_fee'] or 0):,}".replace(",", " ")
            origin = {
                "group": "цена из «Цен групп»",
                "override": "индивидуальная цена из карточки ученика",
            }.get(new.get("price_source"), "цена не задана")
            row["changes"].append(f"{origin} вместо ручной {was} сум")
            found = True
        return found

    # ---------------------------------------------------- price changes (#105)

    def sheets_changed_by_group_price(
        self, org_id: str, group_id: UUID, book: pricing.GroupBook, since: date
    ) -> List[Dict[str, Any]]:
        """Saved sheets that would turn "данные изменились" if the price history of ``group_id`` were ``book``.

        Candidates: the saved sheets of the months from ``since`` on that have a line of this group.  For each
        month the data fingerprint of those teachers is computed twice - with the prices as they are and with
        ``book`` - and a sheet is listed when the two differ (a change that touches no lesson of the sheet, or
        only a student with an individual price, leaves the fingerprint alone).  Reads only.
        """
        rows = (
            self.db.query(PayrollSheet.id, PayrollSheet.teacher_id, PayrollSheet.period_month)
            .filter(
                PayrollSheet.org_id == org_id,
                PayrollSheet.period_month >= since.replace(day=1),
                PayrollSheet.lines.contains([{"group_id": str(group_id)}]),
            )
            .order_by(PayrollSheet.period_month, PayrollSheet.teacher_id)
            .all()
        )
        by_month: Dict[date, List[Any]] = {}
        for row in rows:
            by_month.setdefault(row.period_month, []).append(row)
        names = self._names(row.teacher_id for row in rows)
        changed: List[Dict[str, Any]] = []
        for first, sheets in by_month.items():
            last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
            teachers = {row.teacher_id for row in sheets}
            now = self._collect(org_id, first, last, teacher_ids=teachers)
            then = self._collect(org_id, first, last, teacher_ids=teachers, group_books={group_id: book})
            for row in sheets:
                before, after = now.teachers.get(row.teacher_id), then.teachers.get(row.teacher_id)
                empty = fingerprint_of(first, row.teacher_id, [])
                if (before.fingerprint if before and before.fingerprint else empty) != (
                    after.fingerprint if after and after.fingerprint else empty
                ):
                    changed.append(
                        {
                            "sheet_id": str(row.id),
                            "month": month_label(first),
                            "teacher_id": str(row.teacher_id),
                            "teacher_name": names.get(row.teacher_id),
                        }
                    )
        return changed

    def sheets_changed_by_override(self, org_id: str, membership: StudentGroup, book: "pricing.OverrideBook", since: date) -> List[Dict[str, Any]]:
        """Saved sheets that would turn "данные изменились" if the exception history of ``membership`` were ``book`` (same comparison)."""
        sheets = (
            self.db.query(PayrollSheet.id, PayrollSheet.teacher_id, PayrollSheet.period_month)
            .filter(
                PayrollSheet.org_id == org_id,
                PayrollSheet.period_month >= since.replace(day=1),
                PayrollSheet.lines.contains([{"membership_id": str(membership.id)}]),
            )
            .order_by(PayrollSheet.period_month, PayrollSheet.teacher_id)
            .all()
        )
        names = self._names(s.teacher_id for s in sheets)
        changed: List[Dict[str, Any]] = []
        by_month: Dict[date, List[Any]] = {}
        for row in sheets:
            by_month.setdefault(row.period_month, []).append(row)
        for first, items in by_month.items():
            last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
            teachers = {row.teacher_id for row in items}
            now = self._collect(org_id, first, last, teacher_ids=teachers)
            then = self._collect(org_id, first, last, teacher_ids=teachers, override_books={membership.id: book})
            for row in items:
                before, after = now.teachers.get(row.teacher_id), then.teachers.get(row.teacher_id)
                empty = fingerprint_of(first, row.teacher_id, [])
                if (before.fingerprint if before and before.fingerprint else empty) != (
                    after.fingerprint if after and after.fingerprint else empty
                ):
                    changed.append({"sheet_id": str(row.id), "month": month_label(first), "teacher_id": str(row.teacher_id),
                                    "teacher_name": names.get(row.teacher_id)})
        return changed

    def sheets_changed_by_lesson_type(
        self, org_id: str, group: Group, rows: List[Tuple[date, str]], since: date
    ) -> List[Dict[str, Any]]:
        """Saved sheets that would turn "данные изменились" if the lesson-type history of ``group`` were ``rows`` (F14): the same
        two-fingerprint comparison as ``sheets_changed_by_group_price``.  Reads only."""
        sheets = (
            self.db.query(PayrollSheet.id, PayrollSheet.teacher_id, PayrollSheet.period_month)
            .filter(
                PayrollSheet.org_id == org_id,
                PayrollSheet.period_month >= since.replace(day=1),
                PayrollSheet.lines.contains([{"group_id": str(group.id)}]),
            )
            .order_by(PayrollSheet.period_month, PayrollSheet.teacher_id)
            .all()
        )
        names = self._names(s.teacher_id for s in sheets)
        changed: List[Dict[str, Any]] = []
        by_month: Dict[date, List[Any]] = {}
        for row in sheets:
            by_month.setdefault(row.period_month, []).append(row)
        for first, items in by_month.items():
            last = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
            teachers = {row.teacher_id for row in items}
            now = self._collect(org_id, first, last, teacher_ids=teachers)
            then = self._collect(org_id, first, last, teacher_ids=teachers, type_rows={group.id: rows})
            for row in items:
                before, after = now.teachers.get(row.teacher_id), then.teachers.get(row.teacher_id)
                empty = fingerprint_of(first, row.teacher_id, [])
                if (before.fingerprint if before and before.fingerprint else empty) != (
                    after.fingerprint if after and after.fingerprint else empty
                ):
                    changed.append({"sheet_id": str(row.id), "month": month_label(first), "teacher_id": str(row.teacher_id),
                                    "teacher_name": names.get(row.teacher_id)})
        return changed

    # --------------------------------------------------------------- export

    def sheets_for_export(self, org_id: str, sheet_id: Optional[UUID], month: Optional[str], *, unsaved: Optional[List[Dict[str, Any]]] = None) -> Tuple[List[SheetOut], str]:
        """The saved sheets to put into a workbook and its month label (one sheet, or every sheet of a month).  ``unsaved`` (a month only; a list the caller passes
        in) is filled with ``{"teacher_id", "teacher_name"}`` of the teachers who held lessons in the month but have no saved sheet - from the SAME single collection of
        the month, so the export still reads the month once."""
        if sheet_id is not None:
            sheet = self._lock_sheet(org_id, sheet_id, lock=False)
            return [self._saved_out(sheet, stale=None, payout=self.payout_state(org_id, sheet))], month_label(sheet.period_month)
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
        # m8: ONE collection of the month (all the teachers) and one book of rates serve every sheet
        seed = self._collect(org_id, first, date(first.year, first.month, calendar.monthrange(first.year, first.month)[1]),
                             teacher_ids=None if unsaved is not None else {r.teacher_id for r in rows})
        book = payroll_rates.load_book(self.db, org_id, {r.teacher_id for r in rows})
        outs = [self._saved_out(r, stale=None, names=names, notes=notes, payout=self.payout_state(org_id, r, seed, book)) for r in rows]
        outs.sort(key=lambda o: ((o.teacher_name or "").casefold(), o.teacher_id))
        if unsaved is not None:
            saved_ids = {r.teacher_id for r in rows}
            missing = [tid for tid, t in seed.teachers.items() if tid not in saved_ids and t.lessons_count > 0]
            names_missing = self._names(missing)
            unsaved.extend(sorted(({"teacher_id": str(tid), "teacher_name": names_missing.get(tid)} for tid in missing),
                                  key=lambda x: ((x["teacher_name"] or "").casefold(), x["teacher_id"])))
        return outs, month_label(first)
