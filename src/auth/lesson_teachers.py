"""Who teaches a lesson (LOGIC-B O5 / F6, SPEC 0.3a and 3.4).

    lesson_teacher(group, lesson occurrence) =
        1. the active ``lesson_teacher_assignments`` row (group, lesson, date), if any;
        2. else ``group_teacher_at(group, end_instant)`` with ``end_instant`` = the planned END of the lesson as a naive UTC instant.

``group_teacher_at(group, instant)``: the first ``group_audit`` row ``teacher_changed`` with ``created_at > instant`` says (``snapshot.teacher_id[0]``)
who the teacher was; without such a row it is the group's current teacher.  A change exactly at ``end_instant`` has already come into force.
So a handover takes everything that is still ahead (also the lesson that is running by the schedule and has not been started); the lessons
that ended before the handover stay with the old teacher.

A ``group_attendance`` row freezes the teacher when it is created (``teacher_id``); afterwards only ``PUT /org/groups/{id}/lesson-teacher`` changes it.
That column is the ONLY source of "who taught" for payroll (never ``marked_by``).

Lock order of the operations here (SPEC 0.4): member / student -> groups (ascending id) -> ``group_attendance`` rows -> User (teacher) -> assignments.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

from . import business_clock as bc
from . import payroll_hooks
from src.models.auth import PayrollRate

from .models import (
    Group,
    GroupAttendance,
    GroupAttendanceAudit,
    GroupAudit,
    LessonTeacherAssignment,
    OrganizationMember,
    User,
)
from .ops_rules import (
    OpsError,
    conflict,
    forbidden,
    hhmm_to_minutes,
    minutes_overlap,
    not_found,
    short_name,
    teacher_lesson_window_start,
    unprocessable,
)

logger = logging.getLogger(__name__)

TEACHING_ROLES = ("teacher",)  # W1 round 2 (#156): lessons are led by a TEACHER only; an owner / administrator leads nothing (the one place of the rule)


REASON_REMOVED = "Сотрудник удалён: замена снята"
REASON_ROLE_CHANGED = "Роль изменена: замена снята"


def can_lead(org_role: Optional[str]) -> bool:
    """May a member with this role lead lessons (main teacher of a group, substitute, teacher of an import row, receiver of a payroll rate)?"""
    return org_role in TEACHING_ROLES


CONFLICT_LIMIT = 20
HORIZON_DAYS = 56
LESSON_DATE_MAX_AHEAD_DAYS = 365
CANCELLED = "cancelled"
COMPLETED = "completed"
IN_PROGRESS = "in_progress"


def _s(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


def _uuid(value: Any) -> Optional[UUID]:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


# ===================================================================================== the book (history + assignments)


@dataclass(frozen=True)
class Resolved:
    teacher_id: Optional[UUID]
    default_teacher_id: Optional[UUID]
    substitution: bool


class TeacherBook:
    """The history of teacher changes and the active substitutions of many groups, loaded in batches (one query each)."""

    def __init__(self, db) -> None:
        self.db = db
        self._history: Dict[UUID, List[Tuple[datetime, Optional[UUID], Optional[UUID]]]] = {}
        self._assignments: Dict[Tuple[UUID, str, str], Optional[LessonTeacherAssignment]] = {}
        self._covered: Dict[UUID, Tuple[str, str]] = {}

    # ---- history
    def load_history(self, group_ids: Iterable[UUID]) -> None:
        wanted = {gid for gid in group_ids if gid not in self._history}
        if not wanted:
            return
        for gid in wanted:
            self._history[gid] = []
        rows = (
            self.db.query(GroupAudit)
            .filter(GroupAudit.group_id.in_(wanted), GroupAudit.action == "teacher_changed")
            .order_by(GroupAudit.created_at, GroupAudit.id)
            .all()
        )
        for row in rows:
            pair = (row.snapshot or {}).get("teacher_id") or [None, None]
            old = _uuid(pair[0]) if isinstance(pair, (list, tuple)) and len(pair) > 0 else None
            new = _uuid(pair[1]) if isinstance(pair, (list, tuple)) and len(pair) > 1 else None
            self._history[row.group_id].append((row.created_at, old, new))

    def history(self, group_id: UUID) -> List[Tuple[datetime, Optional[UUID], Optional[UUID]]]:
        self.load_history([group_id])
        return self._history[group_id]

    def group_teacher_at(self, group: Group, instant: datetime) -> Optional[UUID]:
        """SPEC 0.3a: the first change strictly after ``instant`` names the old teacher; no such change = the current one."""
        for created_at, old, _new in self.history(group.id):
            if created_at is not None and created_at > instant:
                return old
        return group.teacher_id

    def teachers_in_window(self, group: Group, instant: datetime) -> Set[UUID]:
        """Everybody who was the teacher of the group at some moment since ``instant`` (including now)."""
        out: Set[UUID] = set()
        first = self.group_teacher_at(group, instant)
        if first:
            out.add(first)
        for created_at, _old, new in self.history(group.id):
            if created_at is not None and created_at > instant and new:
                out.add(new)
        if group.teacher_id:
            out.add(group.teacher_id)
        return out

    # ---- substitutions
    def load_assignments(self, group_ids: Iterable[UUID], date_from: str, date_to: str) -> None:
        ids = [gid for gid in set(group_ids)]
        if not ids:
            return
        rows = (
            self.db.query(LessonTeacherAssignment)
            .filter(
                LessonTeacherAssignment.group_id.in_(ids),
                LessonTeacherAssignment.cancelled_at.is_(None),
                LessonTeacherAssignment.lesson_date >= date_from,
                LessonTeacherAssignment.lesson_date <= date_to,
            )
            .all()
        )
        for gid in ids:
            previous = self._covered.get(gid)
            self._covered[gid] = (
                min(previous[0], date_from) if previous else date_from,
                max(previous[1], date_to) if previous else date_to,
            )
        for key in [k for k in self._assignments if k[0] in ids and date_from <= k[2] <= date_to]:
            self._assignments.pop(key)
        for row in rows:
            self._assignments[(row.group_id, row.lesson_id, row.lesson_date)] = row

    def assignment(self, group_id: UUID, lesson_id: str, lesson_date: str) -> Optional[LessonTeacherAssignment]:
        key = (group_id, lesson_id, lesson_date)
        if key in self._assignments:
            return self._assignments[key]
        covered = self._covered.get(group_id)
        if covered and covered[0] <= lesson_date <= covered[1]:
            return None
        row = (
            self.db.query(LessonTeacherAssignment)
            .filter(
                LessonTeacherAssignment.group_id == group_id,
                LessonTeacherAssignment.lesson_id == lesson_id,
                LessonTeacherAssignment.lesson_date == lesson_date,
                LessonTeacherAssignment.cancelled_at.is_(None),
            )
            .first()
        )
        self._assignments[key] = row
        return row

    def forget(self, group_id: UUID, lesson_id: str, lesson_date: str) -> None:
        self._assignments.pop((group_id, lesson_id, lesson_date), None)

    # ---- the rule
    @staticmethod
    def end_instant(lesson_date: str, end_minutes: Optional[int]) -> datetime:
        day = date.fromisoformat(lesson_date)
        return bc.lesson_instant_utc(day, end_minutes if end_minutes is not None else 24 * 60 - 1)

    def default_teacher(self, group: Group, lesson_date: str, end_minutes: Optional[int]) -> Optional[UUID]:
        return self.group_teacher_at(group, self.end_instant(lesson_date, end_minutes))

    def resolve(self, group: Group, lesson_id: str, lesson_date: str, end_minutes: Optional[int]) -> Resolved:
        default = self.default_teacher(group, lesson_date, end_minutes)
        asg = self.assignment(group.id, lesson_id, lesson_date)
        if asg is not None:
            return Resolved(asg.teacher_id, default, asg.teacher_id != default)
        return Resolved(default, default, False)


def group_teacher_at(db, group: Group, instant: datetime) -> Optional[UUID]:
    return TeacherBook(db).group_teacher_at(group, instant)


def lesson_teacher(
    db, group: Group, lesson_id: str, lesson_date: str, occurrence: Optional[Dict[str, Any]] = None
) -> Optional[UUID]:
    end = hhmm_to_minutes(occurrence["endTime"]) if occurrence and occurrence.get("endTime") else None
    return TeacherBook(db).resolve(group, lesson_id, lesson_date, end).teacher_id


def effective_teacher(row: Optional[GroupAttendance], resolved: Resolved) -> Optional[UUID]:
    """Who leads the lesson: the teacher frozen in its row, else the resolver's (a legacy row without teacher follows the resolver)."""
    if row is not None and row.teacher_id is not None:
        return row.teacher_id
    return resolved.teacher_id


