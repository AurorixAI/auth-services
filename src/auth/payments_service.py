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
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
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


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


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
        lesson_dates = self._counted_lesson_dates(org_id, lessons_members, live_by_membership)

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
        )

    def _counted_lesson_dates(
        self,
        org_id: str,
        members: Sequence[tuple],
        live_by_membership: Dict[UUID, List[StudentPayment]],
    ) -> Dict[UUID, List[date]]:
        """Dates of the lessons that spend a lessons payment, per membership.

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
            for lesson, roster in by_group.get(group.id, ()):
                if roster is not None and sid not in roster:
                    continue
                records = lesson.records if isinstance(lesson.records, dict) else {}
                excuse = "confirmed" if (lesson.id, sid) in confirmed else None
                if ps.lesson_counts(mark=records.get(sid), excuse_status=excuse):
                    try:
                        dates.append(date.fromisoformat(lesson.lesson_date))
                    except ValueError:
                        logger.warning("Skipping lesson %s with malformed date", lesson.id)
            out[membership.id] = dates
        return out

    # ---------------------------------------------------------- serialising

    def _users(self, ids: Iterable[Optional[UUID]]) -> Dict[UUID, User]:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        return {u.id: u for u in self.db.query(User).filter(User.id.in_(wanted)).all()}

    def _payment_out(self, payment: StudentPayment, users: Dict[UUID, User]) -> PaymentOut:
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
                    last_payment=self._payment_out(payment, users) if payment else None,
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
        return [self._payment_out(p, users) for p in payments]

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
            query = query.with_for_update(of=StudentGroup)
        membership = query.first()
        if not membership:
            raise PaymentNotFound("Членство не найдено")
        return membership

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
        previous_until = max((p.valid_to for p in live if p.valid_to is not None), default=None)
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
            payment=self._payment_out(payment, users),
            warnings=warnings,
            row=self._row(org_id, payment.membership_id),
        )

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

    def _lock_payment(self, org_id: str, payment_id: UUID) -> StudentPayment:
        payment = (
            self.db.query(StudentPayment)
            .filter(StudentPayment.id == payment_id, StudentPayment.org_id == org_id)
            .with_for_update()
            .first()
        )
        if not payment:
            raise PaymentNotFound("Оплата не найдена")
        if payment.voided_at is not None:
            raise AlreadyVoided("Оплата уже отменена")
        return payment

    def replace_payment(
        self,
        org_id: str,
        payment_id: UUID,
        actor_id: str,
        data: PaymentCreate,
        reason: Optional[str] = None,
    ) -> PaymentCreated:
        """Void the old payment and insert the corrected one - one transaction."""
        try:
            old = self._lock_payment(org_id, payment_id)
            membership = self._membership(org_id, old.membership_id, lock=True)
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
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        logger.info(
            "payment replaced old=%s new=%s membership=%s actor=%s",
            payment_id, payment.id, membership.id, actor_id,
        )
        return self._created(org_id, payment, warnings)

    def void_payment(
        self, org_id: str, payment_id: UUID, actor_id: str, reason: str
    ) -> PaymentVoided:
        try:
            payment = self._lock_payment(org_id, payment_id)
            payment.voided_at = _utcnow()
            payment.voided_by = UUID(str(actor_id))
            payment.void_reason = reason
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        logger.info("payment voided id=%s actor=%s", payment_id, actor_id)
        users = self._users([payment.created_by, payment.voided_by])
        return PaymentVoided(
            payment=self._payment_out(payment, users),
            row=self._row(org_id, payment.membership_id),
        )
