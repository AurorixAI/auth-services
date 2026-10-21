"""
ALGO - LOGIC-A (F14, S-9): the type of lessons of a group BY DAY (as prices and rates are).

``group_lesson_type_history(group_id, valid_from, lesson_type)``: the type of a lesson is the row with the greatest ``valid_from <=`` the
day of the lesson (a group without rows: ``groups.lesson_type``).  ``groups.lesson_type`` mirrors the type of today.  A change in the
middle of a month splits the payroll line of a student in two (each with the percent of its type).
"""
from __future__ import annotations

import logging
from bisect import bisect_right
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import Group, GroupAudit, GroupLessonTypeHistory, User

from . import payment_status as ps
from .payments_schemas import GroupLessonTypeChanged, GroupLessonTypes, LessonTypeImpact, LessonTypePut, LessonTypeRow, LessonTypeSheet
from .payments_service import PaymentConflict, PaymentError, PaymentInvalid, PaymentNotFound, _person

logger = logging.getLogger(__name__)

LESSON_TYPES = ("group", "mini_group", "pair", "individual")
SEED_DAY = date(2020, 1, 1)
AUDIT_LESSON_TYPE_SET = "group_lesson_type_set"


class LessonTypeRange(PaymentInvalid):
    code = "LESSON_TYPE_RANGE"


class LessonTypeBook:
    """The history of the types of some groups, read once; ``type_at`` is a bisect."""

    def __init__(self, rows: Dict[UUID, List[Tuple[date, str]]], fallback: Dict[UUID, str]) -> None:
        self._days = {gid: [d for d, _ in sorted(items)] for gid, items in rows.items()}
        self._types = {gid: [t for _, t in sorted(items)] for gid, items in rows.items()}
        self._fallback = fallback

    def type_at(self, group_id: UUID, day: date) -> str:
        days = self._days.get(group_id)
        if not days:
            return self._fallback.get(group_id) or "group"
        i = bisect_right(days, day)
        return self._types[group_id][i - 1] if i else self._types[group_id][0]

    def with_group(self, group_id: UUID, rows: List[Tuple[date, str]]) -> "LessonTypeBook":
        """The same book with the history of one group replaced ("what if", the impact of a change)."""
        merged = {gid: list(zip(self._days[gid], self._types[gid])) for gid in self._days}
        merged[group_id] = sorted(rows)
        return LessonTypeBook(merged, self._fallback)


def load_book(db: Session, groups: Iterable[Group]) -> LessonTypeBook:
    groups = list(groups)
    ids = [g.id for g in groups]
    rows: Dict[UUID, List[Tuple[date, str]]] = {}
    if ids:
        for gid, valid_from, lesson_type in db.query(
            GroupLessonTypeHistory.group_id, GroupLessonTypeHistory.valid_from, GroupLessonTypeHistory.lesson_type
        ).filter(GroupLessonTypeHistory.group_id.in_(ids)):
            rows.setdefault(gid, []).append((valid_from, lesson_type))
    return LessonTypeBook(rows, {g.id: g.lesson_type or "group" for g in groups})


def history_of(db: Session, group_id: UUID) -> List[GroupLessonTypeHistory]:
    return (db.query(GroupLessonTypeHistory).filter(GroupLessonTypeHistory.group_id == group_id)
            .order_by(GroupLessonTypeHistory.valid_from).all())


def type_today(db: Session, group: Group, today: date) -> str:
    rows = [r for r in history_of(db, group.id) if r.valid_from <= today]
    return rows[-1].lesson_type if rows else (group.lesson_type or "group")


