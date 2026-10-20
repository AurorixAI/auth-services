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
* ROADMAP #105.  The price is no longer a property of the membership: ``pricing.py`` resolves the effective price
  (the price of the group, or the individual exception of the student) on a day.  The status uses the price of
  today ("не задана" / free), the amount hint (``quote``) the price at the start of the payment's period, the
  soft warning of a short period the same.  The sum of a payment is stored in the payment itself, so a change of a
  price - also a backdated one - never alters a payment already entered.
* N13 P2.2.  One payment day for the centre (``billing_anchor = fixed_day``): ``quote`` suggests the dates and
  the amount of a payment with the very code that creates it (``_resolve_period``), writing nothing.  Every
  payment but a carry-over gets a receipt number ``max + 1`` inside the organisation, taken in a SAVEPOINT and
  retried on a unique-index conflict - no lock on the organisation row, so the lock order above is unchanged.  A
  carry-over has no number and copies ``partial_period`` from its source.  Cheques (photos / PDFs) are kept in
  ``student_payment_files`` like the certificates of excuses.
"""
from __future__ import annotations

import functools
import logging
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, defer
from sqlalchemy.orm.attributes import flag_modified

from src.models.auth import (
    AttendanceExcuse,
    Group,
    GroupAttendance,
    Organization,
    Student,
    StudentGroup,
    StudentPayment,
    StudentPaymentFile,
    User,
)

from . import payment_status as ps
from . import pricing
from .money_words import amount_words
from .payments_files import (
    MAX_FILE_BYTES,
    MAX_FILES_PER_PAYMENT,
    load_files,
    person_name,
    sniff_content_type,
    staff_name,
    stored_filename,
)
from .payments_schemas import (
    MAX_MONEY,
    CarriedFromOut,
    CarriedOutOut,
    MembershipPaymentRow,
    PaymentCreate,
    PaymentCreated,
    PaymentFileOut,
    PaymentOut,
    PaymentQuote,
    PaymentSettings,
    PaymentSettingsUpdate,
    PaymentsSummary,
    PaymentStatusPage,
    PaymentVoided,
    PaymentWarningOut,
    PriceOut,
    QuotePartOut,
    ReceiptOut,
)

logger = logging.getLogger(__name__)

DEFAULT_REPLACE_REASON = "Исправлено"
UNIQUE_DUP_INDEX = "uq_student_payments_active_dup"
RECEIPT_INDEX = "uq_student_payments_receipt_no"
FILE_ON_CARRY_OVER = "К переносу оплаты чек не прикрепляется — это не денежная операция"
FILE_ON_VOIDED = "Оплата аннулирована — чек прикрепить нельзя"
FILE_LIMIT = f"К оплате можно приложить не более {MAX_FILES_PER_PAYMENT} файлов"
RECEIPT_ATTEMPTS = 10  # max+1 under a SAVEPOINT; a conflict with a concurrent payment takes the next number

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


class StudentArchived(PaymentConflict):
    """The student left the centre: money is not entered on his closed memberships until he is brought back
    (a payment can still be voided, and the history can be read)."""


class ConcurrentEdit(PaymentConflict):
    """A deadlock with another edit of the same student; nothing was written, repeat the request."""


class PaymentForbidden(PaymentError):
    status_code = 403


class PaymentFileTooLarge(PaymentError):
    status_code = 413


class PaymentFileUnsupported(PaymentError):
    status_code = 415


CONCURRENT_EDIT_MESSAGE = "Операция совпала с другой правкой. Повторите."
ARCHIVED_STUDENT_MESSAGE = "Ученик в архиве — сначала верните его"


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
    return staff_name(user.first_name, user.last_name)


def _plural_lessons(count: int) -> str:
    last, last2 = count % 10, count % 100
    if last == 1 and last2 != 11:
        return "урок"
    if 2 <= last <= 4 and not 12 <= last2 <= 14:
        return "урока"
    return "уроков"


def _plural_days(count: int) -> str:
    last, last2 = count % 10, count % 100
    if last == 1 and last2 != 11:
        return "день"
    if 2 <= last <= 4 and not 12 <= last2 <= 14:
        return "дня"
    return "дней"


def _num(amount: int) -> str:
    """``250 000`` (a plain space between the thousands)."""
    return f"{amount:,}".replace(",", " ")


def _sum(amount: int) -> str:
    """``250 000 сум``."""
    return _num(amount) + " сум"


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
    taken: Dict[UUID, List[date]] = field(default_factory=dict)  # payment id -> dates of the lessons it covers


@dataclass(frozen=True)
class LessonCover:
    """What pays for one counted lesson (``PaymentService.lesson_coverage``)."""

    payment_id: Optional[UUID]  # None = the lesson is not covered by any live payment
    mode: Optional[str]  # ``payment_status.CALENDAR`` / ``LESSONS`` / None
    # Lessons mode: size of the ORIGINAL package (a carry-over keeps its root's size) - the divisor of
    # payroll.  For a lesson that no payment covers: the size of the membership's latest package when the
    # membership pays by packages (so an unpaid lesson that the owner includes is priced like the others),
    # else None.
    package_size: Optional[int]


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
    price: pricing.Price = pricing.NO_PRICE  # the effective price today (#105)

    @property
    def student_name(self) -> str:
        return person_name(self.student.last_name, self.student.first_name) or ""


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
           the confirmed excuses of those lessons;
        4. the price history of those groups and memberships (two queries): the status takes the price of today.
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
        books = pricing.load_books(self.db, {g.id for _, _, g, _ in rows}, ids)  # 4. the price histories
        entries = []
        for membership, student, group, teacher in rows:
            price = books.price(membership.id, membership.group_id, today)
            result = ps.compute_status(
                today=today,
                monthly_fee=price.amount,
                payments=records.get(membership.id, ()),
                lesson_dates=lesson_dates.get(membership.id, ()),
                settings=status_settings,
            )
            entries.append(
                _Entry(
                    membership, student, group, teacher,
                    live_by_membership.get(membership.id, []), result, price,
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
        *,
        since: Optional[date] = None,
        until: Optional[date] = None,
        detail_out: Optional[Dict[UUID, List[tuple]]] = None,
    ) -> "_LessonsState":
        """Counted lessons and LIVE capacities of the lessons packages of ``seeds`` and of their chains.

        ``seeds``: ``(membership, group)`` pairs; ``live_by_membership``: their live payments (rows
        or entities with the payment columns).  A carry-over's capacity is derived along its chain
        (``payment_status.resolve_lesson_chains``), which needs the memberships on the way: the
        payments a carry-over came from and the carry-overs of a payment that moved away.  They are
        loaded in a few batched queries (one round per step of the chain) - and not at all when the
        seeds have no carry-overs.

        ``since`` / ``until`` / ``detail_out`` are passed to ``_counted_lesson_dates`` (payroll reads one
        month: it needs the lessons of that month even before the first package, and their ids).
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
        dates = self._counted_lesson_dates(
            org_id, usable, live, late_out=late, since=since, until=until, detail_out=detail_out
        )
        records = []
        for m, _, _ in usable:
            records.extend(
                self._with_late(
                    [self._record(p) for p in live.get(m.id, ()) if p.lessons_count is not None],
                    late.get(m.id),
                )
            )
        resolved, taken, left = ps.resolve_lesson_chains(records, dates)
        return _LessonsState(resolved, dates, left, taken)

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

    def _scheduled_starts(self, group: Group, day: date, preloaded: Any = None) -> Dict[str, datetime]:
        """``{lesson_id: scheduled start}`` (naive UTC) of the group's lessons on one day.

        The schedule (with overrides and one-off lessons) is the only place that knows a lesson's
        start time; a lesson whose time cannot be read is left out (it counts as "before").
        ``preloaded`` (``AuthService._preload_schedule_data``) replaces the per-group queries.
        """
        from .service import AuthService, lesson_start_as_naive_utc  # lazy: service is heavy

        service = AuthService(self.db)
        starts: Dict[str, datetime] = {}
        for occurrence in service._resolved_occurrences_for_group(
            group, date_from=day, date_to=day, preloaded=preloaded
        ):
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
        *,
        since: Optional[date] = None,
        until: Optional[date] = None,
        detail_out: Optional[Dict[UUID, List[tuple]]] = None,
    ) -> Dict[UUID, List[date]]:
        """Dates of the lessons that spend a lessons payment, per membership.

        With ``late_out`` the lessons of the day a payment moved away on are looked at once more:
        ``late_out[membership][instant]`` = how many of them started at or after that instant
        (``PaymentRecord.carried_out_late``).

        Rule 4.3: the lesson is completed, the student is in its roster
        snapshot (no snapshot on a legacy lesson: being marked is enough), the
        mark is present / late / absent or an excused mark without a confirmed
        excuse.

        Payroll (N13 P3) reads the same lessons through three optional arguments: ``since`` moves the
        start of the read back (it never moves it forward), ``until`` cuts the end (lessons after it
        cannot change who covers an earlier lesson), ``detail_out[membership]`` receives
        ``(day, lesson_id, attendance_id)`` of every counted lesson in the ORDER OF HAND-OUT: by day,
        then - when a day has several lessons of the student - by scheduled start, then by lesson id.
        That is the order ``_take`` assumes ("the later lessons of the day come last") and the one
        ``lesson_coverage`` uses to say WHICH lesson of a day a package covers, so the status page
        (which counts) and payroll (which names the lessons) share one rule.
        """
        if not members:
            return {}
        group_ids = {g.id for _, _, g in members}
        starts = [
            p.valid_from
            for m, _, _ in members
            for p in live_by_membership.get(m.id, [])
            if p.lessons_count is not None
        ]
        if since is not None:
            starts.append(since)
        earliest = min(starts)
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
                *([GroupAttendance.lesson_date <= until.isoformat()] if until is not None else []),
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
        group_of: Dict[UUID, Group] = {}
        for membership, student, group in members:
            sid = str(membership.student_id)
            dates: List[date] = []
            detail: List[tuple] = []
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
                    detail.append((lesson_day, lesson.lesson_id or "", lesson.id))
                    if lesson_day in moved_at:
                        on_moved_day.setdefault(lesson_day, []).append(str(lesson.lesson_id))
            out[membership.id] = dates
            if detail_out is not None:
                detail_out[membership.id] = detail
                group_of[membership.id] = group
            if late_out is not None and on_moved_day:
                counts: Dict[datetime, int] = {}
                for lesson_day, lesson_ids in on_moved_day.items():
                    starts = self._scheduled_starts(group, lesson_day)
                    for instant in moved_at[lesson_day]:
                        counts[instant] = sum(
                            1 for lid in lesson_ids if lid in starts and starts[lid] >= instant
                        )
                late_out[membership.id] = counts
        if detail_out is not None:
            self._order_by_start(detail_out, group_of)
        return out

    def _starts_of_days(
        self, wanted: Dict[Tuple[UUID, date], Group]
    ) -> Dict[Tuple[UUID, date], Dict[str, datetime]]:
        """``{(group id, day): {lesson_id: scheduled start}}`` of several (group, day) pairs, read together.

        One preload of the schedules (two queries) whatever the number of pairs - never per day.
        """
        if not wanted:
            return {}
        from .service import AuthService  # lazy: service is heavy

        days = [day for _, day in wanted]
        preload = AuthService(self.db)._preload_schedule_data(
            list({gid for gid, _ in wanted}), date_from=min(days), date_to=max(days)
        )
        return {
            (gid, day): self._scheduled_starts(group, day, preloaded=preload)
            for (gid, day), group in wanted.items()
        }

    def _order_by_start(self, detail_out: Dict[UUID, List[tuple]], group_of: Dict[UUID, Group]) -> None:
        """Put the lessons of one day of a student in hand-out order: scheduled start, then lesson id.

        Only days with SEVERAL counted lessons of the student need the schedule (usually none); it is
        read for all of them together (two queries), never per day.  A lesson whose time cannot be read
        counts as "before", like in ``_scheduled_starts``.
        """
        several: Dict[Tuple[UUID, date], Group] = {}
        for mid, items in detail_out.items():
            if mid not in group_of:
                continue
            seen: Dict[date, int] = {}
            for day, _lesson_id, _attendance in items:
                seen[day] = seen.get(day, 0) + 1
            for day, n in seen.items():
                if n > 1:
                    several[(group_of[mid].id, day)] = group_of[mid]
        starts = self._starts_of_days(several)
        for mid, items in detail_out.items():
            if mid not in group_of:
                continue
            gid = group_of[mid].id
            items.sort(
                key=lambda item: (item[0], starts.get((gid, item[0]), {}).get(item[1], datetime.min), item[1])
            )

    # ------------------------------------------------- coverage (payroll)

    def lesson_coverage(
        self,
        org_id: str,
        membership_ids: Iterable[UUID],
        first_day: date,
        last_day: date,
    ) -> Dict[Tuple[UUID, date, str], LessonCover]:
        """What pays for each counted lesson dated ``first_day .. last_day`` (N13 P3, payroll).

        ``{(membership_id, lesson_date, lesson_id): LessonCover}`` for every lesson that spends a
        payment by rule 4.3 (``_counted_lesson_dates``: completed, the student is in the roster
        snapshot, present / late / absent, or an excused mark without a confirmed excuse).  The money
        rules are the ones of the status page and nothing else:

        * calendar payment: the lesson date is inside ``valid_from .. PaymentRecord.end`` (a payment
          that moved away with the student is cut at the transfer day; one that moved before its
          period began covers nothing);
        * lessons package: the lesson is handed to the package by the FIFO of ``resolve_lesson_chains``
          (live capacities along carry-over chains, a lesson started after the transfer instant stays
          with the new group).  ``package_size`` is the size of the ORIGINAL package (the root of the
          chain) - the divisor of payroll for that lesson.

        A membership can hold payments of both modes (the centre changed its setting): the lesson is
        paid if EITHER kind covers it; if both do, the mode of the membership's latest payment (the one
        the status page reads) wins.  ``LessonCover.payment_id is None`` = counted but not paid.
        Cost: a fixed number of queries, whatever the number of memberships.
        """
        ids = {i for i in membership_ids if i}
        if not ids:
            return {}
        rows = (
            self.db.query(StudentGroup, Group)
            .join(Group, Group.id == StudentGroup.group_id)
            .filter(StudentGroup.id.in_(ids), Group.org_id == org_id)
            .all()
        )
        if not rows:
            return {}
        live: Dict[UUID, List[StudentPayment]] = {m.id: [] for m, _ in rows}
        for payment in (
            self.db.query(StudentPayment)
            .filter(
                StudentPayment.org_id == org_id,
                StudentPayment.membership_id.in_(list(live)),
                StudentPayment.voided_at.is_(None),
            )
            .all()
        ):
            live[payment.membership_id].append(payment)
        records = {mid: [self._record(p) for p in plist] for mid, plist in live.items()}

        detail: Dict[UUID, List[tuple]] = {}
        with_package = [(m, g) for m, g in rows if any(p.lessons_count is not None for p in live[m.id])]
        state = _LessonsState({}, {}, {})
        if with_package:
            state = self._lessons_state(
                org_id,
                with_package,
                {m.id: live[m.id] for m, _ in with_package},
                since=first_day,
                until=last_day,
                detail_out=detail,
            )
        package_ids = {m.id for m, _ in with_package}
        plain = [(m, None, g) for m, g in rows if m.id not in package_ids]
        if plain:
            self._counted_lesson_dates(
                org_id, plain, live, since=first_day, until=last_day, detail_out=detail
            )

        # Which lesson of a day went to which package: the FIFO hands out DATES; the lessons of one day
        # are told apart by their id, in the order of the payments of ``resolve_lesson_chains``.
        package_of: Dict[Tuple[UUID, date, str], Tuple[UUID, int]] = {}
        if state.taken:
            queues: Dict[Tuple[UUID, date], deque] = {}
            for mid, items in detail.items():
                for day, lesson_id, _attendance in items:        # already in hand-out order (start, id)
                    queues.setdefault((mid, day), deque()).append(lesson_id)
            for record in sorted(
                state.records.values(),
                key=lambda r: (r.valid_from, r.paid_on, r.created_at, str(r.id)),
            ):
                size = self._package_size(state.records, record)
                for day in state.taken.get(record.id, ()):
                    queue = queues.get((record.membership_id, day))
                    if queue:
                        package_of[(record.membership_id, day, queue.popleft())] = (record.id, size)

        out: Dict[Tuple[UUID, date, str], LessonCover] = {}
        for membership, _group in rows:
            recs = records[membership.id]
            # like compute_status: a calendar payment that moved away before its period began is nothing
            latest = ps.latest_payment([r for r in recs if not r.covers_nothing])
            package_first = latest is not None and latest.mode == ps.LESSONS
            latest_size = (
                self._package_size(state.records, state.records.get(latest.id, latest)) if package_first else None
            )
            calendar = sorted(
                (r for r in recs if r.lessons_count is None and not r.covers_nothing and r.end is not None),
                key=lambda r: (r.valid_from, r.paid_on, r.created_at, str(r.id)),
            )
            for day, lesson_id, _attendance in detail.get(membership.id, ()):
                if not first_day <= day <= last_day:
                    continue
                options: List[LessonCover] = []
                package = package_of.get((membership.id, day, lesson_id))
                if package is not None:
                    options.append(LessonCover(package[0], ps.LESSONS, package[1]))
                paid = next((r for r in calendar if r.valid_from <= day <= r.end), None)
                if paid is not None:
                    options.append(LessonCover(paid.id, ps.CALENDAR, None))
                if not package_first:
                    options.reverse()
                out[(membership.id, day, lesson_id)] = (
                    options[0] if options else LessonCover(None, None, latest_size)
                )
        return out

    @staticmethod
    def _package_size(records: Dict[UUID, ps.PaymentRecord], record: ps.PaymentRecord) -> int:
        """Size of the original package of a payment: ``lessons_count`` of the root of its carry-over chain."""
        root, hops = record, 0
        while root.carried_from_id in records and hops < 4 * MAX_CARRY_CHAIN:
            root, hops = records[root.carried_from_id], hops + 1
        return root.lessons_count

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
        files: Optional[Dict[UUID, List[PaymentFileOut]]] = None,
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
            receipt_no=payment.receipt_no,
            partial_period=bool(payment.partial_period),
            files=list(files.get(payment.id, ())) if files else [],
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
        files = load_files(self.db, [p.id for p in last.values() if p])
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
                    price=PriceOut(**pricing.price_dict(e.price)),
                    monthly_fee=e.price.amount,
                    status=r.status,
                    period_mode=r.period_mode,
                    paid_until=r.paid_until,
                    days_overdue=r.days_overdue,
                    days_left=r.days_left,
                    lessons_total=r.lessons_total,
                    lessons_used=r.lessons_used,
                    lessons_left=r.lessons_left,
                    last_payment=self._payment_out(payment, users, links, files) if payment else None,
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
        students_due: set = set()
        students_expired: set = set()
        for e in entries:
            counts[e.result.status] += 1
            # The same definitions as ``due`` / ``expired`` below, counted per student (the entries are already in memory).
            if e.result.status in (ps.EXPIRED, ps.ENDS_TODAY):
                students_due.add(e.student.id)
            if e.result.status == ps.EXPIRED:
                students_expired.add(e.student.id)
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
            students_due=len(students_due),
            students_expired=len(students_expired),
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
        files = load_files(self.db, [p.id for p in payments])
        return [self._payment_out(p, users, links, files) for p in payments]

    # -------------------------------------------------------------- writing

    def _membership(
        self, org_id: str, membership_id: UUID, *, lock: bool = False, writable: bool = False
    ) -> StudentGroup:
        """The membership of this organisation (``lock``: FOR NO KEY UPDATE).

        ``writable``: the caller is about to ENTER money (create / replace / set the fee): refused with 409
        when the student is archived.  A void is not "entering money" and does not ask for it.  The student's
        status is read by a statement of its own, AFTER the membership lock: the archiving holds the student
        and the membership together, so whoever wins the membership lock sees its outcome (the joined read of
        the first statement would still carry the snapshot from before the wait).  The student row is read,
        never locked here - the lock order student -> membership -> payment is not touched."""
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
        if writable:
            status = (
                self.db.query(Student.status)
                .filter(Student.id == membership.student_id)
                .scalar()
            )
            if status == "archived":
                raise StudentArchived(ARCHIVED_STUDENT_MESSAGE)
        return membership

    @staticmethod
    def _fixed_day(settings: PaymentSettings) -> bool:
        """The centre has calendar periods that start on one payment day (``billing_day``)."""
        return settings.period_mode == ps.CALENDAR and settings.billing_anchor == ps.FIXED_DAY

    @staticmethod
    def _membership_start(membership: StudentGroup) -> Optional[date]:
        """Business date (Tashkent) the student joined the group.

        ``student_groups.joined_at`` is a naive UTC instant (like ``left_at``), so it is read the way
        ``ps.business_date`` reads every naive moment; a joined_at of 22:00 UTC is already the next day.
        """
        return ps.business_date(membership.joined_at) if membership.joined_at else None

    def _live_payments(self, org_id: str, membership_id: UUID) -> List[StudentPayment]:
        return (
            self.db.query(StudentPayment)
            .filter(
                StudentPayment.org_id == org_id,
                StudentPayment.membership_id == membership_id,
                StudentPayment.voided_at.is_(None),
            )
            .all()
        )

    def _resolve_period(
        self,
        *,
        paid_on: date,
        valid_from: Optional[date],
        valid_to: Optional[date],
        lessons_count: Optional[int],
        settings: PaymentSettings,
        live: Sequence[StudentPayment],
        today: date,
        membership_start: Optional[date] = None,
    ) -> tuple[date, Optional[date], Optional[int]]:
        """The period of a payment: what the client sent, the rest from the centre's settings.

        The one place that knows the defaults - ``create_payment``, ``replace_payment`` and ``quote`` all
        come here, so a suggestion can never differ from what is saved.  With one payment day for the
        centre (``fixed_day``) a calendar payment starts at ``default_fixed_day_period`` and ends with the
        centre's period of its start; whatever the client sent is used as it is, with the same checks.
        """
        if valid_to is not None and lessons_count is not None:
            raise PaymentInvalid("Укажите что-то одно: «оплачено до» или число уроков")
        mode = (
            ps.CALENDAR if valid_to is not None
            else ps.LESSONS if lessons_count is not None
            else settings.period_mode
        )
        fixed = mode == ps.CALENDAR and self._fixed_day(settings)
        # What the live payments still cover in this membership (a moved-away original ends the day before).
        previous_until = max(
            (r.end for r in map(self._record, live) if r.end is not None and not r.covers_nothing),
            default=None,
        )
        # The payment date and the start are bounded first: every default
        # below does date arithmetic that must never see 0001 or 9999.
        errors = ps.validation_errors(
            today=today, paid_on=paid_on, valid_from=valid_from or paid_on, valid_to=None,
        )
        if errors:
            raise PaymentInvalid(errors[0])
        try:
            if valid_from is not None:
                start = valid_from
            elif fixed:
                start, _ = ps.default_fixed_day_period(
                    paid_on, previous_until, membership_start, settings.billing_day
                )
            else:
                start = ps.default_valid_from(mode, paid_on, previous_until)
        except OverflowError:
            raise PaymentInvalid("Начало периода слишком далеко в будущем")
        problem = ps.valid_from_error(paid_on, start)
        if problem:
            raise PaymentInvalid(problem)
        if mode == ps.CALENDAR:
            try:
                if valid_to is not None:
                    end = valid_to
                elif fixed:
                    end = ps.billing_period_end(start, settings.billing_day)
                else:
                    end = ps.add_month_minus_day(start)
            except OverflowError:
                raise PaymentInvalid("Период выходит за допустимые даты")
            count = None
        else:
            end = None
            count = lessons_count or settings.lessons_per_payment
        errors = ps.validation_errors(today=today, paid_on=paid_on, valid_from=start, valid_to=end)
        if errors:
            raise PaymentInvalid(errors[0])
        return start, end, count

    def _partial_flag(self, settings: PaymentSettings, valid_from: date, valid_to: Optional[date]) -> bool:
        """``student_payments.partial_period``: set from the settings of the moment of the payment."""
        return (
            valid_to is not None
            and self._fixed_day(settings)
            and ps.is_partial_period(valid_from, valid_to, settings.billing_day)
        )

    def _next_receipt_no(self, org_id: str) -> int:
        """``max + 1`` inside the organisation; voided payments keep their numbers, so none is reused."""
        current = (
            self.db.query(func.max(StudentPayment.receipt_no))
            .filter(StudentPayment.org_id == org_id, StudentPayment.receipt_no.isnot(None))
            .scalar()
        )
        return int(current or 0) + 1

    def _add_with_receipt(self, org_id: str, build) -> StudentPayment:
        """Insert the payment ``build(receipt_no)`` makes under the next receipt number.

        The number is ``max + 1`` taken inside a SAVEPOINT; two concurrent payments of one centre may pick
        the same one, the unique index refuses the second, and it retries with the new maximum (at most
        ``RECEIPT_ATTEMPTS`` times).  No lock on the organisation: the insert waits only for a concurrent
        insert of the same number, which never waits for anything this transaction holds.
        """
        for attempt in range(1, RECEIPT_ATTEMPTS + 1):
            payment = build(self._next_receipt_no(org_id))
            try:
                with self.db.begin_nested():
                    self.db.add(payment)
                    self.db.flush()
                return payment
            except IntegrityError as exc:
                reason = str(exc.orig)
                if UNIQUE_DUP_INDEX in reason:  # a concurrent identical request slipped past the check
                    raise DuplicatePayment("Такая оплата уже внесена") from exc
                if RECEIPT_INDEX not in reason:
                    raise
                logger.info("receipt number taken by a concurrent payment (attempt %s)", attempt)
        raise PaymentConflict("Не удалось присвоить номер квитанции. Повторите.")

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
        live = self._live_payments(org_id, membership.id)
        valid_from, valid_to, lessons_count = self._resolve_period(
            paid_on=data.paid_on, valid_from=data.valid_from, valid_to=data.valid_to,
            lessons_count=data.lessons_count, settings=settings, live=live, today=today,
            membership_start=self._membership_start(membership),
        )

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

        partial = self._partial_flag(settings, valid_from, valid_to)
        created_at = _utcnow()
        payment = self._add_with_receipt(
            org_id,
            lambda receipt_no: StudentPayment(
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
                receipt_no=receipt_no,
                partial_period=partial,
                created_by=UUID(str(actor_id)),
                created_at=created_at,
            ),
        )

        warnings = [
            PaymentWarningOut(code=w.code, message=w.message)
            for w in ps.payment_warnings(
                today=today,
                paid_on=data.paid_on,
                valid_from=valid_from,
                valid_to=valid_to,
                existing=[self._record(p) for p in live],
                amount=data.amount,
                # the price at the start of the paid period, like the amount hint
                monthly_fee=pricing.effective_price(self.db, membership, valid_from).amount,
            )
        ]
        return payment, warnings

    def _created(self, org_id: str, payment: StudentPayment, warnings) -> PaymentCreated:
        users = self._users([payment.created_by])
        return PaymentCreated(
            payment=self._payment_out(
                payment, users, self._links([payment]), load_files(self.db, [payment.id])
            ),
            warnings=warnings,
            row=self._row(org_id, payment.membership_id),
        )

    @deadlock_guard
    def create_payment(
        self, org_id: str, membership_id: UUID, actor_id: str, data: PaymentCreate
    ) -> PaymentCreated:
        try:
            membership = self._membership(org_id, membership_id, lock=True, writable=True)
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
            membership = self._membership(org_id, probe.membership_id, lock=True, writable=True)
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
            payment=self._payment_out(
                payment, users, self._links([payment]), load_files(self.db, [payment.id])
            ),
            row=self._row(org_id, payment.membership_id),
        )

    # ------------------------------------------------ amount hint (N13 P2.2)

    def _group_lesson_days(self, group: Group, start: date, end: date) -> List[date]:
        """Dates of the group's lessons in ``start .. end`` by the schedule: the regular schedule, moved and
        one-off lessons, minus the lessons cancelled in the journal (``group_attendance.status =
        'cancelled'``).  One expansion of the schedule and one query for the cancelled rows."""
        from .service import AuthService  # lazy: service is heavy

        occurrences = AuthService(self.db)._resolved_occurrences_for_group(
            group, date_from=start, date_to=end
        )
        if not occurrences:
            return []
        cancelled = {
            (row.lesson_date, row.lesson_id)
            for row in self.db.query(GroupAttendance.lesson_date, GroupAttendance.lesson_id).filter(
                GroupAttendance.group_id == group.id,
                GroupAttendance.status == "cancelled",
                GroupAttendance.lesson_date >= start.isoformat(),
                GroupAttendance.lesson_date <= end.isoformat(),
            )
        }
        return [
            date.fromisoformat(item["occurrenceDate"])
            for item in occurrences
            if (item["occurrenceDate"], str(item["id"])) not in cancelled
        ]

    def quote(
        self,
        org_id: str,
        membership_id: UUID,
        *,
        paid_on: date,
        valid_from: Optional[date] = None,
        valid_to: Optional[date] = None,
        lessons_count: Optional[int] = None,
        monthly_fee: Optional[int] = None,
    ) -> PaymentQuote:
        """Suggested dates and amount for a payment of this membership.  Reads only, writes nothing.

        The base is the effective price of the membership (group price or individual price, ``pricing.py``) ON THE
        FIRST DAY OF THE PERIOD being paid.  ``monthly_fee`` (a sum typed in the form) replaces it for this
        calculation only - a one-off correction of the suggestion, nothing is saved; the answer's ``monthly_fee``
        is the sum that was actually used and ``price`` the price at the start of the period.

        The dates come from ``_resolve_period`` - the code that saves a payment - and fail with the same 422
        texts.  The amount rules (``PaymentSettings`` of the centre, the price above):

        * no fee -> no amount (``basis`` ``none``);
        * a lessons package: the fee for the centre's package size, else ``fee * lessons / package size``
          rounded to 1 000 (``package``);
        * calendar, from the payment date: the standard period ``add_month_minus_day`` -> the fee (``full``),
          any other -> no amount (``custom``);
        * calendar, one payment day: period by period of the centre, a complete one costs the fee, an incomplete
          one ``fee * lessons in the part / lessons in the period`` by the group's schedule (``lessons``), or by
          calendar days when the period has no lessons (``days``); the parts are rounded to 1 000 and added.
        """
        membership = self._membership(org_id, membership_id)
        settings = self.get_settings(org_id)
        today = ps.business_today()
        start, end, count = self._resolve_period(
            paid_on=paid_on, valid_from=valid_from, valid_to=valid_to, lessons_count=lessons_count,
            settings=settings, live=self._live_payments(org_id, membership.id), today=today,
            membership_start=self._membership_start(membership),
        )
        price = pricing.effective_price(self.db, membership, start)
        fee = int(monthly_fee) if monthly_fee is not None else price.amount
        # "Оплата в месяц" for a calendar period, "Стоимость пакета" for a lessons package.
        price_name = "Оплата в месяц" if end is not None else "Стоимость пакета"
        fixed = end is not None and self._fixed_day(settings)
        partial = fixed and ps.is_partial_period(start, end, settings.billing_day)
        parts: List[ps.QuotePart] = []
        amount: Optional[int] = None

        if fee is None:
            basis, explanation = "none", f"{price_name} не задана"
        elif end is None:  # a lessons package
            per = settings.lessons_per_payment
            amount = fee if count == per else ps.round_1000(fee * count, per)
            basis = "package"
            explanation = (
                f"Пакет из {count} {_plural_lessons(count)} — {_sum(amount)}"
                if count == per
                else f"Пакет из {count} {_plural_lessons(count)} вместо {per}: {_sum(amount)} из {_num(fee)}"
            )
        elif not fixed:
            try:
                standard = ps.add_month_minus_day(start) == end
            except OverflowError:
                standard = False
            if standard:
                amount, basis, explanation = fee, "full", f"Полный месяц — {_sum(fee)}"
            else:
                basis, explanation = "custom", "Нестандартный период — укажите сумму"
        else:
            group = self.db.query(Group).filter(Group.id == membership.group_id, Group.org_id == org_id).first()
            parts = ps.price_fixed_day(
                start, end, settings.billing_day, fee,
                (lambda a, b: self._group_lesson_days(group, a, b)) if group else (lambda a, b: []),
            )
            total = sum(part.amount for part in parts)
            if total > MAX_MONEY:
                parts, basis, explanation = [], "custom", "Сумма слишком большая — укажите её вручную"
            else:
                amount, basis = total, ps.quote_basis(parts)
                explanation = self._explain_parts(parts, fee, total, today)
        if fee == 0 and amount == 0:
            explanation = f"{price_name} равна 0 — занятия бесплатные"

        return PaymentQuote(
            mode=ps.CALENDAR if end is not None else ps.LESSONS,
            billing_anchor=settings.billing_anchor,
            billing_day=settings.billing_day,
            paid_on=paid_on,
            valid_from=start,
            valid_to=end,
            lessons_count=count,
            partial=bool(partial),
            monthly_fee=fee,
            price=PriceOut(**pricing.price_dict(price)),
            suggested_amount=amount,
            basis=basis,
            parts=[
                QuotePartOut(
                    from_=part.start, to=part.end, period_from=part.period_start, period_to=part.period_end,
                    lessons=part.lessons, period_lessons=part.period_lessons, amount=part.amount,
                )
                for part in parts
            ],
            explanation=explanation,
        )

    @staticmethod
    def _explain_parts(parts: Sequence[ps.QuotePart], fee: int, total: int, today: date) -> str:
        """The caption under the amount, in Russian."""
        if len(parts) == 1:
            part = parts[0]
            if part.basis == ps.BASIS_FULL:
                return f"Полный месяц — {_sum(fee)}"
            if part.basis == ps.BASIS_LESSONS:
                return (
                    f"Неполный месяц: {part.lessons} {_plural_lessons(part.lessons)} из {part.period_lessons} "
                    f"по расписанию — {_sum(part.amount)} из {_num(fee)}"
                )
            covered = (part.end - part.start).days + 1
            whole = (part.period_end - part.period_start).days + 1
            return (
                f"Неполный месяц: {covered} {_plural_days(covered)} из {whole} (уроков по расписанию нет, "
                f"считаем по дням) — {_sum(part.amount)} из {_num(fee)}"
            )
        if all(part.basis == ps.BASIS_FULL for part in parts):
            return f"Полных месяцев: {len(parts)} — {_sum(total)}"
        lines = []
        for part in parts:
            span = f"{_short_date(part.start, today)}–{_short_date(part.end, today)}"
            if part.basis == ps.BASIS_FULL:
                lines.append(f"{span}: полный месяц — {_sum(part.amount)}")
            elif part.basis == ps.BASIS_LESSONS:
                lines.append(
                    f"{span}: {part.lessons} {_plural_lessons(part.lessons)} из {part.period_lessons} "
                    f"— {_sum(part.amount)}"
                )
            else:
                covered = (part.end - part.start).days + 1
                whole = (part.period_end - part.period_start).days + 1
                lines.append(
                    f"{span}: {covered} {_plural_days(covered)} из {whole} (по дням) — {_sum(part.amount)}"
                )
        return "; ".join(lines) + f". Итого {_sum(total)}"

    # ------------------------------------------- receipts and cheques (N13 P2.2)

    def receipt(self, org_id: str, payment_id: UUID) -> ReceiptOut:
        """What the printed receipt shows.  A carry-over is not money: 409."""
        payment = self._probe_payment(org_id, payment_id)
        if payment.carried_from_payment_id is not None:
            raise PaymentConflict("Перенос оплаты — не денежная операция, квитанции нет")
        if payment.receipt_no is None:  # only a row written around the application
            raise PaymentConflict("У этой оплаты нет номера квитанции")
        org = self.db.query(Organization).filter(Organization.id == org_id).first()
        student = self.db.query(Student).filter(Student.id == payment.student_id, Student.org_id == org_id).first()
        group = self.db.query(Group).filter(Group.id == payment.group_id, Group.org_id == org_id).first()
        if not (org and student and group):
            raise PaymentNotFound("Оплата не найдена")
        # The correction chain: this payment replaced another one / was replaced by another one.
        replaced_by = (
            self.db.query(StudentPayment.receipt_no)
            .filter(
                StudentPayment.org_id == org_id,
                StudentPayment.membership_id == payment.membership_id,  # a correction stays in its membership
                StudentPayment.replaces_payment_id == payment.id,
            )
            .order_by(StudentPayment.created_at.desc(), StudentPayment.id)
            .first()
        )
        replaces = (
            self.db.query(StudentPayment.receipt_no)
            .filter(StudentPayment.org_id == org_id, StudentPayment.id == payment.replaces_payment_id)
            .first()
            if payment.replaces_payment_id
            else None
        )
        users = self._users([payment.created_by])
        return ReceiptOut(
            receipt_no=payment.receipt_no,
            org_name=org.name,
            org_city=(org.city or "").strip() or None,
            org_phone=(org.phone or "").strip() or None,
            student_name=person_name(student.last_name, student.first_name) or "",
            display_id=student.display_id,
            group_name=group.name,
            amount=int(payment.amount),
            amount_words=amount_words(int(payment.amount)),
            currency=payment.currency,
            paid_on=payment.paid_on,
            valid_from=payment.valid_from,
            valid_to=payment.valid_to,
            lessons_count=payment.lessons_count,
            partial_period=bool(payment.partial_period),
            note=payment.note,
            accepted_by_name=_person(users.get(payment.created_by)),
            created_at=_iso(payment.created_at),
            voided_at=_iso(payment.voided_at),
            void_reason=payment.void_reason,
            replaced_by_receipt_no=replaced_by[0] if replaced_by else None,
            replaces_receipt_no=replaces[0] if replaces else None,
        )

    def precheck_file_upload(self, org_id: str, payment_id: UUID) -> None:
        """Whether a cheque could be attached right now, WITHOUT a lock - asked before the upload body is
        parsed, so a request that cannot succeed costs nothing.  The authoritative check is ``add_file``
        under the row lock; this one only answers early: unknown payment 404, carry-over / voided / three
        cheques already 409."""
        payment = (
            self.db.query(StudentPayment)
            .filter(StudentPayment.id == payment_id, StudentPayment.org_id == org_id)
            .populate_existing()
            .first()
        )
        if payment is None:
            raise PaymentNotFound("Оплата не найдена")
        if payment.carried_from_payment_id is not None:
            raise PaymentConflict(FILE_ON_CARRY_OVER)
        if payment.voided_at is not None:
            raise PaymentConflict(FILE_ON_VOIDED)
        count = (
            self.db.query(func.count(StudentPaymentFile.id))
            .filter(StudentPaymentFile.payment_id == payment.id)
            .scalar()
            or 0
        )
        if count >= MAX_FILES_PER_PAYMENT:
            raise PaymentConflict(FILE_LIMIT)

    @deadlock_guard
    def add_file(
        self, org_id: str, payment_id: UUID, actor_id: str, data: bytes, filename: Optional[str]
    ) -> PaymentFileOut:
        """Attach a cheque (JPEG / PNG / PDF, up to 2 MB, up to 3 per payment) to a live payment."""
        payment_id = UUID(str(payment_id))
        try:
            probe = self._probe_payment(org_id, payment_id)
            if probe.carried_from_payment_id is not None:
                raise PaymentConflict(FILE_ON_CARRY_OVER)
            # Serialises uploads to one payment (the count below) with a void / replace of it.  Only this
            # row is locked, FOR NO KEY UPDATE: the insert of the file (a foreign key = FOR KEY SHARE) does
            # not wait for it, and nothing else is taken afterwards, so no lock order can be inverted.
            payment = (
                self.db.query(StudentPayment)
                .filter(StudentPayment.id == payment_id, StudentPayment.org_id == org_id)
                .populate_existing()
                .with_for_update(key_share=True)
                .first()
            )
            if payment is None:
                raise PaymentNotFound("Оплата не найдена")
            if payment.voided_at is not None:
                raise PaymentConflict(FILE_ON_VOIDED)
            if not data:
                raise PaymentInvalid("Файл пустой")
            if len(data) > MAX_FILE_BYTES:
                raise PaymentFileTooLarge("Файл слишком большой (макс. 2 МБ)")
            content_type = sniff_content_type(data)
            if not content_type:
                raise PaymentFileUnsupported("Допустимы только файлы JPEG, PNG или PDF")
            count = (
                self.db.query(func.count(StudentPaymentFile.id))
                .filter(StudentPaymentFile.payment_id == payment.id)
                .scalar()
                or 0
            )
            if count >= MAX_FILES_PER_PAYMENT:
                raise PaymentConflict(FILE_LIMIT)
            file_id = uuid4()
            stored = StudentPaymentFile(
                id=file_id,
                org_id=UUID(str(org_id)),
                payment_id=payment.id,
                filename=stored_filename(filename, content_type),
                content_type=content_type,
                size_bytes=len(data),
                data=data,
                uploaded_by=UUID(str(actor_id)),
                uploaded_at=_utcnow(),
            )
            self.db.add(stored)
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        logger.info("payment file added id=%s payment=%s actor=%s", file_id, payment_id, actor_id)
        return next(f for f in load_files(self.db, [payment_id]).get(payment_id, ()) if f.id == str(file_id))

    def get_file(self, org_id: str, payment_id: UUID, file_id: UUID) -> StudentPaymentFile:
        """The cheque with its bytes (the payment must be this centre's and the file this payment's)."""
        stored = (
            self.db.query(StudentPaymentFile)
            .filter(
                StudentPaymentFile.id == file_id,
                StudentPaymentFile.payment_id == payment_id,
                StudentPaymentFile.org_id == org_id,
            )
            .first()
        )
        if not stored:
            raise PaymentNotFound("Файл не найден")
        return stored

    @deadlock_guard
    def delete_file(
        self, org_id: str, payment_id: UUID, file_id: UUID, actor_id: str, *, is_owner: bool
    ) -> None:
        """The owner, or whoever uploaded the cheque, may remove it (the bytes are not loaded)."""
        stored = (
            self.db.query(StudentPaymentFile)
            .options(defer(StudentPaymentFile.data))
            .filter(
                StudentPaymentFile.id == file_id,
                StudentPaymentFile.payment_id == payment_id,
                StudentPaymentFile.org_id == org_id,
            )
            .first()
        )
        if not stored:
            raise PaymentNotFound("Файл не найден")
        if not is_owner and str(stored.uploaded_by) != str(actor_id):
            raise PaymentForbidden("Удалить чек может владелец или тот, кто его загрузил")
        self.db.delete(stored)
        self.db.commit()
        logger.info("payment file removed id=%s payment=%s actor=%s", file_id, payment_id, actor_id)

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

    def carry_over_on_transfer(
        self,
        org_id: str,
        actor_id: Optional[str],
        sources: Sequence[StudentGroup],
        target: StudentGroup,
        *,
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

        The price is NOT carried (#105): the student takes the price of the new group, an individual price of the
        closed group stays with that membership.  Returns the ``warnings`` entries for the response: dates / lessons
        when ``detailed`` (the caller may see payments), else one generic sentence; no amount in either.
        """
        sources = [m for m in sources if m.id != target.id]
        if not sources:
            return []
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
                partial_period=bool(c.original.partial_period),  # a carry-over has no receipt number
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
            partial_period=bool(new_parent.partial_period),
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
