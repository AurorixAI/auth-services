"""
ALGO - Student payments (N13 P2).

Money lives only here and in ``payments_router.py``; no student, group or
profile payload carries an amount.  Status maths is ``payment_status.py``
(pure); this module loads the data in a bounded number of queries and writes.

* Nothing is ever deleted: ``void`` cancels, ``replace`` = void + insert in one
  transaction under ``SELECT ... FOR UPDATE``.
* The status is computed on every request from the live (not voided) payments;
  nothing is stored and no background job is needed.
* Logs carry ids and the actor, never amounts or notes.
* Carry-over (P2.1, "the payment moves with the student"): a transfer copies the
  payments still in force of the group being left into the new group as amount-0
  payments linked to the original (``carried_from_payment_id``) and stamps the
  original with ``carried_out_at`` = the transfer day T.  A stamped original covers
  only what lies strictly before T in its own membership (``payment_status``), so
  the coverage is never counted twice - also not when the student comes back, which
  is just one more transfer that carries the live payments of the group being left.
  Carry-overs are ordinary live payments for the status, the excuse deadline and
  (later) payroll.  Voiding the original voids its carry-overs; voiding a carry-over
  gives the source its coverage back (``_release_source``); replacing the original
  rebuilds the carry-overs from the replacement and keeps the stamp
  (``_rebuild_carry_overs``).  A carry-over cannot be replaced (409): correct the payment it
  was made from; it can be cancelled.
* A carry-over's ``lessons_count`` is only the remainder at transfer time; its capacity is
  derived live along the chain (``_lessons_state`` / ``payment_status.resolve_lesson_chains``).
* Locks, in this order everywhere: student -> membership(s) -> payment(s), a source payment
  before its carry-overs; membership locks are FOR NO KEY UPDATE so that inserting a
  carry-over (foreign key: FOR KEY SHARE) into a membership someone else holds does not wait.  A deadlock PostgreSQL still finds is a 409 (``deadlock_guard``).
"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from src.models.auth import (
    AttendanceExcuse,
    Group,
    GroupAttendance,
    Organization,
    Student,
    StudentGroup,
    StudentPayment,
    User,
)

from . import payment_status as ps
from .payments_schemas import (
    CarriedFromOut,
    CarriedOutOut,
    MembershipPaymentRow,
    PaymentCreate,
    PaymentCreated,
    PaymentOut,
    PaymentSettings,
    PaymentSettingsUpdate,
    PaymentsSummary,
    PaymentStatusPage,
    PaymentVoided,
    PaymentWarningOut,
)

logger = logging.getLogger(__name__)

DEFAULT_REPLACE_REASON = "Исправлено"
UNIQUE_DUP_INDEX = "uq_student_payments_active_dup"

# Carry-over on transfer (N13 P2.1).
CARRY_CODE = "payment_carried_over"
CARRY_NOTE = "Перенос оплаты из «{group}»"
CARRY_VOID_REASON = "Исходная оплата отменена"
CARRY_REPLACE_REASON = "Исходная оплата исправлена"
CARRY_GENERIC_MESSAGE = "Действующая оплата ученика перенесена в новую группу"
MAX_CARRY_CHAIN = 20


# ============================================================================
# Errors (mapped to HTTP by the router)
# ============================================================================


class PaymentError(Exception):
    status_code = 400
    code: Optional[str] = None

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    @property
    def detail(self) -> Any:
        return {"code": self.code, "message": self.message} if self.code else self.message


class PaymentNotFound(PaymentError):
    status_code = 404


class PaymentConflict(PaymentError):
    status_code = 409


class DuplicatePayment(PaymentConflict):
    code = "DUPLICATE_PAYMENT"


class AlreadyVoided(PaymentConflict):
    code = "ALREADY_VOIDED"


class PaymentInvalid(PaymentError):
    status_code = 422


class CarryOverNotReplaceable(PaymentConflict):
    code = "CARRY_OVER_NOT_REPLACEABLE"


class ConcurrentEdit(PaymentConflict):
    """A deadlock with another edit of the same student; nothing was written, repeat the request."""


CONCURRENT_EDIT_MESSAGE = "Операция совпала с другой правкой. Повторите."


def is_deadlock(exc: BaseException) -> bool:
    """PostgreSQL deadlock_detected (SQLSTATE 40P01)."""
    return isinstance(exc, OperationalError) and getattr(getattr(exc, "orig", None), "pgcode", None) == "40P01"


def deadlock_guard(method):
    """Locks are taken in one order (student -> membership -> payment, a source before its
    carry-overs); should PostgreSQL still find a deadlock, the caller gets 409, never 500."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except OperationalError as exc:
            if not is_deadlock(exc):
                raise
            self.db.rollback()
            logger.warning("deadlock in %s; the request was rolled back", method.__name__)
            raise ConcurrentEdit(CONCURRENT_EDIT_MESSAGE) from exc

    return wrapper


# ============================================================================
# Helpers
# ============================================================================


