"""
ALGO - Rates service (UX-2): the page of the percents of the teachers, set per kind of lessons from a month.  Owner only.

* ``GET`` - the teachers of the centre with the rate in force for a month (and the nearest future change);
* ``PUT`` - appends rows (one per kind) valid from a month; nothing is edited, a correction of the same month is a
  newer row and wins (``payroll_rates.RateBook``);
* ``history`` - every row of a teacher, newest first.

A saved sheet is a snapshot and does not move; it turns "данные изменились" when the rate of ITS month differs from
the percent it was calculated with (the payroll service compares, see ``payroll_service._stale_by_rates``).
Logs carry ids and the actor, never percents of other people's money.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Set
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import Group, GroupAttendance, OrganizationMember, PayrollRate, User

from . import payment_status as ps
from . import payroll_engine as engine
from . import payroll_rates
from .payments_files import staff_name
from .payroll_schemas import (
    MAX_NOTE,
    RateHistoryRow,
    RateNextOut,
    RateOut,
    RatePut,
    RatesResponse,
    TeacherRates,
)
from .payroll_service import FIRST_YEAR, MONTH_RE, PayrollInvalid, PayrollNotFound, _iso, _utcnow, month_label

logger = logging.getLogger(__name__)

MAX_YEARS_AHEAD = 5


def _month_start(text: Any, *, what: str = "Месяц") -> date:
    """``"2026-09"`` -> 2026-09-01.  Any month from 2020 to a few years ahead (a rate may be set in advance)."""
    match = MONTH_RE.match(text if isinstance(text, str) else "")
    if not match:
        raise PayrollInvalid(f"{what} указывается в формате ГГГГ-ММ, например 2026-09")
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12 or year < FIRST_YEAR or year > ps.business_today().year + MAX_YEARS_AHEAD:
        raise PayrollInvalid(f"{what}: некорректный месяц")
    return date(year, month, 1)


def _month_end(first: date) -> date:
    return date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])


class RatesService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------ helpers

    def _member_ids(self, org_id: str) -> Dict[UUID, tuple]:
        return {
            uid: (role, bool(active))
            for uid, role, active in self.db.query(
                OrganizationMember.user_id, OrganizationMember.org_role, OrganizationMember.is_active
            ).filter(OrganizationMember.org_id == org_id)
        }

    def _require_teacher(self, org_id: str, teacher_id: UUID) -> User:
        user = (
            self.db.query(User)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .filter(
                OrganizationMember.org_id == org_id,
                OrganizationMember.user_id == teacher_id,
                # any role that can teach (owners and admins do); a DEACTIVATED member is accepted on purpose: a teacher
                # let go in October is still paid for September
                OrganizationMember.org_role.in_(("teacher", "owner", "admin")),
            )
            .first()
        )
        if user is None:
            raise PayrollNotFound("Преподаватель не найден")
        return user

    def _names(self, ids: Iterable[Optional[UUID]]) -> Dict[UUID, Optional[str]]:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        return {
            u.id: staff_name(u.first_name, u.last_name) for u in self.db.query(User).filter(User.id.in_(wanted)).all()
        }

    def _taught(self, org_id: str, first: date, last: date) -> Dict[UUID, Set[str]]:
        """Kinds of lessons per teacher: the ones of the active groups he leads now plus the ones of the lessons he
        held in the month (a substitute teacher is paid too) - what the month overview calls the kinds of a teacher."""
        out: Dict[UUID, Set[str]] = {}
        for teacher_id, lesson_type in (
            self.db.query(Group.teacher_id, Group.lesson_type)
            .filter(Group.org_id == org_id, Group.status == "active", Group.teacher_id.isnot(None))
            .distinct()
        ):
            out.setdefault(teacher_id, set()).add(lesson_type or "group")
        for teacher_id, lesson_type in (
            self.db.query(GroupAttendance.teacher_id, Group.lesson_type)
            .join(Group, Group.id == GroupAttendance.group_id)
            .filter(
                GroupAttendance.org_id == org_id,
                GroupAttendance.status == "completed",
                GroupAttendance.teacher_id.isnot(None),
                GroupAttendance.lesson_date >= first.isoformat(),
                GroupAttendance.lesson_date <= last.isoformat(),
            )
            .distinct()
        ):
            out.setdefault(teacher_id, set()).add(lesson_type or "group")
        return out

    @staticmethod
    def _item(
        teacher_id: UUID,
        name: Optional[str],
        taught: Set[str],
        book: payroll_rates.RateBook,
        first: date,
        names: Dict[UUID, Optional[str]],
    ) -> TeacherRates:
        kinds = [
            t
            for t in engine.LESSON_TYPES
            if t in taught or book.at(teacher_id, t, first) is not None or book.after(teacher_id, t, first) is not None
        ]
        rates: Dict[str, Optional[RateOut]] = {}
        upcoming: Dict[str, RateNextOut] = {}
        for lesson_type in kinds:
            row = book.at(teacher_id, lesson_type, first)
            rates[lesson_type] = (
                RateOut(
                    percent=engine.format_percent(row.percent),
                    valid_from=row.valid_from.isoformat(),
                    set_by_name=names.get(row.created_by),
                    set_at=_iso(row.created_at),
                )
                if row is not None
                else None
            )
            later = book.after(teacher_id, lesson_type, first)
            if later is not None:
                upcoming[lesson_type] = RateNextOut(
                    percent=engine.format_percent(later.percent), valid_from=later.valid_from.isoformat()
                )
        return TeacherRates(
            teacher_id=str(teacher_id), teacher_name=name, lesson_types=kinds, rates=rates, next=upcoming
        )

    # ---------------------------------------------------------------- GET

    def list_rates(self, org_id: str, month: Optional[str]) -> RatesResponse:
        first = _month_start(month) if month else ps.business_today().replace(day=1)
        last = _month_end(first)
        members = self._member_ids(org_id)
        taught = self._taught(org_id, first, last)
        candidates = {uid for uid, (role, active) in members.items() if role == "teacher" and active}
        candidates |= {uid for uid in taught if uid in members}
        book = payroll_rates.load_book(self.db, org_id, candidates)
        users = {
            u.id: u for u in self.db.query(User).filter(User.id.in_(candidates)).all()
        } if candidates else {}
        names = self._names([r.created_by for r in book.rows()])
        items = [
            self._item(
                uid, staff_name(users[uid].first_name, users[uid].last_name), taught.get(uid, set()), book, first, names
            )
            for uid in candidates
            if uid in users
        ]
        items.sort(key=lambda i: ((i.teacher_name or "").casefold(), i.teacher_id))
        return RatesResponse(month=month_label(first), items=items)

    # ---------------------------------------------------------------- PUT

    def put_rates(self, org_id: str, actor_id: str, teacher_id: UUID, body: RatePut) -> TeacherRates:
        teacher = self._require_teacher(org_id, teacher_id)
        valid_from = _month_start(body.valid_from, what="«Действует с»")
        percents = body.percents
        if not isinstance(percents, dict) or not percents:
            raise PayrollInvalid("Укажите ставку хотя бы для одного вида занятий")
        parsed = {}
        for lesson_type, value in percents.items():
            if lesson_type not in engine.LESSON_TYPES:
                raise PayrollInvalid(f"Неизвестный вид занятий: {str(lesson_type)[:40]}")
            try:
                parsed[lesson_type] = engine.parse_percent(value)
            except engine.PercentError as exc:
                raise PayrollInvalid(f"{engine.LESSON_TYPE_LABELS[lesson_type]}: {exc}") from exc
        note = body.note
        if note is not None and not isinstance(note, str):
            raise PayrollInvalid("Примечание должно быть текстом")
        note = (note or "").strip() or None
        if note and len(note) > MAX_NOTE:
            raise PayrollInvalid(f"Примечание — не длиннее {MAX_NOTE} знаков")
        now, actor = _utcnow(), UUID(str(actor_id))
        for lesson_type in engine.LESSON_TYPES:  # a fixed order: the rows of one request get increasing times
            if lesson_type in parsed:
                self.db.add(
                    PayrollRate(
                        org_id=UUID(str(org_id)),
                        teacher_id=teacher_id,
                        lesson_type=lesson_type,
                        percent=parsed[lesson_type],
                        valid_from=valid_from,
                        note=note,
                        created_by=actor,
                        created_at=now,
                    )
                )
        self.db.commit()
        logger.info(
            "payroll rates set org=%s teacher=%s from=%s kinds=%s actor=%s",
            org_id, teacher_id, valid_from.isoformat(), ",".join(sorted(parsed)), actor_id,
        )
        first = ps.business_today().replace(day=1)
        book = payroll_rates.load_book(self.db, org_id, [teacher_id])
        names = self._names([r.created_by for r in book.rows()])
        taught = self._taught(org_id, first, _month_end(first)).get(teacher_id, set())
        return self._item(teacher_id, staff_name(teacher.first_name, teacher.last_name), taught, book, first, names)

    # ------------------------------------------------------------ history

    def history(self, org_id: str, teacher_id: UUID) -> List[RateHistoryRow]:
        self._require_teacher(org_id, teacher_id)
        rows = (
            self.db.query(PayrollRate)
            .filter(PayrollRate.org_id == org_id, PayrollRate.teacher_id == teacher_id)
            .order_by(PayrollRate.created_at.desc(), PayrollRate.valid_from.desc(), PayrollRate.lesson_type)
            .all()
        )
        names = self._names(r.created_by for r in rows)
        return [
            RateHistoryRow(
                lesson_type=r.lesson_type,
                percent=engine.format_percent(r.percent),
                valid_from=r.valid_from.isoformat(),
                set_by_name=names.get(r.created_by),
                set_at=_iso(r.created_at),
                note=r.note,
            )
            for r in rows
        ]