# ============================================================================================== the service


@dataclass
class BusyItem:
    date: str
    start: int
    end: int
    startTime: str
    endTime: str
    group_id: str
    group_name: str
    lesson_id: str
    substitution: bool = False  # the teacher leads it as a substitute (LOGIC-B2: ``busyWith.kind``)

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.group_id, self.lesson_id, self.date)


class LessonTeachers:
    """The operations on lesson teachers; ``svc`` is the ``AuthService`` (schedule resolver, session)."""

    def __init__(self, svc) -> None:
        self.svc = svc
        self.db = svc.db
        self.last_released: List[Tuple[LessonTeacherAssignment, Group]] = []
        self.last_assignment = None  # the active assignment the last ``_put_locked`` left (None when the lesson went back to its group teacher)

    # ---------------------------------------------------------------------------------------------- people
    def teaching_member(self, org_id: Any, user_id: Any) -> Optional[Tuple[User, OrganizationMember]]:
        """The user, if he is an active member of the centre who can teach (``can_lead``: a teacher) and his account is active."""
        uid = _uuid(user_id)
        if uid is None:
            return None
        row = (
            self.db.query(User, OrganizationMember)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .filter(
                OrganizationMember.org_id == org_id,
                User.id == uid,
                OrganizationMember.is_active.is_(True),
                OrganizationMember.org_role.in_(TEACHING_ROLES),
                User.is_active.isnot(False),
            )
            .first()
        )
        return (row[0], row[1]) if row else None

    def staff_who_can_teach(self, org_id: Any) -> List[User]:
        rows = (
            self.db.query(User)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .filter(
                OrganizationMember.org_id == org_id,
                OrganizationMember.is_active.is_(True),
                OrganizationMember.org_role.in_(TEACHING_ROLES),
                User.is_active.isnot(False),
            )
            .order_by(User.last_name, User.first_name, User.id)
            .all()
        )
        return rows

    def lesson_type_on(self, group: Group, lesson_date: str) -> str:
        """The kind of lessons of the group ON the day of the lesson (the payroll rate is kept per kind; the group may have changed its kind)."""
        from . import lesson_types  # lazy

        return lesson_types.load_book(self.db, [group]).type_at(group.id, date.fromisoformat(lesson_date))

    def rates_missing(self, org_id: Any, teacher_ids: Iterable[UUID], lesson_type: Optional[str], lesson_date: str) -> Set[UUID]:
        """W1 (F5, D3): the teachers (of ``teacher_ids``) without a payroll rate for the type of lessons in the month of the lesson - one query for all of them.  A yes / no only:
        the rates themselves stay the owner's."""
        ids = list(teacher_ids)
        if not ids:
            return set()
        month = date.fromisoformat(lesson_date[:7] + "-01")
        have = {t for (t,) in self.db.query(PayrollRate.teacher_id).filter(
            PayrollRate.org_id == org_id, PayrollRate.teacher_id.in_(ids), PayrollRate.lesson_type == (lesson_type or "group"), PayrollRate.valid_from <= month)}
        return set(ids) - have

    def users_by_id(self, ids: Iterable[Any]) -> Dict[str, User]:
        wanted = {u for u in (_uuid(i) for i in ids) if u}
        if not wanted:
            return {}
        return {str(u.id): u for u in self.db.query(User).filter(User.id.in_(wanted)).all()}

    # ------------------------------------------------------------------------------------------- reading
    def info_for(self, book: TeacherBook, group: Group, lesson_id: str, lesson_date: str, end_minutes: Optional[int],
                 row: Optional[GroupAttendance], names: Dict[str, User]) -> Dict[str, Any]:
        res = book.resolve(group, lesson_id, lesson_date, end_minutes)
        teacher = effective_teacher(row, res)
        default = res.default_teacher_id
        return {
            "teacherId": _s(teacher),
            "teacherName": short_name(names.get(_s(teacher))) if teacher else None,
            "isSubstitution": bool(teacher is not None and default is not None and teacher != default)
            or bool(teacher is not None and default is None),
            "defaultTeacherId": _s(default),
            "defaultTeacherName": short_name(names.get(_s(default))) if default else None,
        }

    def annotated_occurrences(self, groups: Sequence[Group], d_from: date, d_to: date, *, only_teacher: Any = None,
                              preloaded=None, include_replaced: bool = False) -> List[Dict[str, Any]]:
        """The resolved occurrences of ``groups`` with the teacher of each lesson (``teacherId``, ``teacherName``, ``isSubstitution``,
        ``defaultTeacherId``, ``defaultTeacherName``).  ``only_teacher`` keeps the lessons that teacher leads (substitutions included).  ``include_replaced`` (W1 F10: the schedule and the dashboard of a teacher) also keeps HIS lessons
        that somebody else substitutes - marked ``isSubstitution`` with ``defaultTeacherId`` = him and ``replacedBy {id, name}`` (he sees «Замена: <имя>»; the rights on such a lesson are
        the substitute's: he cannot start or mark it)."""
        wanted = str(only_teacher) if only_teacher is not None else None
        ids = [g.id for g in groups]
        if not ids:
            return []
        book = TeacherBook(self.db)
        book.load_history(ids)
        book.load_assignments(ids, d_from.isoformat(), d_to.isoformat())
        preload = preloaded or self.svc._preload_schedule_data(ids, date_from=d_from, date_to=d_to)
        rows = {
            (str(r.group_id), r.lesson_id, r.lesson_date): r
            for r in self.db.query(GroupAttendance).filter(
                GroupAttendance.group_id.in_(ids),
                GroupAttendance.lesson_date >= d_from.isoformat(),
                GroupAttendance.lesson_date <= d_to.isoformat(),
            )
        }
        out: List[Dict[str, Any]] = []
        pending: List[Tuple[Dict[str, Any], Optional[UUID], Optional[UUID]]] = []
        for group in groups:
            for occ in self.svc._resolved_occurrences_for_group(group, date_from=d_from, date_to=d_to, preloaded=preload):
                try:
                    end = hhmm_to_minutes(occ["endTime"])
                except (KeyError, ValueError, AttributeError):
                    end = None
                res = book.resolve(group, occ["id"], occ["occurrenceDate"], end)
                teacher = effective_teacher(rows.get((str(group.id), occ["id"], occ["occurrenceDate"])), res)
                if wanted is not None and str(teacher or "") != wanted:
                    if not (include_replaced and teacher is not None and str(res.default_teacher_id or "") == wanted):
                        continue
                pending.append((occ, teacher, res.default_teacher_id))
        names = self.users_by_id([x for _o, t, d in pending for x in (t, d)])
        for occ, teacher, default in pending:
            replaced = wanted is not None and str(teacher or "") != wanted
            out.append(
                {
                    **occ,
                    **({"replacedBy": {"id": _s(teacher), "name": short_name(names.get(_s(teacher)))}} if replaced else {}),
                    "teacherId": _s(teacher),
                    "teacherName": short_name(names.get(_s(teacher))) if teacher else None,
                    "isSubstitution": bool(teacher is not None and default is not None and teacher != default),
                    "defaultTeacherId": _s(default),
                    "defaultTeacherName": short_name(names.get(_s(default))) if default else None,
                }
            )
        return out

    # -------------------------------------------------------------------------------------- group access
    def can_read_group(self, group: Group, actor_id: Any) -> bool:
        """A teacher reads the group when he is its teacher now, was one within the last 7 days, or has a substitution there
        (``lesson_date >= today - 7``).  What he sees of it is narrowed by ``attendance_rules.restrict_journal`` (LOGIC-B2)."""
        actor = _uuid(actor_id)
        if actor is None:
            return False
        if group.teacher_id == actor:
            return True
        today = bc.business_today()
        window_from = teacher_lesson_window_start(today)
        book = TeacherBook(self.db)
        if actor in book.teachers_in_window(group, bc.lesson_instant_utc(window_from, 0)):
            return True
        return (
            self.db.query(LessonTeacherAssignment.id)
            .filter(
                LessonTeacherAssignment.group_id == group.id,
                LessonTeacherAssignment.teacher_id == actor,
                LessonTeacherAssignment.cancelled_at.is_(None),
                LessonTeacherAssignment.lesson_date >= window_from.isoformat(),
            )
            .first()
            is not None
        )

    def groups_visible_to_teacher(self, org_id: Any, actor_id: Any, *, window_from: date, window_to: Optional[date] = None,
                                  include_history: bool = True) -> Dict[UUID, str]:
        """``{group_id: reason}`` with reason ``own`` | ``substitution`` | ``history`` (was the teacher in the window)."""
        actor = _uuid(actor_id)
        out: Dict[UUID, str] = {}
        if actor is None:
            return out
        for (gid,) in self.db.query(Group.id).filter(Group.org_id == org_id, Group.teacher_id == actor).all():
            out[gid] = "own"
        q = self.db.query(LessonTeacherAssignment.group_id).filter(
            LessonTeacherAssignment.org_id == org_id,
            LessonTeacherAssignment.teacher_id == actor,
            LessonTeacherAssignment.cancelled_at.is_(None),
            LessonTeacherAssignment.lesson_date >= window_from.isoformat(),
        )
        if window_to is not None:
            q = q.filter(LessonTeacherAssignment.lesson_date <= window_to.isoformat())
        for (gid,) in q.distinct().all():
            out.setdefault(gid, "substitution")
        if include_history:
            since = bc.lesson_instant_utc(window_from, 0)
            rows = (
                self.db.query(GroupAudit.group_id, GroupAudit.snapshot, GroupAudit.created_at)
                .filter(GroupAudit.org_id == org_id, GroupAudit.action == "teacher_changed", GroupAudit.created_at >= since)
                .all()
            )
            for gid, snapshot, _created in rows:
                pair = (snapshot or {}).get("teacher_id") or [None, None]
                if actor in {_uuid(x) for x in pair}:
                    out.setdefault(gid, "history")
        return out

    # ---------------------------------------------------------------------------------------- busy checks
    def busy_items(self, org_id: Any, teacher_id: UUID, d_from: date, d_to: date, *, book: Optional[TeacherBook] = None,
                   exclude_groups: Optional[Set[str]] = None, exclude_keys: Optional[Set[Tuple[str, str, str]]] = None) -> List[BusyItem]:
        """Every lesson of ``teacher_id`` in [d_from, d_to]: as the teacher of an active group (a lesson substituted away does not count)
        and as a substitute (the started / finished lessons count with the teacher frozen in their row; cancelled ones do not)."""
        book = book or TeacherBook(self.db)
        exclude_groups = exclude_groups or set()
        exclude_keys = exclude_keys or set()
        sub_groups = {
            gid
            for (gid,) in self.db.query(LessonTeacherAssignment.group_id)
            .filter(
                LessonTeacherAssignment.org_id == org_id,
                LessonTeacherAssignment.teacher_id == teacher_id,
                LessonTeacherAssignment.cancelled_at.is_(None),
                LessonTeacherAssignment.lesson_date >= d_from.isoformat(),
                LessonTeacherAssignment.lesson_date <= d_to.isoformat(),
            )
            .all()
        }
        # a lesson he STARTED / held in a group that was handed over afterwards stays his (the row freezes the teacher): count those groups too
        frozen_groups = {
            gid
            for (gid,) in self.db.query(GroupAttendance.group_id)
            .filter(
                GroupAttendance.org_id == org_id,
                GroupAttendance.teacher_id == teacher_id,
                GroupAttendance.status.in_(("in_progress", "completed")),
                GroupAttendance.lesson_date >= d_from.isoformat(),
                GroupAttendance.lesson_date <= d_to.isoformat(),
            )
            .all()
        }
        q = self.db.query(Group).filter(Group.org_id == org_id, Group.status == "active")
        from sqlalchemy import or_

        q = q.filter(or_(Group.teacher_id == teacher_id, Group.id.in_(sub_groups | frozen_groups))) if (sub_groups or frozen_groups) else q.filter(Group.teacher_id == teacher_id)
        groups = [g for g in q.all() if str(g.id) not in exclude_groups]
        return self._items_of_groups(groups, d_from, d_to, book, exclude_keys, only_teacher=teacher_id).get(teacher_id, [])

    def busy_map(self, org_id: Any, d_from: date, d_to: date, *, exclude_keys: Optional[Set[Tuple[str, str, str]]] = None,
                 book: Optional[TeacherBook] = None) -> Dict[UUID, List[BusyItem]]:
        """``{teacher_id: [BusyItem]}`` for EVERY teacher at once (one pass over the active groups): the candidates list without a query set per person."""
        book = book or TeacherBook(self.db)
        groups = self.db.query(Group).filter(Group.org_id == org_id, Group.status == "active").all()
        return self._items_of_groups(groups, d_from, d_to, book, exclude_keys or set(), only_teacher=None)

    def _items_of_groups(self, groups: Sequence[Group], d_from: date, d_to: date, book: TeacherBook, exclude_keys: Set[Tuple[str, str, str]],
                         only_teacher: Optional[UUID]) -> Dict[UUID, List[BusyItem]]:
        out: Dict[UUID, List[BusyItem]] = {}
        if not groups:
            return out
        ids = [g.id for g in groups]
        book.load_history(ids)
        book.load_assignments(ids, d_from.isoformat(), d_to.isoformat())
        preload = self.svc._preload_schedule_data(ids, date_from=d_from, date_to=d_to)
        rows = {
            (str(r.group_id), r.lesson_id, r.lesson_date): r
            for r in self.db.query(GroupAttendance).filter(
                GroupAttendance.group_id.in_(ids),
                GroupAttendance.lesson_date >= d_from.isoformat(),
                GroupAttendance.lesson_date <= d_to.isoformat(),
            )
        }
        for group in groups:
            for occ in self.svc._resolved_occurrences_for_group(group, date_from=d_from, date_to=d_to, preloaded=preload):
                day = occ["occurrenceDate"]
                key = (str(group.id), occ["id"], day)
                if key in exclude_keys:
                    continue
                row = rows.get(key)
                if row is not None and row.status == CANCELLED:
                    continue
                try:
                    start, end = hhmm_to_minutes(occ["startTime"]), hhmm_to_minutes(occ["endTime"])
                except (KeyError, ValueError, AttributeError):
                    continue
                res = book.resolve(group, occ["id"], day, end)
                teacher = effective_teacher(row, res)
                if teacher is None or (only_teacher is not None and teacher != only_teacher):
                    continue
                out.setdefault(teacher, []).append(BusyItem(day, start, end, occ["startTime"], occ["endTime"], str(group.id), group.name, occ["id"],
                                                            substitution=bool(res.substitution)))
        return out

    @staticmethod
    def overlap_pairs(candidates: Sequence[BusyItem], busy: Sequence[BusyItem]) -> List[Tuple[BusyItem, BusyItem]]:
        """(candidate, busy) pairs of the same day that overlap (half-open intervals), never a lesson against itself."""
        by_day: Dict[str, List[BusyItem]] = {}
        for item in busy:
            by_day.setdefault(item.date, []).append(item)
        out: List[Tuple[BusyItem, BusyItem]] = []
        for cand in sorted(candidates, key=lambda c: (c.date, c.start, c.group_name)):
            for other in sorted(by_day.get(cand.date, []), key=lambda o: (o.start, o.group_name)):
                if other.key != cand.key and minutes_overlap(cand.start, cand.end, other.start, other.end):
                    out.append((cand, other))
        return out

    @staticmethod
    def conflict_dict(cand: BusyItem, other: BusyItem) -> Dict[str, Any]:
        return {
            "date": cand.date,
            "startTime": cand.startTime,
            "endTime": cand.endTime,
            "groupId": cand.group_id,
            "groupName": cand.group_name,
            "withGroupName": other.group_name,
        }

    def overlaps(self, candidates: Sequence[BusyItem], busy: Sequence[BusyItem]) -> List[Dict[str, Any]]:
        return [self.conflict_dict(c, o) for c, o in self.overlap_pairs(candidates, busy)]

    def teacher_conflicts(self, org_id: Any, teacher_id: UUID, candidates: Sequence[BusyItem], *,
                          ignore: Optional[Set[Tuple[str, str, str]]] = None, exclude_groups: Optional[Set[str]] = None,
                          extra_busy: Sequence[BusyItem] = (), book: Optional[TeacherBook] = None) -> List[Dict[str, Any]]:
        """Single check of "does this teacher already teach at that time" (his own lessons as the group teacher, his substitutions,
        the lessons of the batch in ``extra_busy``)."""
        if not candidates:
            return []
        d_from = date.fromisoformat(min(c.date for c in candidates))
        d_to = date.fromisoformat(max(c.date for c in candidates))
        busy = self.busy_items(org_id, teacher_id, d_from, d_to, book=book, exclude_groups=exclude_groups, exclude_keys=ignore)
        return self.overlaps(candidates, [*busy, *extra_busy])

    def raise_if_busy(self, conflicts: List[Dict[str, Any]], teacher_name: Optional[str] = None) -> None:
        if not conflicts:
            return
        first = conflicts[0]
        who = f"У преподавателя {teacher_name} " if teacher_name else "У преподавателя "
        raise conflict(
            "TEACHER_BUSY",
            f"{who}уже есть урок «{first['withGroupName']}» {first['date']} {first['startTime']}–{first['endTime']}",
            conflicts=conflicts[:CONFLICT_LIMIT],
            total=len(conflicts),
        )

    # ------------------------------------------------------------------------ lessons that move on a handover
    def moving_lessons(self, group: Group, *, now_utc: Optional[datetime] = None, book: Optional[TeacherBook] = None,
                       horizon_days: int = HORIZON_DAYS) -> List[BusyItem]:
        """The lessons of ``group`` that pass to a NEW teacher of the group: planned end after now, not substituted away, not cancelled,
        without a started / finished row of their own, within the next ``horizon_days`` days."""
        now_utc = now_utc or bc.utc_now()
        today = bc.business_today()
        d_to = today + timedelta(days=horizon_days - 1)
        book = book or TeacherBook(self.db)
        book.load_history([group.id])
        book.load_assignments([group.id], today.isoformat(), d_to.isoformat())
        rows = {
            (r.lesson_id, r.lesson_date): r
            for r in self.db.query(GroupAttendance).filter(
                GroupAttendance.group_id == group.id,
                GroupAttendance.lesson_date >= today.isoformat(),
                GroupAttendance.lesson_date <= d_to.isoformat(),
            )
        }
        out: List[BusyItem] = []
        for occ in self.svc._resolved_occurrences_for_group(group, date_from=today, date_to=d_to):
            day = occ["occurrenceDate"]
            try:
                start, end = hhmm_to_minutes(occ["startTime"]), hhmm_to_minutes(occ["endTime"])
            except (KeyError, ValueError, AttributeError):
                continue
            if bc.lesson_instant_utc(date.fromisoformat(day), end) <= now_utc:
                continue
            if (occ["id"], day) in rows:
                continue
            asg = book.assignment(group.id, occ["id"], day)
            if asg is not None and asg.teacher_id != group.teacher_id:
                continue  # substituted by somebody who is not the new teacher either: stays with the substitute
            out.append(BusyItem(day, start, end, occ["startTime"], occ["endTime"], str(group.id), group.name, occ["id"]))
        return out

    def check_new_group_teacher(self, org_id: Any, moves: Dict[UUID, Tuple[Group, Optional[UUID]]], *, include_unchanged: bool = False) -> None:
        """Overlaps of the new teachers over the next 56 days, the batch included.  ``moves`` = ``{group_id: (group, new_teacher_id)}``.
        Raises ``TEACHER_BUSY`` (the first teacher with a conflict, in id order) with up to 20 ``conflicts`` and ``total``."""
        book = TeacherBook(self.db)
        candidates: Dict[UUID, List[BusyItem]] = {}
        leaving: Dict[UUID, Set[str]] = {}
        for group, new_teacher in moves.values():
            if new_teacher is None or (new_teacher == group.teacher_id and not include_unchanged):
                continue
            candidates.setdefault(new_teacher, []).extend(self.moving_lessons(group, book=book))
            if group.teacher_id and group.teacher_id != new_teacher:
                leaving.setdefault(group.teacher_id, set()).add(str(group.id))
        for teacher_id in sorted(candidates, key=str):
            cands = candidates[teacher_id]
            if not cands:
                continue
            batch_groups = {c.group_id for c in cands}
            # the teacher's own lessons (the groups of the batch are judged by their candidates, the ones he leaves do not count) ...
            external = self.teacher_conflicts(
                org_id, teacher_id, cands, exclude_groups=leaving.get(teacher_id, set()) | batch_groups, book=book
            )
            # ... and the batch against itself: two groups handed to one teacher must not overlap each other
            internal = [
                self.conflict_dict(c, o)
                for c, o in self.overlap_pairs(cands, cands)
                if c.group_id != o.group_id
            ]
            merged = {tuple(sorted(c.items())) for c in [*external, *internal]}
            conflicts = sorted((dict(t) for t in merged), key=lambda c: (c["date"], c["startTime"], c["groupName"], c["withGroupName"]))
            if conflicts:
                user = self.db.query(User).filter(User.id == teacher_id).first()
                self.raise_if_busy(conflicts, short_name(user))

    def check_substitutes_of_group(self, org_id: Any, group: Group) -> None:
        """W1 S23 (D26): a group that comes back from the archive gets its lessons back WITH their substitutes (the assignments stayed): each substitute must be free then - the new lessons of
        his own may have been given meanwhile (``TEACHER_BUSY``, the first substitute with a conflict).  The same horizon as a handover."""
        today = bc.business_today()
        d_to = today + timedelta(days=HORIZON_DAYS - 1)
        book = TeacherBook(self.db)
        by_teacher: Dict[UUID, List[BusyItem]] = {}
        asgs = (self.db.query(LessonTeacherAssignment).filter(LessonTeacherAssignment.group_id == group.id, LessonTeacherAssignment.cancelled_at.is_(None),
                                                              LessonTeacherAssignment.lesson_date >= today.isoformat(), LessonTeacherAssignment.lesson_date <= d_to.isoformat()).all())
        gone = {(r.lesson_id, r.lesson_date) for r in self.db.query(GroupAttendance.lesson_id, GroupAttendance.lesson_date).filter(
            GroupAttendance.group_id == group.id, GroupAttendance.status == CANCELLED, GroupAttendance.lesson_date >= today.isoformat())}
        for asg in asgs:
            day = date.fromisoformat(asg.lesson_date)
            occ = self._occurrence(group, asg.lesson_id, day)
            if occ is None or (asg.lesson_id, asg.lesson_date) in gone:          # a cancelled lesson is nobody's time (R3 10)
                continue
            by_teacher.setdefault(asg.teacher_id, []).append(BusyItem(asg.lesson_date, hhmm_to_minutes(occ["startTime"]), hhmm_to_minutes(occ["endTime"]), occ["startTime"],
                                                                      occ["endTime"], str(group.id), group.name, asg.lesson_id))
        for teacher_id in sorted(by_teacher, key=str):
            cands = by_teacher[teacher_id]
            conflicts = self.teacher_conflicts(org_id, teacher_id, cands, ignore={c.key for c in cands}, book=book)
            if conflicts:
                self.raise_if_busy(conflicts, short_name(self.db.query(User).filter(User.id == teacher_id).first()))

    # ----------------------------------------------------------------------------- substitution cleaning
    def cancel_assignment(self, asg: LessonTeacherAssignment, actor_id: Any, *, group: Group, reason: str, trigger: str = "release", extra: Optional[Dict[str, Any]] = None) -> None:
        asg.cancelled_at = bc.utc_now()
        asg.cancelled_by = _uuid(actor_id)
        self.db.flush()
        from . import lesson_substitutions  # lazy: it imports this module

        lesson_substitutions.on_assignment_cancelled(self.svc, asg, actor_id, trigger, extra)  # LOGIC-B2: a request substituted through it gets ``assignment_cleared``
        self.db.add(
            GroupAudit(
                org_id=group.org_id,
                group_id=group.id,
                actor_id=_uuid(actor_id),
                actor_role="admin",
                action="lesson_teacher_cleared",
                assigned_teacher_id=group.teacher_id,
                snapshot={"lessonId": asg.lesson_id, "lessonDate": asg.lesson_date, "teacherId": [_s(asg.teacher_id), None],
                          "reason": reason},
                created_at=bc.utc_now(),
            )
        )

    def release_equal_to_group_teacher(self, group: Group, actor_id: Any) -> int:
        """After a change of the group teacher: the future substitutions that now name the group teacher are taken off (audit)."""
        if group.teacher_id is None:
            return 0
        rows = (
            self.db.query(LessonTeacherAssignment)
            .filter(
                LessonTeacherAssignment.group_id == group.id,
                LessonTeacherAssignment.cancelled_at.is_(None),
                LessonTeacherAssignment.teacher_id == group.teacher_id,
                LessonTeacherAssignment.lesson_date >= bc.business_today().isoformat(),
            )
            .all()
        )
        rows = self._not_started([(asg, group) for asg in rows])
        for asg, _g in rows:
            self.cancel_assignment(asg, actor_id, group=group, reason="Преподаватель группы сменён: замена совпала с ним",
                                   trigger="group_teacher_changed")
        return len(rows)

    def _not_started(self, pairs: List[Tuple[LessonTeacherAssignment, Group]]) -> List[Tuple[LessonTeacherAssignment, Group]]:
        """W1 S22 (D30): an assignment on a lesson that has already begun (a journal row ``in_progress`` / ``completed``) is a fact - who taught it - and is never taken off by a release."""
        if not pairs:
            return pairs
        started = {(r.group_id, r.lesson_id, r.lesson_date) for r in self.db.query(GroupAttendance.group_id, GroupAttendance.lesson_id, GroupAttendance.lesson_date).filter(
            GroupAttendance.group_id.in_({a.group_id for a, _ in pairs}), GroupAttendance.lesson_date.in_({a.lesson_date for a, _ in pairs}),
            GroupAttendance.status.in_((IN_PROGRESS, COMPLETED)))}
        return [(a, g) for a, g in pairs if (a.group_id, a.lesson_id, a.lesson_date) not in started]

    def release_substitutions_of(self, org_id: Any, teacher_id: UUID, actor_id: Any, *, reason: str = REASON_REMOVED, trigger: str = "member_removed") -> int:
        """A removed teacher's (or one who stopped being a teacher) future substitutions are taken off (the lessons return to the group teachers by date).
        ``reason`` / ``trigger`` say why: «Сотрудник удалён» / «Роль изменена».  ``self.last_released`` keeps ``[(assignment, group)]`` for LOGIC-B2 (a substitution
        decided from a request is reopened as a new request)."""
        rows = (
            self.db.query(LessonTeacherAssignment, Group)
            .join(Group, Group.id == LessonTeacherAssignment.group_id)
            .filter(
                LessonTeacherAssignment.org_id == org_id,
                LessonTeacherAssignment.teacher_id == teacher_id,
                LessonTeacherAssignment.cancelled_at.is_(None),
                LessonTeacherAssignment.lesson_date >= bc.business_today().isoformat(),
            )
            .all()
        )
        rows = self._not_started(list(rows))
        self.last_released = list(rows)
        for asg, group in rows:
            self.cancel_assignment(asg, actor_id, group=group, reason=reason, trigger=trigger)
        return len(rows)

    def count_future_substitutions(self, org_id: Any, teacher_id: UUID) -> int:
        return (
            self.db.query(func.count(LessonTeacherAssignment.id))
            .filter(
                LessonTeacherAssignment.org_id == org_id,
                LessonTeacherAssignment.teacher_id == teacher_id,
                LessonTeacherAssignment.cancelled_at.is_(None),
                LessonTeacherAssignment.lesson_date >= bc.business_today().isoformat(),
            )
            .scalar()
            or 0
        )

    # ----------------------------------------------------------------------------------------------- PUT
    def _occurrence(self, group: Group, lesson_id: str, day: date) -> Optional[Dict[str, Any]]:
        return next(
            (o for o in self.svc._resolved_occurrences_for_group(group, date_from=day, date_to=day) if o["id"] == lesson_id), None
        )

    def put(self, org_id: Any, group_id: Any, req, *, actor_id: Any, actor_role: str = "admin") -> Dict[str, Any]:
        """``PUT /org/groups/{id}/lesson-teacher`` (SPEC 4.3).  Manager only (checked by the caller)."""
        # locks: group -> attendance row -> request row -> assignments
        group = (
            self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).with_for_update().populate_existing().first()
        )
        if not group:
            raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
        return self._put_locked(org_id, group, req, actor_id=actor_id, actor_role=actor_role, commit=True)

    def _put_locked(self, org_id: Any, group: Group, req, *, actor_id: Any, actor_role: str = "admin", commit: bool = True,
                    skip_request_id: Any = None) -> Dict[str, Any]:
        """The body of ``put`` for a caller that already holds the group lock (LOGIC-B2: a decision of a substitution request runs it in its own transaction,
        ``commit=False``; ``skip_request_id`` is the request being decided, so the hook does not close it a second time)."""
        self.last_assignment = None
        today = bc.business_today()
        day = req.lessonDate
        if day < bc.month_window_start(today):
            raise unprocessable("LESSON_OUTSIDE_WINDOW", "Урок старше окна незакрытых уроков: сменить преподавателя нельзя")
        if day > today + timedelta(days=LESSON_DATE_MAX_AHEAD_DAYS):
            raise not_found("LESSON_NOT_FOUND", "Урок не найден")
        lesson_date = day.isoformat()
        row = (
            self.db.query(GroupAttendance)
            .filter(GroupAttendance.group_id == group.id, GroupAttendance.lesson_id == req.lessonId,
                    GroupAttendance.lesson_date == lesson_date)
            .with_for_update()
            .first()
        )
        occ = self._occurrence(group, req.lessonId, day)
        if occ is None and row is None:
            raise not_found("LESSON_NOT_FOUND", "Урок не найден в расписании группы")
        if row is not None and row.status == CANCELLED:
            raise conflict("LESSON_CANCELLED", "Урок отменён: преподавателя менять нельзя")
        if row is not None and row.status == IN_PROGRESS:              # W1 S7 (D20): a lesson that is going on keeps its teacher; finish it first, then «назначить того, кто провёл»
            raise conflict("LESSON_IN_PROGRESS", "Урок идёт: сначала завершите его, затем назначьте того, кто провёл")
        end_minutes = hhmm_to_minutes(occ["endTime"]) if occ and occ.get("endTime") else None
        book = TeacherBook(self.db)
        res = book.resolve(group, req.lessonId, lesson_date, end_minutes)
        current = effective_teacher(row, res)
        default = res.default_teacher_id
        if req.expectedTeacherId is not None and _uuid(req.expectedTeacherId) != current:
            names = self.users_by_id([current])
            who = short_name(names.get(_s(current))) or "не назначен"
            raise conflict(
                "LESSON_TEACHER_CHANGED",
                f"Преподаватель урока уже изменён: {who}" + ("" if who.endswith(".") else ".") + " Обновите страницу",
                currentTeacherId=_s(current),
                teacher_name=short_name(names.get(_s(current))),
            )
        target = _uuid(req.teacherId)
        if req.teacherId is not None and target is None:
            raise unprocessable("TARGET_NOT_TEACHER", "Выберите преподавателя центра")
        clearing = target is None or (default is not None and target == default)
        want = default if clearing else target
        asg = book.assignment(group.id, req.lessonId, lesson_date)
        row_teacher = row.teacher_id if row is not None else None
        asg_state_ok = (asg is None) if clearing else (asg is not None and asg.teacher_id == target)
        row_state_ok = row is None or row_teacher == want
        if asg_state_ok and row_state_ok:
            return self._out(group, req.lessonId, lesson_date, current if row is None else row.teacher_id, default, row, version_changed=False,
                             sheets=[])
        if not clearing:
            # the target's row first: a removal of him serialises with this assignment.  FOR NO KEY UPDATE (as the move does): the record of the journal that follows takes
            # FOR KEY SHARE on the MAIN teacher's row, and two crossed assignments (X takes D's lesson, D takes X's) must not wait for each other
            self.db.query(User.id).filter(User.id == target).with_for_update(key_share=True).first()
            member = self.teaching_member(org_id, target)
            if member is None:
                raise unprocessable("TARGET_NOT_TEACHER", "Выберите преподавателя центра")
            if occ is not None:
                cand = BusyItem(lesson_date, hhmm_to_minutes(occ["startTime"]), hhmm_to_minutes(occ["endTime"]), occ["startTime"],
                                occ["endTime"], str(group.id), group.name, req.lessonId)
                conflicts = self.teacher_conflicts(org_id, target, [cand], ignore={cand.key}, book=book)
                self.raise_if_busy(conflicts, short_name(member[0]))
        sheets: List[Dict[str, Any]] = []
        old_for_row = row_teacher if row is not None else current
        if row is not None and row.status == COMPLETED and want != row_teacher:
            sheets = payroll_hooks.sheets_touched_by_lesson_teacher_change(self.db, org_id, lesson_date, row_teacher, want, owner=actor_role == "owner")
        now = bc.utc_now()
        actor = _uuid(actor_id)
        if row is not None and row_teacher != want:
            row.teacher_id = want
            row.version = (row.version or 1) + 1
            self.db.add(
                GroupAttendanceAudit(
                    attendance_id=row.id, org_id=org_id, group_id=group.id, actor_id=actor, action="teacher_changed",
                    before_status=row.status, after_status=row.status, before_records=dict(row.records or {}),
                    after_records=dict(row.records or {}),
                    details={"teacher_id": [_s(row_teacher), _s(want)], "reason": req.reason, "lesson_date": lesson_date},
                    created_at=now,
                )
            )
        cancelled_asg = None
        if asg is not None and not (not clearing and asg.teacher_id == target):
            asg.cancelled_at = now
            asg.cancelled_by = actor
            self.db.flush()
            book.forget(group.id, req.lessonId, lesson_date)
            cancelled_asg = asg
        new_asg = asg if (asg is not None and cancelled_asg is None) else None
        assigned = not clearing and not (asg is not None and asg.teacher_id == target)         # a substitute set by THIS call (not one that was already there)
        if assigned:
            new_asg = LessonTeacherAssignment(
                org_id=org_id, group_id=group.id, lesson_id=req.lessonId, lesson_date=lesson_date, teacher_id=target,
                reason=req.reason, set_by=actor, created_at=now,
            )
            self.db.add(new_asg)
        self.db.add(
            GroupAudit(
                org_id=org_id, group_id=group.id, actor_id=actor, actor_role=actor_role,
                action="lesson_teacher_cleared" if clearing else "lesson_teacher_set", assigned_teacher_id=group.teacher_id,
                snapshot={"lessonId": req.lessonId, "lessonDate": lesson_date, "teacherId": [_s(old_for_row), _s(want)],
                          "reason": req.reason},
                created_at=now,
            )
        )
        self.db.flush()
        out = self._out(group, req.lessonId, lesson_date, want, default, row, version_changed=row is not None and row_teacher != want,
                        sheets=sheets, force_teacher=want)
        self.last_assignment = new_asg
        from . import lesson_substitutions  # lazy: it imports this module

        out["requestClosed"] = lesson_substitutions.on_lesson_teacher_changed(
            self.svc, group, req.lessonId, lesson_date, cancelled_asg=cancelled_asg, new_asg=new_asg, assigned=assigned, want=want, default=default, occ=occ,
            reason=req.reason, actor_id=actor, skip_request_id=skip_request_id,
        )
        if commit:
            self.db.commit()
        return out

    def _out(self, group: Group, lesson_id: str, lesson_date: str, teacher: Optional[UUID], default: Optional[UUID],
             row: Optional[GroupAttendance], *, version_changed: bool, sheets: List[Dict[str, Any]],
             force_teacher: Optional[UUID] = None) -> Dict[str, Any]:
        shown = force_teacher if force_teacher is not None or version_changed else teacher
        if shown is None and teacher is not None:
            shown = teacher
        names = self.users_by_id([shown, default])
        return {
            "groupId": str(group.id),
            "lessonId": lesson_id,
            "lessonDate": lesson_date,
            "teacherId": _s(shown),
            "teacherName": short_name(names.get(_s(shown))) if shown else None,
            "isSubstitution": bool(shown is not None and default is not None and shown != default),          # a group without a main teacher has nobody to replace
            "defaultTeacherId": _s(default),
            "defaultTeacherName": short_name(names.get(_s(default))) if default else None,
            "attendanceUpdated": bool(version_changed),
            "version": (row.version if row is not None and version_changed else None),
            "payrollSheetsAffected": sheets,
        }

    # -------------------------------------------------------------------------------------- candidates
    def candidates(self, org_id: Any, group_id: Any, lesson_id: str, lesson_date: str, *, is_owner: bool = False) -> Dict[str, Any]:
        day = date.fromisoformat(lesson_date)
        group = self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id).first()
        if not group:
            raise not_found("GROUP_NOT_FOUND", "Группа не найдена")
        occ = self._occurrence(group, lesson_id, day)
        if occ is None:
            raise not_found("LESSON_NOT_FOUND", "Урок не найден в расписании группы")
        start, end = hhmm_to_minutes(occ["startTime"]), hhmm_to_minutes(occ["endTime"])
        book = TeacherBook(self.db)
        res = book.resolve(group, lesson_id, lesson_date, end)
        row = self.db.query(GroupAttendance).filter(
            GroupAttendance.group_id == group.id, GroupAttendance.lesson_id == lesson_id, GroupAttendance.lesson_date == lesson_date
        ).first()
        current = effective_teacher(row, res)
        cand = BusyItem(lesson_date, start, end, occ["startTime"], occ["endTime"], str(group.id), group.name, lesson_id)
        out: List[Dict[str, Any]] = []
        everybody = self.busy_map(org_id, day, day, exclude_keys={cand.key}, book=book)      # one pass for all the teachers
        roles = dict(self.db.query(OrganizationMember.user_id, OrganizationMember.org_role).filter(OrganizationMember.org_id == org_id).all())
        staff = self.staff_who_can_teach(org_id)
        no_rate = self.rates_missing(org_id, [u.id for u in staff], self.lesson_type_on(group, lesson_date), lesson_date)
        from .payroll_estimate import substitute_pay_estimates  # lazy: it imports the payments and payroll services

        pay = substitute_pay_estimates(self.svc, org_id, group, lesson_id, lesson_date, start, [u.id for u in staff], is_owner=is_owner)
        for user in staff:
            pairs = self.overlap_pairs([cand], everybody.get(user.id, []))
            other = pairs[0][1] if pairs else None
            out.append(
                {
                    "teacherId": str(user.id),
                    "name": short_name(user) or user.email,
                    "orgRole": roles.get(user.id, "teacher"),
                    "rateMissing": user.id in no_rate,
                    "substitutePayEstimate": pay[user.id]["amount"],
                    "substitutePayEstimateReason": pay[user.id]["reason"],
                    "busy": other is not None,
                    "busyWith": {"groupName": other.group_name, "startTime": other.startTime, "endTime": other.endTime}
                    if other is not None
                    else None,
                }
            )
        return {
            "lessonId": lesson_id,
            "lessonDate": lesson_date,
            "startTime": occ["startTime"],
            "endTime": occ["endTime"],
            "currentTeacherId": _s(current),
            "defaultTeacherId": _s(res.default_teacher_id),
            "candidates": out,
        }