class LessonTypeService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _group(self, org_id: str, group_id: UUID, *, lock: bool = False) -> Group:
        query = self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id)
        group = (query.with_for_update() if lock else query).first()
        if group is None:
            raise PaymentNotFound("Группа не найдена")
        return group

    def _out(self, group: Group, today: date, *, impact: Optional[LessonTypeImpact] = None, dry_run: bool = False) -> GroupLessonTypeChanged:
        rows = history_of(self.db, group.id)
        users = {u.id: u for u in self.db.query(User).filter(User.id.in_({r.created_by for r in rows if r.created_by})).all()} if rows else {}
        history = [LessonTypeRow(valid_from=r.valid_from, lesson_type=r.lesson_type, created_by_name=_person(users.get(r.created_by)))
                   for r in rows]
        return GroupLessonTypeChanged(current=type_today(self.db, group, today), history=history, dry_run=dry_run, impact=impact)

    def get(self, org_id: str, group_id: UUID) -> GroupLessonTypes:
        group = self._group(org_id, group_id)
        out = self._out(group, ps.business_today())
        return GroupLessonTypes(current=out.current, history=out.history)

    @staticmethod
    def _check_range(valid_from: date, today: date) -> None:
        lowest = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        if valid_from > today or valid_from < lowest:
            raise LessonTypeRange("Тип занятий можно менять с начала прошлого месяца по сегодня")

    def _write(self, org_id, group: Group, rows, valid_from: date, lesson_type: str, was: str, actor_id: str, actor_role: str, today: date) -> None:
        """One history row (valid_from, type), the mirror ``groups.lesson_type`` and the audit row - in the caller's transaction (no commit)."""
        existing = next((r for r in rows if r.valid_from == valid_from), None)
        if existing is not None:
            existing.lesson_type, existing.created_by = lesson_type, UUID(str(actor_id))
        else:
            self.db.add(GroupLessonTypeHistory(group_id=group.id, valid_from=valid_from, lesson_type=lesson_type, created_by=UUID(str(actor_id))))
        self.db.flush()
        group.lesson_type = type_today(self.db, group, today)       # the mirror
        self.db.add(GroupAudit(
            org_id=UUID(str(org_id)), group_id=group.id, actor_id=UUID(str(actor_id)),
            actor_role=actor_role if actor_role in ("owner", "admin", "teacher", "superadmin") else "admin",
            action=AUDIT_LESSON_TYPE_SET, snapshot={
                "actor_id": str(actor_id), "group_id": str(group.id), "lesson_type": lesson_type,
                "valid_from": valid_from.isoformat(), "was": was}))

    def patch_type(self, org_id: str, group: Group, lesson_type: str, actor_id: str, actor_role: str = "admin") -> bool:
        """``PATCH /org/groups/{id}`` with a new ``lesson_type`` (B1): the change is a history row effective the business TODAY (other dates:
        only ``PUT …/lesson-type``), written in the caller's transaction (no commit) together with the audit row.  The type of the
        history is the one that counts - a bare ``groups.lesson_type`` write would be ignored by payroll.  Returns whether a row was written."""
        today = ps.business_today()
        self.db.refresh(group, with_for_update=True)                  # the same lock as ``put``
        rows = history_of(self.db, group.id)
        if not rows:  # a group made after the migration: its history starts with its own (old) type
            self.db.add(GroupLessonTypeHistory(group_id=group.id, valid_from=SEED_DAY, lesson_type=group.lesson_type or "group"))
            self.db.flush()
            rows = history_of(self.db, group.id)
        effective = next((r for r in reversed(rows) if r.valid_from <= today), rows[0])
        if effective.lesson_type == lesson_type:
            group.lesson_type = lesson_type
            return False
        self._write(org_id, group, rows, today, lesson_type, effective.lesson_type, actor_id, actor_role, today)
        return True

    def put(
        self, org_id: str, group_id: UUID, actor_id: str, body: LessonTypePut, *, dry_run: bool = False, owner: bool = False,
        actor_role: str = "admin",
    ) -> GroupLessonTypeChanged:
        from .payroll_service import PayrollService

        today = ps.business_today()
        try:
            valid_from = body.valid_from or today
            self._check_range(valid_from, today)
            group = self._group(org_id, group_id, lock=True)
            rows = history_of(self.db, group.id)
            if not rows:  # a group made after the migration: its history starts with its own type
                self.db.add(GroupLessonTypeHistory(group_id=group.id, valid_from=SEED_DAY, lesson_type=group.lesson_type or "group"))
                self.db.flush()
                rows = history_of(self.db, group.id)
            effective = next((r for r in reversed(rows) if r.valid_from <= valid_from), rows[0])
            if effective.lesson_type == body.lesson_type:
                self.db.rollback()
                return self._out(group, today, dry_run=dry_run)                      # R: the same type from the same day - nothing
            before_rows = [(r.valid_from, r.lesson_type) for r in rows]
            after_rows = [(d, t) for d, t in before_rows if d != valid_from] + [(valid_from, body.lesson_type)]
            impact = None
            if owner:
                changed = PayrollService(self.db).sheets_changed_by_lesson_type(org_id, group, after_rows, valid_from)
                impact = LessonTypeImpact(stale_payroll_sheets=len(changed), stale_sheets=[
                    LessonTypeSheet(sheet_id=c["sheet_id"], month=c["month"], teacher_name=c["teacher_name"]) for c in changed])
            self._write(org_id, group, rows, valid_from, body.lesson_type, effective.lesson_type, actor_id, actor_role, today)
            out = self._out(group, today, impact=impact, dry_run=dry_run)
            if dry_run:
                self.db.rollback()
                return out
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        logger.info("lesson type set group=%s actor=%s", group_id, actor_id)
        return self._out(self._group(org_id, group_id), today, impact=impact)