def _utcnow() -> datetime:
    """Naive UTC timestamp (the convention of the other audit-like columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _person(user: Optional[User]) -> Optional[str]:
    if not user:
        return None
    return f"{user.first_name} {user.last_name}".strip() or None


def _plural_lessons(count: int) -> str:
    last, last2 = count % 10, count % 100
    if last == 1 and last2 != 11:
        return "урок"
    if 2 <= last <= 4 and not 12 <= last2 <= 14:
        return "урока"
    return "уроков"


def _short_date(day: date, today: date) -> str:
    """``10.11`` within the current year, ``10.11.2027`` otherwise."""
    return day.strftime("%d.%m") if day.year == today.year else day.strftime("%d.%m.%Y")


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@dataclass
class _Links:
    """Carry links of a batch of payments, for the response."""

    sources: Dict[UUID, CarriedFromOut]  # original id -> {payment_id, group_name}
    targets: Dict[UUID, Optional[str]]  # stamped original id -> group its live carry-over lives in


@dataclass
class _LessonsState:
    """Lessons payments of a set of memberships (and their carry-over chains) after the FIFO."""

    records: Dict[UUID, ps.PaymentRecord]  # payment id -> record with the LIVE capacity as lessons_count
    dates: Dict[UUID, List[date]]  # membership id -> counted lesson dates
    left: Dict[UUID, int]  # payment id -> capacity - lessons it took (for one that moved: what travelled on)


@dataclass
class _Carry:
    """One payment of the group being left that moves to the new group."""

    original: StudentPayment
    group_name: str
    valid_from: date
    valid_to: Optional[date]
    lessons_count: Optional[int]


@dataclass
class _Entry:
    """One tracked membership with everything needed to render its row."""

    membership: StudentGroup
    student: Student
    group: Group
    teacher: Optional[User]
    live: List[StudentPayment]
    result: ps.StatusResult

    @property
    def student_name(self) -> str:
        return f"{self.student.last_name or ''} {self.student.first_name or ''}".strip()


# ============================================================================
# Service
# ============================================================================


class PaymentService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------ settings

    def _settings_dict(self, org_id: str) -> Dict[str, Any]:
        raw = self.db.query(Organization.settings).filter(Organization.id == org_id).scalar()
        block = raw.get("payments") if isinstance(raw, dict) else None
        return dict(block) if isinstance(block, dict) else {}

    def get_settings(self, org_id: str) -> PaymentSettings:
        block = self._settings_dict(org_id)
        fields = PaymentSettings.model_fields
        values = {k: block[k] for k in fields if k in block and block[k] is not None}
        try:
            return PaymentSettings(**values)
        except ValueError:  # a hand-edited, out-of-range value must not break the page
            logger.warning("Invalid payment settings stored for org %s; using defaults", org_id)
            return PaymentSettings()

    def update_settings(
        self, org_id: str, actor_id: str, patch: PaymentSettingsUpdate
    ) -> PaymentSettings:
        org = (
            self.db.query(Organization).filter(Organization.id == org_id).with_for_update().first()
        )
        if not org:
            raise PaymentNotFound("Организация не найдена")
        changes = patch.model_dump(exclude_none=True)
        block = dict(((org.settings or {}).get("payments")) or {})
        block.update(changes)
        block["updated_by"] = str(actor_id)
        block["updated_at"] = _utcnow().isoformat()
        org.settings = {**(org.settings or {}), "payments": block}
        flag_modified(org, "settings")
        self.db.commit()
        # The organisation has no audit table; the log line is the trail (no sums here anyway).
        logger.info(
            "payment settings updated org=%s actor=%s fields=%s",
            org_id, actor_id, sorted(changes),
        )
        return self.get_settings(org_id)

    @staticmethod
    def _status_settings(settings: PaymentSettings) -> ps.StatusSettings:
        return ps.StatusSettings(
            expiring_days=settings.expiring_days, expiring_lessons=settings.expiring_lessons
        )

    # -------------------------------------------------------------- loading

    def _entries(
        self,
        org_id: str,
        settings: PaymentSettings,
        today: date,
        *,
        tracked_only: bool = True,
        membership_id: Optional[UUID] = None,
        student_id: Optional[UUID] = None,
        group_id: Optional[UUID] = None,
        teacher_id: Optional[UUID] = None,
        search: Optional[str] = None,
    ) -> List[_Entry]:
        """Memberships with their status in a bounded number of queries.

        1. memberships + student + group + teacher (filters in SQL);
        2. the live payments of those memberships;
        3. lessons mode only: the completed lessons of the involved groups and
           the confirmed excuses of those lessons.
        """
        query = (
            self.db.query(StudentGroup, Student, Group, User)
            .join(Student, Student.id == StudentGroup.student_id)
            .join(Group, Group.id == StudentGroup.group_id)
            .outerjoin(User, User.id == Group.teacher_id)
            .filter(Student.org_id == org_id, Group.org_id == org_id)
        )
        if tracked_only:
            query = query.filter(
                StudentGroup.is_active.is_(True),
                Group.status == "active",
                Student.status != "archived",
            )
        if membership_id:
            query = query.filter(StudentGroup.id == membership_id)
        if student_id:
            query = query.filter(StudentGroup.student_id == student_id)
        if group_id:
            query = query.filter(StudentGroup.group_id == group_id)
        if teacher_id:
            query = query.filter(Group.teacher_id == teacher_id)
        term = (search or "").strip()
        if term:
            for word in term.split():
                pattern = _like(word)
                query = query.filter(
                    or_(
                        Student.first_name.ilike(pattern, escape="\\"),
                        Student.last_name.ilike(pattern, escape="\\"),
                        Student.display_id.ilike(pattern, escape="\\"),
                    )
                )
        rows = query.all()
        if not rows:
            return []

        ids = [m.id for m, *_ in rows]
        live_by_membership: Dict[UUID, List[StudentPayment]] = {}
        for payment in (
            self.db.query(StudentPayment)
            .filter(
                StudentPayment.org_id == org_id,
                StudentPayment.membership_id.in_(ids),
                StudentPayment.voided_at.is_(None),
            )
            .all()
        ):
            live_by_membership.setdefault(payment.membership_id, []).append(payment)

        records = {
            mid: [self._record(p) for p in plist] for mid, plist in live_by_membership.items()
        }
        lessons_members = [
            (m, st, g)
            for m, st, g, _ in rows
            if (last := ps.latest_payment(records.get(m.id, ()))) is not None and last.mode == ps.LESSONS
        ]
        # Lessons packages: capacities are derived along the carry-over chain (see ``_lessons_state``).
        state = self._lessons_state(
            org_id,
            [(m, g) for m, _, g in lessons_members],
            {m.id: live_by_membership[m.id] for m, _, _ in lessons_members},
        )
        lesson_dates = state.dates
        for m, _, _ in lessons_members:
            records[m.id] = [state.records.get(r.id, r) for r in records[m.id]]

        status_settings = self._status_settings(settings)
        entries = []
        for membership, student, group, teacher in rows:
            result = ps.compute_status(
                today=today,
                monthly_fee=membership.monthly_fee,
                payments=records.get(membership.id, ()),
                lesson_dates=lesson_dates.get(membership.id, ()),
                settings=status_settings,
            )
            entries.append(
                _Entry(
                    membership, student, group, teacher,
                    live_by_membership.get(membership.id, []), result,
                )
            )
        return entries

    @staticmethod
    def _record(payment: StudentPayment) -> ps.PaymentRecord:
        return ps.PaymentRecord(
            paid_on=payment.paid_on,
            valid_from=payment.valid_from,
            valid_to=payment.valid_to,
            lessons_count=payment.lessons_count,
            created_at=payment.created_at or datetime.min,
            id=payment.id,
            carried_out_at=payment.carried_out_at,
            membership_id=payment.membership_id,
            carried_from_id=payment.carried_from_payment_id,
        )

    def _lessons_state(
        self,
        org_id: str,
        seeds: Sequence[tuple],
        live_by_membership: Dict[UUID, Sequence[Any]],
    ) -> "_LessonsState":
        """Counted lessons and LIVE capacities of the lessons packages of ``seeds`` and of their chains.

        ``seeds``: ``(membership, group)`` pairs; ``live_by_membership``: their live payments (rows
        or entities with the payment columns).  A carry-over's capacity is derived along its chain
        (``payment_status.resolve_lesson_chains``), which needs the memberships on the way: the
        payments a carry-over came from and the carry-overs of a payment that moved away.  They are
        loaded in a few batched queries (one round per step of the chain) - and not at all when the
        seeds have no carry-overs.
        """
        members: Dict[UUID, tuple] = {m.id: (m, g) for m, g in seeds}
        live: Dict[UUID, List[Any]] = {mid: list(plist) for mid, plist in live_by_membership.items()}
        seen = {p.id for plist in live.values() for p in plist}
        probe = [p for plist in live.values() for p in plist if p.lessons_count is not None]
        for _ in range(MAX_CARRY_CHAIN):
            linked = [p for p in probe if p.carried_from_payment_id is not None or p.carried_out_at is not None]
            if not linked:
                break
            wanted_up = {p.carried_from_payment_id for p in linked if p.carried_from_payment_id} - seen
            moved = {p.id for p in linked if p.carried_out_at is not None}
            conditions = []
            if wanted_up:
                conditions.append(StudentPayment.id.in_(wanted_up))
            if moved:
                conditions.append(StudentPayment.carried_from_payment_id.in_(moved))
            if not conditions:
                break
            related = [
                p for p in self.db.query(StudentPayment)
                .filter(StudentPayment.org_id == org_id, StudentPayment.voided_at.is_(None), or_(*conditions))
                .all()
                if p.id not in seen and p.lessons_count is not None
            ]
            new_membership_ids = {p.membership_id for p in related} - set(members)
            if not new_membership_ids:
                break
            for membership in self.db.query(StudentGroup).filter(StudentGroup.id.in_(new_membership_ids)).all():
                members[membership.id] = (membership, None)
            groups = {
                g.id: g for g in self.db.query(Group).filter(
                    Group.id.in_({m.group_id for m, g in members.values() if g is None})
                )
            }
            for mid in new_membership_ids:
                membership, _ = members[mid]
                members[mid] = (membership, groups.get(membership.group_id))
            probe = []
            for p in self.db.query(StudentPayment).filter(
                StudentPayment.org_id == org_id,
                StudentPayment.voided_at.is_(None),
                StudentPayment.membership_id.in_(new_membership_ids),
            ):
                live.setdefault(p.membership_id, []).append(p)
                seen.add(p.id)
                if p.lessons_count is not None:
                    probe.append(p)
        usable = [(m, None, g) for m, g in members.values() if g is not None]
        if not usable or not any(
            p.lessons_count is not None for m, _, _ in usable for p in live.get(m.id, ())
        ):
            return _LessonsState({}, {}, {})
        late: Dict[UUID, Dict[datetime, int]] = {}
        dates = self._counted_lesson_dates(org_id, usable, live, late_out=late)
        records = []
        for m, _, _ in usable:
            records.extend(
                self._with_late(
                    [self._record(p) for p in live.get(m.id, ()) if p.lessons_count is not None],
                    late.get(m.id),
                )
            )
        resolved, _, left = ps.resolve_lesson_chains(records, dates)
        return _LessonsState(resolved, dates, left)

    @staticmethod
    def _with_late(
        records: List[ps.PaymentRecord], late: Optional[Dict[datetime, int]]
    ) -> List[ps.PaymentRecord]:
        """Fill ``carried_out_late`` of the lessons payments that moved away (see ``payment_status``)."""
        if not late:
            return records
        return [
            replace(r, carried_out_late=late.get(r.carried_out_at, 0))
            if r.carried_out_at is not None and r.lessons_count is not None
            else r
            for r in records
        ]

    def _scheduled_starts(self, group: Group, day: date) -> Dict[str, datetime]:
        """``{lesson_id: scheduled start}`` (naive UTC) of the group's lessons on one day.

        The schedule (with overrides and one-off lessons) is the only place that knows a lesson's
        start time; a lesson whose time cannot be read is left out (it counts as "before").
        """
        from .service import AuthService, lesson_start_as_naive_utc  # lazy: service is heavy

        service = AuthService(self.db)
        starts: Dict[str, datetime] = {}
        for occurrence in service._resolved_occurrences_for_group(group, date_from=day, date_to=day):
            try:
                minutes, _ = service._validate_lesson_time_range(
                    occurrence.get("startTime"), occurrence.get("endTime")
                )
            except ValueError:
                continue
            starts[str(occurrence.get("id"))] = lesson_start_as_naive_utc(day, minutes)
        return starts

    def _counted_lesson_dates(
        self,
        org_id: str,
        members: Sequence[tuple],
        live_by_membership: Dict[UUID, List[StudentPayment]],
        late_out: Optional[Dict[UUID, Dict[datetime, int]]] = None,
    ) -> Dict[UUID, List[date]]:
        """Dates of the lessons that spend a lessons payment, per membership.

        With ``late_out`` the lessons of the day a payment moved away on are looked at once more:
        ``late_out[membership][instant]`` = how many of them started at or after that instant
        (``PaymentRecord.carried_out_late``).

        Rule 4.3: the lesson is completed, the student is in its roster
        snapshot (no snapshot on a legacy lesson: being marked is enough), the
        mark is present / late / absent or an excused mark without a confirmed
        excuse.
        """
        if not members:
            return {}
        group_ids = {g.id for _, _, g in members}
        earliest = min(
            p.valid_from
            for m, _, _ in members
            for p in live_by_membership.get(m.id, [])
            if p.lessons_count is not None
        )
        lessons = (
            self.db.query(
                GroupAttendance.id,
                GroupAttendance.group_id,
                GroupAttendance.lesson_date,
                GroupAttendance.lesson_id,
                GroupAttendance.records,
                GroupAttendance.roster_snapshot,
            )
            .filter(
                GroupAttendance.org_id == org_id,
                GroupAttendance.group_id.in_(group_ids),
                GroupAttendance.status == "completed",
                GroupAttendance.lesson_date >= earliest.isoformat(),
            )
            .all()
        )
        if not lessons:
            return {}
        confirmed = {
            (row.attendance_id, str(row.student_id))
            for row in self.db.query(AttendanceExcuse.attendance_id, AttendanceExcuse.student_id)
            .filter(
                AttendanceExcuse.org_id == org_id,
                AttendanceExcuse.attendance_id.in_([lesson.id for lesson in lessons]),
                AttendanceExcuse.status == "confirmed",
            )
            .all()
        }
        by_group: Dict[UUID, list] = {}
        for lesson in lessons:
            roster = None
            if isinstance(lesson.roster_snapshot, list):
                roster = {
                    str(item.get("student_id"))
                    for item in lesson.roster_snapshot
                    if isinstance(item, dict) and item.get("student_id")
                }
            by_group.setdefault(lesson.group_id, []).append((lesson, roster))

        out: Dict[UUID, List[date]] = {}
        for membership, student, group in members:
            sid = str(membership.student_id)
            dates: List[date] = []
            # instants a payment of this membership moved away at, by business day
            moved_at: Dict[date, set] = {}
            if late_out is not None:
                for p in live_by_membership.get(membership.id, ()):
                    if p.lessons_count is not None and p.carried_out_at is not None:
                        moved_at.setdefault(ps.business_date(p.carried_out_at), set()).add(p.carried_out_at)
            on_moved_day: Dict[date, List[str]] = {}
            for lesson, roster in by_group.get(group.id, ()):
                if roster is not None and sid not in roster:
                    continue
                records = lesson.records if isinstance(lesson.records, dict) else {}
                excuse = "confirmed" if (lesson.id, sid) in confirmed else None
                if ps.lesson_counts(mark=records.get(sid), excuse_status=excuse):
                    try:
                        lesson_day = date.fromisoformat(lesson.lesson_date)
                    except ValueError:
                        logger.warning("Skipping lesson %s with malformed date", lesson.id)
                        continue
                    dates.append(lesson_day)
                    if lesson_day in moved_at:
                        on_moved_day.setdefault(lesson_day, []).append(str(lesson.lesson_id))
            out[membership.id] = dates
            if late_out is not None and on_moved_day:
                counts: Dict[datetime, int] = {}
                for lesson_day, lesson_ids in on_moved_day.items():
                    starts = self._scheduled_starts(group, lesson_day)
                    for instant in moved_at[lesson_day]:
                        counts[instant] = sum(
                            1 for lid in lesson_ids if lid in starts and starts[lid] >= instant
                        )
                late_out[membership.id] = counts
        return out

    # ---------------------------------------------------------- serialising

    def _users(self, ids: Iterable[Optional[UUID]]) -> Dict[UUID, User]:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        return {u.id: u for u in self.db.query(User).filter(User.id.in_(wanted)).all()}

    def _links(self, payments: Iterable[StudentPayment]) -> _Links:
        """Where the carry-overs among ``payments`` come from and where the stamped ones went."""
        payments = list(payments)
        sources: Dict[UUID, CarriedFromOut] = {}
        ids = {p.carried_from_payment_id for p in payments if p.carried_from_payment_id}
        if ids:
            for pid, name in (
                self.db.query(StudentPayment.id, Group.name)
                .join(Group, Group.id == StudentPayment.group_id)
                .filter(StudentPayment.id.in_(ids))
                .all()
            ):
                sources[pid] = CarriedFromOut(payment_id=str(pid), group_name=name or "")
        targets: Dict[UUID, Optional[str]] = {}
        stamped = {p.id for p in payments if p.carried_out_at is not None}
        if stamped:
            for source_id, name in (
                self.db.query(StudentPayment.carried_from_payment_id, Group.name)
                .join(Group, Group.id == StudentPayment.group_id)
                .filter(
                    StudentPayment.carried_from_payment_id.in_(stamped),
                    StudentPayment.voided_at.is_(None),
                )
                .order_by(StudentPayment.created_at)
                .all()
            ):
                targets[source_id] = name
        return _Links(sources, targets)

    def _payment_out(
        self,
        payment: StudentPayment,
        users: Dict[UUID, User],
        links: Optional[_Links] = None,
    ) -> PaymentOut:
        return PaymentOut(
            id=str(payment.id),
            membership_id=str(payment.membership_id),
            amount=int(payment.amount),
            currency=payment.currency,
            paid_on=payment.paid_on,
            valid_from=payment.valid_from,
            valid_to=payment.valid_to,
            lessons_count=payment.lessons_count,
            note=payment.note,
            replaces_payment_id=str(payment.replaces_payment_id) if payment.replaces_payment_id else None,
            carried_from=links.sources.get(payment.carried_from_payment_id) if links else None,
            carried_out=(
                CarriedOutOut(
                    date=ps.business_date(payment.carried_out_at), group_name=links.targets.get(payment.id)
                )
                if links and payment.carried_out_at is not None
                else None
            ),
            created_by=str(payment.created_by) if payment.created_by else None,
            created_by_name=_person(users.get(payment.created_by)),
            created_at=_iso(payment.created_at),
            voided_at=_iso(payment.voided_at),
            voided_by=str(payment.voided_by) if payment.voided_by else None,
            voided_by_name=_person(users.get(payment.voided_by)),
            void_reason=payment.void_reason,
        )

    @staticmethod
    def _last_live(entry: _Entry) -> Optional[StudentPayment]:
        if not entry.live:
            return None
        return max(entry.live, key=lambda p: (p.paid_on, p.created_at or datetime.min))

    def _rows(self, entries: Sequence[_Entry]) -> List[MembershipPaymentRow]:
        last = {e.membership.id: self._last_live(e) for e in entries}
        users = self._users(
            x for p in last.values() if p for x in (p.created_by, p.voided_by)
        )
        links = self._links(p for p in last.values() if p)
        rows = []
        for e in entries:
            payment = last[e.membership.id]
            r = e.result
            rows.append(
                MembershipPaymentRow(
                    membership_id=str(e.membership.id),
                    student_id=str(e.student.id),
                    student_name=e.student_name,
                    display_id=e.student.display_id,
                    class_level=e.student.class_level,
                    group_id=str(e.group.id),
                    group_name=e.group.name,
                    teacher_id=str(e.group.teacher_id) if e.group.teacher_id else None,
                    teacher_name=_person(e.teacher),
                    is_primary=str(e.student.group_id or "") == str(e.group.id),
                    is_active=bool(e.membership.is_active),
                    monthly_fee=int(e.membership.monthly_fee) if e.membership.monthly_fee is not None else None,
                    status=r.status,
                    period_mode=r.period_mode,
                    paid_until=r.paid_until,
                    days_overdue=r.days_overdue,
                    days_left=r.days_left,
                    lessons_total=r.lessons_total,
                    lessons_used=r.lessons_used,
                    lessons_left=r.lessons_left,
                    last_payment=self._payment_out(payment, users, links) if payment else None,
                )
            )
        return rows

    def _row(self, org_id: str, membership_id: UUID) -> MembershipPaymentRow:
        settings = self.get_settings(org_id)
        entries = self._entries(
            org_id, settings, ps.business_today(), tracked_only=False, membership_id=membership_id
        )
        if not entries:
            raise PaymentNotFound("Членство не найдено")
        return self._rows(entries)[0]

    # -------------------------------------------------------------- reading

    def summary(self, org_id: str) -> PaymentsSummary:
        settings = self.get_settings(org_id)
        today = ps.business_today()
        entries = self._entries(org_id, settings, today)
        counts = {s: 0 for s in ps.STATUSES}
        for e in entries:
            counts[e.result.status] += 1
        return PaymentsSummary(
            as_of=today.isoformat(),
            due=counts[ps.EXPIRED] + counts[ps.ENDS_TODAY],
            ends_today=counts[ps.ENDS_TODAY],
            expired=counts[ps.EXPIRED],
            expiring=counts[ps.EXPIRING],
            none=counts[ps.NONE],
            active=counts[ps.ACTIVE] + counts[ps.FREE],
            free=counts[ps.FREE],
            tracked_total=len(entries),
            period_mode=settings.period_mode,
            lessons_per_payment=settings.lessons_per_payment,
        )

    @staticmethod
    def _sort_key(bucket: str):
        def name(e: _Entry):
            return (e.student_name.lower(), e.group.name.lower(), str(e.membership.id))

        if bucket == ps.BUCKET_DUE:
            # Most overdue first; "ends today" and a spent lessons payment have 0 days.
            return lambda e: (-(e.result.days_overdue or 0), *name(e))
        if bucket == ps.BUCKET_EXPIRING:
            def remaining(e: _Entry):
                r = e.result
                return r.days_left if r.period_mode == ps.CALENDAR else r.lessons_left
            return lambda e: (remaining(e) if remaining(e) is not None else 10**9, *name(e))
        return name

    def status_page(
        self,
        org_id: str,
        *,
        bucket: str = ps.BUCKET_ALL,
        group_id: Optional[UUID] = None,
        teacher_id: Optional[UUID] = None,
        search: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PaymentStatusPage:
        settings = self.get_settings(org_id)
        today = ps.business_today()
        entries = self._entries(
            org_id, settings, today, group_id=group_id, teacher_id=teacher_id, search=search
        )
        picked = [e for e in entries if ps.in_bucket(e.result.status, bucket)]
        picked.sort(key=self._sort_key(bucket))
        page = picked[offset : offset + limit]
        return PaymentStatusPage(
            items=self._rows(page), total=len(picked), limit=limit, offset=offset,
            as_of=today.isoformat(),
        )

    def student_memberships(self, org_id: str, student_id: UUID) -> List[MembershipPaymentRow]:
        student = (
            self.db.query(Student.id).filter(Student.id == student_id, Student.org_id == org_id).first()
        )
        if not student:
            raise PaymentNotFound("Ученик не найден")
        settings = self.get_settings(org_id)
        entries = self._entries(
            org_id, settings, ps.business_today(), tracked_only=False, student_id=student_id
        )
        with_history = {
            row[0]
            for row in self.db.query(StudentPayment.membership_id)
            .filter(
                StudentPayment.org_id == org_id,
                StudentPayment.membership_id.in_([e.membership.id for e in entries] or [None]),
            )
            .distinct()
            .all()
        }
        # Active memberships, and closed ones only if they carry payments.
        kept = [e for e in entries if e.membership.is_active or e.membership.id in with_history]
        kept.sort(
            key=lambda e: (
                not (str(e.student.group_id or "") == str(e.group.id)),
                not e.membership.is_active,
                e.membership.joined_at or datetime.min,
            )
        )
        return self._rows(kept)

    def list_payments(
        self, org_id: str, membership_id: UUID, include_voided: bool = True
    ) -> List[PaymentOut]:
        self._membership(org_id, membership_id)
        query = self.db.query(StudentPayment).filter(
            StudentPayment.org_id == org_id, StudentPayment.membership_id == membership_id
        )
        if not include_voided:
            query = query.filter(StudentPayment.voided_at.is_(None))
        payments = query.order_by(
            StudentPayment.paid_on.desc(), StudentPayment.created_at.desc(), StudentPayment.id
        ).all()
        users = self._users(x for p in payments for x in (p.created_by, p.voided_by))
        links = self._links(payments)
        return [self._payment_out(p, users, links) for p in payments]

    # -------------------------------------------------------------- writing

    def _membership(self, org_id: str, membership_id: UUID, *, lock: bool = False) -> StudentGroup:
        query = (
            self.db.query(StudentGroup)
            .join(Student, Student.id == StudentGroup.student_id)
            .join(Group, Group.id == StudentGroup.group_id)
            .filter(
                StudentGroup.id == membership_id,
                Student.org_id == org_id,
                Group.org_id == org_id,
            )
        )
        if lock:
            # FOR NO KEY UPDATE: still exclusive among membership editors, but it does not block the
            # FOR KEY SHARE that an insert of a payment (or carry-over) takes on this row.
            query = query.with_for_update(of=StudentGroup, key_share=True)
        membership = query.first()
        if not membership:
            raise PaymentNotFound("Членство не найдено")
        return membership

    @deadlock_guard
    def set_fee(
        self, org_id: str, membership_id: UUID, actor_id: str, monthly_fee: Optional[int]
    ) -> MembershipPaymentRow:
        membership = self._membership(org_id, membership_id, lock=True)
        membership.monthly_fee = monthly_fee
        membership.monthly_fee_updated_by = UUID(str(actor_id))
        membership.monthly_fee_updated_at = _utcnow()
        self.db.commit()
        logger.info("membership fee updated membership=%s actor=%s", membership_id, actor_id)
        return self._row(org_id, membership_id)

    def _resolve_period(
        self,
        data: PaymentCreate,
        settings: PaymentSettings,
        live: Sequence[StudentPayment],
        today: date,
    ) -> tuple[date, Optional[date], Optional[int]]:
        if data.valid_to is not None and data.lessons_count is not None:
            raise PaymentInvalid("Укажите что-то одно: «оплачено до» или число уроков")
        mode = (
            ps.CALENDAR if data.valid_to is not None
            else ps.LESSONS if data.lessons_count is not None
            else settings.period_mode
        )
        # What the live payments still cover in this membership (a moved-away original ends the day before).
        previous_until = max(
            (r.end for r in map(self._record, live) if r.end is not None and not r.covers_nothing),
            default=None,
        )
        # The payment date and the start are bounded first: every default
        # below does date arithmetic that must never see 0001 or 9999.
        errors = ps.validation_errors(
            today=today, paid_on=data.paid_on, valid_from=data.valid_from or data.paid_on,
            valid_to=None,
        )
        if errors:
            raise PaymentInvalid(errors[0])
        try:
            valid_from = data.valid_from or ps.default_valid_from(mode, data.paid_on, previous_until)
        except OverflowError:
            raise PaymentInvalid("Начало периода слишком далеко в будущем")
        problem = ps.valid_from_error(data.paid_on, valid_from)
        if problem:
            raise PaymentInvalid(problem)
        if mode == ps.CALENDAR:
            try:
                valid_to = data.valid_to or ps.add_month_minus_day(valid_from)
            except OverflowError:
                raise PaymentInvalid("Период выходит за допустимые даты")
            lessons_count = None
        else:
            valid_to = None
            lessons_count = data.lessons_count or settings.lessons_per_payment
        errors = ps.validation_errors(
            today=today, paid_on=data.paid_on, valid_from=valid_from, valid_to=valid_to
        )
        if errors:
            raise PaymentInvalid(errors[0])
        return valid_from, valid_to, lessons_count

    def _insert(
        self,
        membership: StudentGroup,
        org_id: str,
        actor_id: str,
        data: PaymentCreate,
        settings: PaymentSettings,
        today: date,
        *,
        replaces: Optional[StudentPayment] = None,
    ) -> tuple[StudentPayment, List[PaymentWarningOut]]:
        live = (
            self.db.query(StudentPayment)
            .filter(
                StudentPayment.org_id == org_id,
                StudentPayment.membership_id == membership.id,
                StudentPayment.voided_at.is_(None),
            )
            .all()
        )
        valid_from, valid_to, lessons_count = self._resolve_period(data, settings, live, today)

        duplicate = next(
            (
                p for p in live
                if p.paid_on == data.paid_on and p.valid_from == valid_from
                and p.valid_to == valid_to and p.lessons_count == lessons_count
                and int(p.amount) == data.amount
            ),
            None,
        )
        if duplicate:
            raise DuplicatePayment("Такая оплата уже внесена")

        payment = StudentPayment(
            org_id=org_id,
            membership_id=membership.id,
            student_id=membership.student_id,
            group_id=membership.group_id,
            amount=data.amount,
            currency="UZS",
            paid_on=data.paid_on,
            valid_from=valid_from,
            valid_to=valid_to,
            lessons_count=lessons_count,
            note=data.note,
            replaces_payment_id=replaces.id if replaces else None,
            created_by=UUID(str(actor_id)),
            created_at=_utcnow(),
        )
        try:
            with self.db.begin_nested():
                self.db.add(payment)
                self.db.flush()
        except IntegrityError as exc:
            # A concurrent identical request slipped past the check above.
            if UNIQUE_DUP_INDEX in str(exc.orig):
                raise DuplicatePayment("Такая оплата уже внесена") from exc
            raise

        warnings = [
            PaymentWarningOut(code=w.code, message=w.message)
            for w in ps.payment_warnings(
                today=today,
                paid_on=data.paid_on,
                valid_from=valid_from,
                valid_to=valid_to,
                existing=[self._record(p) for p in live],
            )
        ]
        return payment, warnings

    def _created(self, org_id: str, payment: StudentPayment, warnings) -> PaymentCreated:
        users = self._users([payment.created_by])
        return PaymentCreated(
            payment=self._payment_out(payment, users, self._links([payment])),
            warnings=warnings,
            row=self._row(org_id, payment.membership_id),
        )

    @deadlock_guard
    def create_payment(
        self, org_id: str, membership_id: UUID, actor_id: str, data: PaymentCreate
    ) -> PaymentCreated:
        try:
            membership = self._membership(org_id, membership_id, lock=True)
            settings = self.get_settings(org_id)
            payment, warnings = self._insert(
                membership, org_id, actor_id, data, settings, ps.business_today()
            )
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        logger.info(
            "payment created id=%s membership=%s actor=%s", payment.id, membership_id, actor_id
        )
        return self._created(org_id, payment, warnings)

    def _probe_payment(self, org_id: str, payment_id: UUID) -> StudentPayment:
        """The payment without a lock: just enough to learn which rows to lock first."""
        payment = (
            self.db.query(StudentPayment)
            .filter(StudentPayment.id == payment_id, StudentPayment.org_id == org_id)
            .first()
        )
        if not payment:
            raise PaymentNotFound("Оплата не найдена")
        return payment

    def _lock_payment(self, org_id: str, payment_id: UUID) -> StudentPayment:
        payment = (
            self.db.query(StudentPayment)
            .filter(StudentPayment.id == payment_id, StudentPayment.org_id == org_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if not payment:
            raise PaymentNotFound("Оплата не найдена")
        if payment.voided_at is not None:
            raise AlreadyVoided("Оплата уже отменена")
        return payment

    @deadlock_guard
    def replace_payment(
        self,
        org_id: str,
        payment_id: UUID,
        actor_id: str,
        data: PaymentCreate,
        reason: Optional[str] = None,
    ) -> PaymentCreated:
        """Void the old payment and insert the corrected one - one transaction.

        Lock order: membership, then the payment (a transfer takes the membership first too).
        A carry-over cannot be corrected: correct the payment it was made from.
        """
        try:
            probe = self._probe_payment(org_id, payment_id)
            if probe.carried_from_payment_id is not None:
                raise CarryOverNotReplaceable("Перенос нельзя исправить — исправьте исходную оплату")
            membership = self._membership(org_id, probe.membership_id, lock=True)
            old = self._lock_payment(org_id, payment_id)
            old.voided_at = _utcnow()
            old.voided_by = UUID(str(actor_id))
            old.void_reason = (reason or "").strip() or DEFAULT_REPLACE_REASON
            # The old row must be out of the live set before the unique index
            # sees the new one (a correction may restate the same figures).
            self.db.flush()
            settings = self.get_settings(org_id)
            payment, warnings = self._insert(
                membership, org_id, actor_id, data, settings, ps.business_today(), replaces=old
            )
            # Carry-overs of the old payment follow the correction; the corrected one keeps the
            # stamp "moved away at T" (set before the rebuild: the remainder is counted up to T).
            if old.carried_out_at is not None:
                payment.carried_out_at = old.carried_out_at
                self.db.flush()
                self._rebuild_carry_overs(org_id, actor_id, old, payment, CARRY_REPLACE_REASON)
                if not self._live_children(org_id, payment.id):  # nothing moved after all
                    payment.carried_out_at = None
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        logger.info(
            "payment replaced old=%s new=%s membership=%s actor=%s",
            payment_id, payment.id, membership.id, actor_id,
        )
        return self._created(org_id, payment, warnings)

    @deadlock_guard
    def void_payment(
        self, org_id: str, payment_id: UUID, actor_id: str, reason: str
    ) -> PaymentVoided:
        """Lock order: membership, the payment it was carried over from (if any), the payment,
        then its carry-overs - a parent is never locked after a child."""
        try:
            probe = self._probe_payment(org_id, payment_id)
            self._membership(org_id, probe.membership_id, lock=True)
            if probe.carried_from_payment_id is not None:
                self.db.query(StudentPayment).filter(
                    StudentPayment.id == probe.carried_from_payment_id, StudentPayment.org_id == org_id
                ).populate_existing().with_for_update().first()
            payment = self._lock_payment(org_id, payment_id)
            payment.voided_at = _utcnow()
            payment.voided_by = UUID(str(actor_id))
            payment.void_reason = reason
            self.db.flush()
            self._rebuild_carry_overs(org_id, actor_id, payment, None, CARRY_VOID_REASON)
            self._release_source(org_id, payment)
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        logger.info("payment voided id=%s actor=%s", payment_id, actor_id)
        users = self._users([payment.created_by, payment.voided_by])
        return PaymentVoided(
            payment=self._payment_out(payment, users, self._links([payment])),
            row=self._row(org_id, payment.membership_id),
        )

    # ------------------------------------------------- carry-over (N13 P2.1)

    def _remaining_lessons(
        self,
        org_id: str,
        pairs: Sequence[tuple],
        live_by_membership: Dict[UUID, Sequence[Any]],
    ) -> Dict[UUID, int]:
        """Lessons still unspent per lessons payment, ``{payment_id: left}``: the FIFO of
        ``payment_status`` with capacities derived live along the carry-over chains."""
        pairs = [
            (m, g) for m, g in pairs
            if any(p.lessons_count is not None for p in live_by_membership.get(m.id, ()))
        ]
        if not pairs:
            return {}
        return self._lessons_state(org_id, pairs, live_by_membership).left

    @staticmethod
    def _copy_fee(
        target: StudentGroup,
        sources: Sequence[StudentGroup],
        actor_id: Optional[str],
        primary_group_id: Optional[UUID],
    ) -> None:
        """A new group without a fee takes the fee of the group the student leaves.

        An amount passed in the transfer request is already on ``target`` and wins.
        The student's previous primary group is the donor if it has a fee, else
        the most recently joined group that has one.
        """
        if target.monthly_fee is not None:
            return
        donors = [m for m in sources if m.monthly_fee is not None]
        if not donors:
            return
        donor = next(
            (m for m in donors if primary_group_id and str(m.group_id) == str(primary_group_id)),
            None,
        ) or max(donors, key=lambda m: m.joined_at or datetime.min)
        target.monthly_fee = int(donor.monthly_fee)
        target.monthly_fee_updated_by = UUID(str(actor_id)) if actor_id else None
        target.monthly_fee_updated_at = _utcnow()

    def carry_over_on_transfer(
        self,
        org_id: str,
        actor_id: Optional[str],
        sources: Sequence[StudentGroup],
        target: StudentGroup,
        *,
        primary_group_id: Optional[UUID] = None,
        detailed: bool = False,
        moment: Optional[datetime] = None,
    ) -> List[Dict[str, str]]:
        """Move the payments in force from the memberships a transfer closes into ``target``.

        Called by ``AuthService.add_student_to_group`` (mode ``transfer`` only) inside its
        transaction: this method flushes but never commits.  ``target`` must already be flushed.

        ``moment`` is the transfer instant (the same one the closed memberships get as ``left_at``;
        default now).  In force on the business date T of that instant, per closed membership, among the
        payments that have not moved yet (``carried_out_at`` empty) and only for the mode of the
        latest of them (the others are ignored by the status too):

        * a calendar payment with ``valid_to >= T`` -> carry-over ``valid_from = max(T, its
          valid_from)`` (a prepaid future period keeps its start), the same ``valid_to``;
        * a lessons package with R >= 1 lessons left after the FIFO hand-out -> carry-over with
          ``lessons_count = R``, ``valid_from`` as above.

        A carry-over is a ``StudentPayment`` of amount 0 with the original ``paid_on``, the note
        "Перенос оплаты из «<группа>»" and ``carried_from_payment_id``.  The original is stamped
        ``carried_out_at = moment`` and from then on covers only what happened while the student
        was still in its group (a lesson that started before the instant, a calendar day up to and
        including T), so a return (B -> A) is just one more transfer: the live carry-over on B (with
        what it has consumed) is carried again and stamped in its turn - nothing is counted twice
        and no lesson falls between the two memberships.

        Also copies the fee of the closed group to ``target`` if the target has none.  Returns the
        ``warnings`` entries for the response: dates / lessons when ``detailed`` (the caller may
        see payments), else one generic sentence; no amount in either.
        """
        sources = [m for m in sources if m.id != target.id]
        if not sources:
            return []
        self._copy_fee(target, sources, actor_id, primary_group_id)
        if not actor_id:  # created_by is mandatory; an internal call without an actor carries nothing
            return []

        if moment is None:
            moment = _utcnow()
        elif moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
        today = ps.business_date(moment)
        live_by_membership: Dict[UUID, List[StudentPayment]] = {}
        # Locked (after the student and the memberships): a payment voided a moment ago by someone
        # else is waited for and then drops out of the live set instead of being carried over.
        for payment in (
            self.db.query(StudentPayment)
            .filter(
                StudentPayment.org_id == org_id,
                StudentPayment.membership_id.in_([m.id for m in sources]),
                StudentPayment.voided_at.is_(None),
            )
            .order_by(StudentPayment.created_at, StudentPayment.id)
            .populate_existing()
            .with_for_update()
            .all()
        ):
            live_by_membership.setdefault(payment.membership_id, []).append(payment)
        if not live_by_membership:
            return []
        target_group_id = UUID(str(target.group_id))  # a membership created a moment ago holds the raw string
        groups = {
            g.id: g
            for g in self.db.query(Group).filter(
                Group.org_id == org_id, Group.id.in_({m.group_id for m in sources} | {target_group_id})
            )
        }

        candidates: List[_Carry] = []
        lessons_side = []
        for membership in sources:
            payments = live_by_membership.get(membership.id)
            if not payments:
                continue
            name = groups[membership.group_id].name if membership.group_id in groups else ""
            # A payment that already moved away covers only the past of this membership.
            staying = [p for p in payments if p.carried_out_at is None]
            if not staying:
                continue
            latest = ps.latest_payment([self._record(p) for p in staying])
            if latest.mode == ps.CALENDAR:
                for p in staying:
                    if p.valid_to is not None and p.valid_to >= today:
                        candidates.append(_Carry(p, name, max(today, p.valid_from), p.valid_to, None))
            else:
                lessons_side.append((membership, name))
        left = self._remaining_lessons(
            org_id, [(m, groups[m.group_id]) for m, _ in lessons_side if m.group_id in groups],
            live_by_membership,
        )
        for membership, name in lessons_side:
            for p in live_by_membership[membership.id]:
                if p.lessons_count is not None and p.carried_out_at is None and left.get(p.id, 0) >= 1:
                    candidates.append(_Carry(p, name, max(today, p.valid_from), None, left[p.id]))
        if not candidates:
            return []

        candidates.sort(
            key=lambda c: (c.original.paid_on, c.original.created_at or datetime.min, str(c.original.id))
        )
        now = _utcnow()
        made: List[tuple] = []  # (new row, its source)
        for c in candidates:
            row = StudentPayment(
                org_id=org_id,
                membership_id=target.id,
                student_id=UUID(str(target.student_id)),
                group_id=target_group_id,
                amount=0,
                currency="UZS",
                paid_on=c.original.paid_on,
                valid_from=c.valid_from,
                valid_to=c.valid_to,
                lessons_count=c.lessons_count,
                note=CARRY_NOTE.format(group=c.group_name),
                carried_from_payment_id=c.original.id,
                created_by=UUID(str(actor_id)),
                created_at=now,
            )
            self.db.add(row)
            c.original.carried_out_at = moment  # from now on it covers only what happened before this instant
            made.append((row, c))
        if not made:
            return []
        self.db.flush()
        logger.info(
            "payments carried over to membership=%s from=%s originals=%s actor=%s",
            target.id, [str(m.id) for m in sources], [str(c.original.id) for _, c in made], actor_id,
        )

        if not detailed:
            return [{"code": CARRY_CODE, "message": CARRY_GENERIC_MESSAGE}]
        target_name = groups[target_group_id].name if target_group_id in groups else ""
        messages = []
        for row, c in made:
            if c.lessons_count is not None:
                text = (
                    f"Остаток пакета — {c.lessons_count} {_plural_lessons(c.lessons_count)} — "
                    f"перенесён в «{target_name}»"
                )
            else:
                text = f"Оплата до {_short_date(c.valid_to, today)} перенесена в «{target_name}»"
            messages.append({"code": CARRY_CODE, "message": text})
        return messages

    def _release_source(self, org_id: str, carry_over: StudentPayment) -> None:
        """A cancelled carry-over gives its source the coverage back (the stamp is cleared).

        Only while the source is live and no other live carry-over was made from it.
        """
        if carry_over.carried_from_payment_id is None:
            return
        source = (
            self.db.query(StudentPayment)
            .filter(
                StudentPayment.id == carry_over.carried_from_payment_id,
                StudentPayment.org_id == org_id,
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if source is None or source.voided_at is not None or source.carried_out_at is None:
            return
        others = [c for c in self._live_children(org_id, source.id) if c.id != carry_over.id]
        if not others:
            source.carried_out_at = None
            logger.info("payment=%s covers its group again (carry-over %s cancelled)", source.id, carry_over.id)

    def _live_children(self, org_id: str, parent_id: UUID) -> List[StudentPayment]:
        return (
            self.db.query(StudentPayment)
            .filter(
                StudentPayment.org_id == org_id,
                StudentPayment.carried_from_payment_id == parent_id,
                StudentPayment.voided_at.is_(None),
            )
            .order_by(StudentPayment.created_at, StudentPayment.id)
            .populate_existing()
            .with_for_update()
            .all()
        )

    def _remaining_for(self, org_id: str, payment: StudentPayment) -> int:
        """Lessons left in one lessons payment (0 for a calendar payment)."""
        if payment.lessons_count is None:
            return 0
        membership = self.db.query(StudentGroup).filter(StudentGroup.id == payment.membership_id).first()
        group = self.db.query(Group).filter(Group.id == payment.group_id).first()
        if not membership or not group:
            return 0
        live = {
            membership.id: self.db.query(StudentPayment)
            .filter(
                StudentPayment.org_id == org_id,
                StudentPayment.membership_id == membership.id,
                StudentPayment.voided_at.is_(None),
            )
            .all()
        }
        return self._remaining_lessons(org_id, [(membership, group)], live).get(payment.id, 0)

    def _carry_again(
        self,
        org_id: str,
        actor_id: str,
        new_parent: StudentPayment,
        child: StudentPayment,
    ) -> Optional[StudentPayment]:
        """A replacement carry-over of ``child`` made from the corrected ``new_parent``, if it still applies.

        Same start as the old carry-over; the end (calendar) or the remainder (lessons) comes from
        the corrected payment.  None when the corrected payment no longer covers that start.
        """
        start = child.valid_from
        if new_parent.valid_to is not None:
            if new_parent.valid_to < start:
                return None
            valid_to, lessons = new_parent.valid_to, None
        else:
            lessons = self._remaining_for(org_id, new_parent)
            if lessons < 1:
                return None
            valid_to = None
        group_name = self.db.query(Group.name).filter(Group.id == new_parent.group_id).scalar() or ""
        row = StudentPayment(
            org_id=org_id,
            membership_id=child.membership_id,
            student_id=child.student_id,
            group_id=child.group_id,
            amount=0,
            currency="UZS",
            paid_on=new_parent.paid_on,
            valid_from=max(start, new_parent.valid_from),
            valid_to=valid_to,
            lessons_count=lessons,
            note=CARRY_NOTE.format(group=group_name),
            carried_from_payment_id=new_parent.id,
            carried_out_at=child.carried_out_at,  # it had moved on: the new one moved on too
            created_by=UUID(str(actor_id)),
            created_at=_utcnow(),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            if UNIQUE_DUP_INDEX in str(exc.orig):
                logger.warning("carry-over not recreated (identical payment exists) child=%s", child.id)
                return None
            raise
        return row

    def _rebuild_carry_overs(
        self,
        org_id: str,
        actor_id: str,
        old_parent: StudentPayment,
        new_parent: Optional[StudentPayment],
        reason: str,
    ) -> None:
        """Void the live carry-overs of ``old_parent`` (and theirs, down the chain).

        With a ``new_parent`` (the replacement of a corrected payment) each voided carry-over is
        made again from it when the corrected payment still covers the carry-over's start, and the
        chain below is rebuilt from the new row; without one the whole chain is just cancelled.
        ``old_parent`` must already be voided and flushed (the unique index then ignores it).
        """
        voided = 0
        stack = [(old_parent, new_parent, 0)]
        while stack:
            parent, replacement, depth = stack.pop()
            if depth > MAX_CARRY_CHAIN:
                break
            for child in self._live_children(org_id, parent.id):
                child.voided_at = _utcnow()
                child.voided_by = UUID(str(actor_id))
                child.void_reason = reason
                voided += 1
                self.db.flush()
                again = (
                    self._carry_again(org_id, actor_id, replacement, child)
                    if replacement is not None
                    else None
                )
                stack.append((child, again, depth + 1))
        if voided:
            logger.info(
                "carry-overs of payment=%s voided count=%s replaced=%s actor=%s",
                old_parent.id, voided, new_parent is not None, actor_id,
            )
