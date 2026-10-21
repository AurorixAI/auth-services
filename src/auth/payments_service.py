"""
ALGO - Student payments (N13 P2, PAY-2 "деньги покупают дни").

Money lives only here and in ``payments_router.py``; no student, group or profile payload carries an amount.
A payment is a SUM (``StudentPayment.amount``, a date, a method, a note); everything else - the charges of the periods, where
every payment went, "paid until", debt, advance, "is this lesson paid" - is derived by ``billing_ledger.run_ledger`` from the
facts (payments, write-offs, refunds, transfer links, stays, prices, settings).  ``ledger_service`` loads the facts in a bounded
number of queries; ``payments_views`` renders the answer in the frozen contract (``payments_schemas``).

* Nothing is ever deleted: ``void`` cancels, ``replace`` = void + insert in one transaction.
* Logs carry ids and the actor, never amounts or notes.
* Locks, in this order everywhere: student -> memberships of the student by id (the family of transfers) -> payment.  Creating a
  payment locks only its membership (money added never breaks another invariant); void / replace / write-off / refund / their
  cancellation lock the whole family and re-check after the lock.  Membership locks are FOR NO KEY UPDATE so that inserting a
  payment (foreign key: FOR KEY SHARE) does not wait.  A deadlock PostgreSQL still finds is a 409 (``deadlock_guard``).
* A transfer between groups stores a LINK (``membership_transfers``); the money that travels is derived on the transfer day, so
  correcting a payment in the old group changes what arrived in the new one (``carry_over_on_transfer`` records the link).
* Receipt numbers: ``max + 1`` inside the organisation, taken in a SAVEPOINT and retried on a unique-index conflict.  Every
  payment gets a snapshot of "where the money went at the moment" (``payment_receipt_snapshots``) for the printed receipt.
* Cheques (photos / PDFs) are kept in ``student_payment_files`` like the certificates of excuses.
"""
from __future__ import annotations

import functools
import logging
import os
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

from sqlalchemy import event, func, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, defer
from sqlalchemy.orm.attributes import flag_modified

from src.models.auth import (
    AttendanceExcuse,
    Group,
    GroupAttendance,
    GroupAudit,
    GroupPrice,
    MembershipPriceOverride,
    MembershipTransfer,
    Organization,
    PaymentReceiptSnapshot,
    Student,
    StudentGroup,
    StudentGroupPeriod,
    StudentPayment,
    StudentPaymentFile,
    StudentRefund,
    StudentWriteoff,
    User,
)

from . import billing_ledger as bl
from . import ledger_service as ls
from . import payment_status as ps
from . import payments_texts as tx
from . import payments_views as pv
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
    AccountingStartImpact,
    CarriedFromOut,
    CarriedOutOut,
    MembershipPaymentRow,
    PaymentCreate,
    PaymentCreated,
    PaymentFileOut,
    PaymentOut,
    PaymentPreview,
    PaymentQuote,
    PaymentSettings,
    PaymentSettingsChanged,
    PaymentSettingsUpdate,
    PaymentsSummary,
    PaymentStatusPage,
    PaymentVoided,
    PaymentWarningOut,
    PriceOut,
    ReceiptOut,
    ReceiptSnapshot,
    RefundCreated,
    RefundOut,
    RefundVoided,
    SnapshotPrice,
    WriteoffCreated,
    WriteoffOut,
    WriteoffVoided,
)

logger = logging.getLogger(__name__)

DEFAULT_REPLACE_REASON = "Исправлено"
RECEIPT_INDEX = "uq_student_payments_receipt_no"
IDEMPOTENCY_INDEX = "uq_student_payments_idempotency"
FILE_ON_CARRY_OVER = "К переносу оплаты чек не прикрепляется — это не денежная операция"
FILE_ON_VOIDED = "Оплата аннулирована — чек прикрепить нельзя"
FILE_LIMIT = f"К оплате можно приложить не более {MAX_FILES_PER_PAYMENT} файлов"
RECEIPT_ATTEMPTS = 10  # max+1 under a SAVEPOINT; a conflict with a concurrent payment takes the next number
SOFT_DUPLICATE_SECONDS = 60  # the same student + date + sum within a minute is a double click
MAX_PAID_AGO_YEARS = ps.MAX_PAID_AGO_YEARS
LARGE_OVERPAY_PERIODS = 3  # an advance of more than this many periods asks "are you sure"
WARN_PAID_AGO_DAYS = ps.WARN_PAID_AGO_DAYS
ACCOUNTING_START_AHEAD_DAYS = 31
ACCOUNTING_START_MIN = date(2020, 1, 1)

# a transfer between groups (the warning of the response of ``add_student_to_group``)
CARRY_CODE = "payment_carried_over"
CARRY_GENERIC_MESSAGE = "Остаток оплаты ученика перенесён в новую группу"

# the audit trail of write-offs and refunds lives in ``group_audit``
AUDIT_WRITEOFF = "payment_writeoff"
AUDIT_WRITEOFF_VOID = "payment_writeoff_void"
AUDIT_REFUND = "payment_refund"
AUDIT_REFUND_VOID = "payment_refund_void"

# /org/payments/summary is asked by the banner and the menu of every administrator: a few seconds of cache.
SUMMARY_TTL_SECONDS = float(os.getenv("PAYMENTS_SUMMARY_TTL", "15"))
_SUMMARY_CACHE: Dict[str, Tuple[float, PaymentsSummary]] = {}


def invalidate_summary(org_id: Any = None) -> None:
    """Forget the cached summary (any write of money / price / settings of the centre)."""
    if org_id is None:
        _SUMMARY_CACHE.clear()
    else:
        _SUMMARY_CACHE.pop(str(org_id), None)


# Every write that can change a ledger drops the cached summary - ONE hook on the session instead of a call in every write
# path: a flush (or a bulk update / delete) that touches prices, memberships and their stays, money records, transfers, the
# journal (lessons), excuses, the students / groups or the organisation (settings) marks the session; the commit that follows
# forgets the cache of all centres (cheap; the summary is rebuilt on the next ask).
_LEDGER_MODELS = (
    GroupPrice, MembershipPriceOverride, StudentGroup, StudentGroupPeriod, StudentPayment, StudentWriteoff, StudentRefund,
    MembershipTransfer, GroupAttendance, AttendanceExcuse, Student, Group, Organization,
)
_DIRTY_FLAG = "payments_summary_dirty"


@event.listens_for(Session, "after_flush")
def _mark_ledger_writes(session, flush_context):
    if any(isinstance(o, _LEDGER_MODELS) for group in (session.new, session.dirty, session.deleted) for o in group):
        session.info[_DIRTY_FLAG] = True


@event.listens_for(Session, "after_bulk_update")
@event.listens_for(Session, "after_bulk_delete")
def _mark_ledger_bulk_writes(context):
    mapper = getattr(context, "mapper", None)
    if mapper is not None and issubclass(mapper.class_, _LEDGER_MODELS):
        context.session.info[_DIRTY_FLAG] = True


@event.listens_for(Session, "after_commit")
def _forget_summary_after_commit(session):
    if session.info.pop(_DIRTY_FLAG, False):
        _SUMMARY_CACHE.clear()


@event.listens_for(Session, "after_rollback")
def _forget_mark_after_rollback(session):
    session.info.pop(_DIRTY_FLAG, None)


# ============================================================================
# Errors (mapped to HTTP by the router)
# ============================================================================


class PaymentError(Exception):
    status_code = 400
    code: Optional[str] = None

    def __init__(self, message: str, **extra: Any) -> None:
        self.message = message
        self.extra = extra
        super().__init__(message)

    @property
    def detail(self) -> Any:
        if not self.code:
            return self.message
        return {"code": self.code, "message": self.message, **self.extra}


class PaymentNotFound(PaymentError):
    status_code = 404


class PaymentConflict(PaymentError):
    status_code = 409


class DuplicatePayment(PaymentConflict):
    code = "DUPLICATE_PAYMENT"


class AlreadyVoided(PaymentConflict):
    code = "ALREADY_VOIDED"


class VoidBreaksRefund(PaymentConflict):
    code = "VOID_BREAKS_REFUND"


class PaymentInvalid(PaymentError):
    status_code = 422


class AmountRange(PaymentInvalid):
    code = "AMOUNT_RANGE"


class PaidOnRange(PaymentInvalid):
    code = "PAID_ON_RANGE"


class MethodRequired(PaymentInvalid):
    code = "METHOD_REQUIRED"


class ReasonRequired(PaymentInvalid):
    code = "REASON_REQUIRED"


class WriteoffExceedsDebt(PaymentInvalid):
    code = "WRITEOFF_EXCEEDS_DEBT"


class RefundExceedsAdvance(PaymentInvalid):
    code = "REFUND_EXCEEDS_ADVANCE"


class AccountingStartRange(PaymentInvalid):
    code = "ACCOUNTING_START_RANGE"


class RangeTooLong(PaymentInvalid):
    code = "RANGE_TOO_LONG"


class StudentArchived(PaymentConflict):
    """The student left the centre: money is not entered on his closed memberships until he is brought back
    (a payment can still be voided, and the history can be read)."""

    code = "STUDENT_ARCHIVED"


class ConcurrentEdit(PaymentConflict):
    """A deadlock with another edit of the same student; nothing was written, repeat the request."""

    code = "CONCURRENT_EDIT"


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
    """Locks are taken in one order (student -> memberships -> payment); should PostgreSQL still find a deadlock, the caller
    gets 409, never 500."""

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
    return tx.lessons_word(count)


def _plural_days(count: int) -> str:
    return tx.days_word(count)


def _num(amount: int) -> str:
    """``250 000`` (a plain space between the thousands)."""
    return tx.num(amount)


def _sum(amount: int) -> str:
    """``250 000 сум``."""
    return tx.money(amount)


def _short_date(day: date, today: date) -> str:
    """``10.11`` within the current year, ``10.11.2027`` otherwise."""
    return day.strftime("%d.%m") if day.year == today.year else day.strftime("%d.%m.%Y")


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _years_ago(day: date, years: int) -> date:
    return ps._years_ago(day, years)


@dataclass
class _Links:
    """Carry links of a batch of payments (LEGACY rows made before PAY-2), for the response."""

    sources: Dict[UUID, CarriedFromOut]  # original id -> {payment_id, group_name}
    targets: Dict[UUID, Optional[str]]  # stamped original id -> group its live carry-over lives in


@dataclass(frozen=True)
class LessonCover:
    """Whether one counted lesson is paid (``PaymentService.lesson_coverage``, read by payroll)."""

    payment_id: Optional[UUID]  # always None since PAY-2 (a lesson is covered by the account, not by one payment)
    mode: Optional[str]  # ``payment_status.CALENDAR`` / ``LESSONS``
    # Lessons mode: the size of the package of the regime the lesson belongs to (the divisor of payroll); calendar: None.
    package_size: Optional[int]
    paid: bool = False  # the lesson is paid by the money of the account (or lies before the first charged day)


# ============================================================================
# Service
# ============================================================================


class PaymentService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------ settings

    def _org_settings(self, org_id: str) -> Tuple[Dict[str, Any], Optional[datetime]]:
        row = self.db.query(Organization.settings, Organization.created_at).filter(Organization.id == org_id).first()
        raw = row[0] if row else None
        block = raw.get("payments") if isinstance(raw, dict) else None
        return (dict(block) if isinstance(block, dict) else {}), (row[1] if row else None)

    def _settings_dict(self, org_id: str) -> Dict[str, Any]:
        return self._org_settings(org_id)[0]

    def get_settings(self, org_id: str) -> PaymentSettings:
        block, created = self._org_settings(org_id)
        fields = PaymentSettings.model_fields
        values = {k: block[k] for k in fields if k in block and block[k] is not None and k != "accounting_start_date"}
        try:
            settings = PaymentSettings(**values)
        except ValueError:  # a hand-edited, out-of-range value must not break the page
            logger.warning("Invalid payment settings stored for org %s; using defaults", org_id)
            settings = PaymentSettings()
        settings.accounting_start_date = ls.accounting_start_of(block, created)
        return settings

    @staticmethod
    def _regime_fields(settings: PaymentSettings) -> Dict[str, Any]:
        return {
            "period_mode": settings.period_mode, "billing_anchor": settings.billing_anchor,
            "billing_day": settings.billing_day, "lessons_per_payment": settings.lessons_per_payment,
        }

    def update_settings(
        self, org_id: str, actor_id: str, patch: PaymentSettingsUpdate, *, dry_run: bool = False
    ) -> PaymentSettingsChanged:
        """Change the settings (an absent field stays).  A change of the period mode / billing day / package size is appended
        to ``billing_history`` from TODAY (what was charged before is never recalculated); ``accounting_start_date`` is
        the owner's (not later than today + 31 days).  ``dry_run``: nothing is written, the result carries the ``impact``."""
        query = self.db.query(Organization).filter(Organization.id == org_id)
        org = query.first() if dry_run else query.with_for_update().first()
        if not org:
            raise PaymentNotFound("Организация не найдена")
        changes = patch.model_dump(exclude_none=True)
        today = ps.business_today()
        new_start: Optional[date] = changes.pop("accounting_start_date", None)
        if new_start is not None:
            self._check_accounting_start(org_id, new_start, today)
        before = self.get_settings(org_id)
        block = dict(((org.settings or {}).get("payments")) or {})
        impact: Optional[AccountingStartImpact] = None
        if dry_run:
            if new_start is not None and new_start != before.accounting_start_date:
                impact = self._accounting_start_impact(org_id, new_start)
            return PaymentSettingsChanged(**before.model_dump(), dry_run=True, impact=impact or AccountingStartImpact())
        block.update(changes)
        if new_start is not None:
            block["accounting_start_date"] = new_start.isoformat()
        elif "accounting_start_date" not in block:
            block["accounting_start_date"] = before.accounting_start_date.isoformat()  # fix the effective date
        after = PaymentSettings(**{k: block[k] for k in PaymentSettings.model_fields if k in block and block[k] is not None
                                   and k != "accounting_start_date"})
        if self._regime_fields(after) != self._regime_fields(before):
            history = [dict(h) for h in (block.get("billing_history") or []) if isinstance(h, dict)]
            if not history:
                history = [{"from": "min", **self._regime_fields(before)}]
            entry = {"from": today.isoformat(), **self._regime_fields(after)}
            if history[-1].get("from") == today.isoformat():
                history[-1] = entry
            else:
                history.append(entry)
            block["billing_history"] = history
        block["updated_by"] = str(actor_id)
        block["updated_at"] = _utcnow().isoformat()
        org.settings = {**(org.settings or {}), "payments": block}
        flag_modified(org, "settings")
        self.db.commit()
        invalidate_summary(org_id)
        # The organisation has no audit table; the log line is the trail (no sums here anyway).
        logger.info(
            "payment settings updated org=%s actor=%s fields=%s",
            org_id, actor_id, sorted(list(changes) + (["accounting_start_date"] if new_start is not None else [])),
        )
        return PaymentSettingsChanged(**self.get_settings(org_id).model_dump())

    def _check_accounting_start(self, org_id: str, new_start: date, today: date) -> None:
        """The start is not before 01.01.2020, not later than today + 31 days, and never later than the earliest live money
        event of the centre (a payment, a write-off, a refund, a transfer): that money would lose its place in the account."""
        if new_start < ACCOUNTING_START_MIN:
            raise AccountingStartRange("Начало учёта оплат — не раньше 01.01.2020")
        if new_start > today + timedelta(days=ACCOUNTING_START_AHEAD_DAYS):
            raise AccountingStartRange("Начало учёта оплат — не позже, чем через 31 день от сегодня")
        firsts = [
            self.db.query(func.min(StudentPayment.paid_on)).filter(
                StudentPayment.org_id == org_id, StudentPayment.voided_at.is_(None), StudentPayment.kind == "payment").scalar(),
            self.db.query(func.min(StudentWriteoff.effective_on)).filter(
                StudentWriteoff.org_id == org_id, StudentWriteoff.voided_at.is_(None)).scalar(),
            self.db.query(func.min(StudentRefund.effective_on)).filter(
                StudentRefund.org_id == org_id, StudentRefund.voided_at.is_(None)).scalar(),
        ]
        moved = self.db.query(func.min(MembershipTransfer.at)).filter(
            MembershipTransfer.org_id == org_id, MembershipTransfer.voided_at.is_(None)).scalar()
        if moved is not None:
            firsts.append(ps.business_date(moved))
        earliest = min((d for d in firsts if d is not None), default=None)
        if earliest is not None and new_start > earliest:
            raise AccountingStartRange(f"Есть оплаты до этой даты: {earliest.strftime('%d.%m.%Y')}")

    def _accounting_start_impact(self, org_id: str, new_start: date) -> AccountingStartImpact:
        """How many memberships get a NEW debt (and how much debt appears) if the accounting starts on ``new_start``."""
        today = ps.business_today()
        current = ls.load_settings(self.db, org_id)
        moved = replace(current, accounting_start=new_start)
        old_loader = ls.LedgerLoader(self.db, org_id, today, current)
        new_loader = ls.LedgerLoader(self.db, org_id, today, moved)
        ids = list(dict.fromkeys(old_loader.select(scope="all") + new_loader.select(scope="all")))
        old = {r.membership.id: r.ledger for r in old_loader.run(ids)}
        new = {r.membership.id: r.ledger for r in new_loader.run(ids)}
        grew = [m for m in new if new[m].debt > (old[m].debt if m in old else 0)]
        return AccountingStartImpact(
            memberships_with_new_debt=len(grew), debt_delta=sum(l.debt for l in new.values()) - sum(l.debt for l in old.values()),
            memberships_with_changed_advance=len({m for m in new if new[m].advance != (old[m].advance if m in old else 0)}),
            advance_delta=sum(l.advance for l in new.values()) - sum(l.advance for l in old.values()),
            refundable_delta=sum(l.refundable for l in new.values()) - sum(l.refundable for l in old.values()))

    # -------------------------------------------------------------- loading

    def _loader(self, org_id: str, today: Optional[date] = None) -> ls.LedgerLoader:
        return ls.LedgerLoader(self.db, str(org_id), today or ps.business_today())

    @staticmethod
    def _status_settings(settings: PaymentSettings) -> ps.StatusSettings:
        return ps.StatusSettings(expiring_days=settings.expiring_days, expiring_lessons=settings.expiring_lessons)

    # ------------------------------------------------------------ serialising

    def _users(self, ids: Iterable[Optional[UUID]]) -> Dict[UUID, User]:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        return {u.id: u for u in self.db.query(User).filter(User.id.in_(wanted)).all()}

    def _links(self, payments: Iterable[StudentPayment]) -> _Links:
        """Where the carry-overs among ``payments`` come from and where the stamped ones went (LEGACY rows only)."""
        payments = [p for p in payments if p.carried_from_payment_id or p.carried_out_at]
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
        ledger: Optional[bl.Ledger] = None,
    ) -> PaymentOut:
        legacy = payment.valid_from is not None
        first, through = payment.valid_from, payment.valid_to
        allocation = None
        if ledger is not None and payment.voided_at is None and payment.kind == "payment" and str(payment.id) in ledger.net_money:
            allocation = pv.payment_allocation(ledger, str(payment.id))
            if not legacy:  # the old front reads a period: the first one reached and "paid until" (one release)
                first, through = pv.shim_period(ledger, str(payment.id))
        return PaymentOut(
            id=str(payment.id),
            membership_id=str(payment.membership_id),
            amount=int(payment.amount),
            currency=payment.currency,
            paid_on=payment.paid_on,
            valid_from=first,
            valid_to=through,
            lessons_count=payment.lessons_count,
            legacy_period=legacy,
            method=payment.method or "unknown",
            kind=payment.kind or "payment",
            allocation=allocation,
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

    def _rows(self, entries: Sequence[ls.MembershipLedger]) -> List[MembershipPaymentRow]:
        last = {e.membership.id: (e.payments[-1] if e.payments else None) for e in entries}
        users = self._users(x for p in last.values() if p for x in (p.created_by, p.voided_by))
        files = load_files(self.db, [p.id for p in last.values() if p])
        rows = []
        for e in entries:
            l, payment = e.ledger, last[e.membership.id]
            r = l.status
            rows.append(
                MembershipPaymentRow(
                    membership_id=str(e.membership.id),
                    student_id=str(e.student.id),
                    student_name=person_name(e.student.last_name, e.student.first_name) or "",
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
                    period_mode=l.mode,
                    paid_until=r.paid_until,
                    days_overdue=r.days_overdue,
                    days_left=r.days_left,
                    lessons_total=r.lessons_total,
                    lessons_used=r.lessons_used,
                    lessons_left=r.lessons_left,
                    last_payment=self._payment_out(payment, users, None, files, l) if payment else None,
                    debt=l.debt, past_debt=l.past_debt, current_due=l.current_due, advance=l.advance,
                    partial_credit=l.partial_credit, price_missing=bl.F_PRICE_MISSING in l.flags,
                    lessons_unpaid=l.lessons.unpaid if l.lessons else 0, refundable=l.refundable,
                    writeoff_allowed=l.debt > 0,
                )
            )
        return rows

    def _row(self, org_id: str, membership_id: UUID) -> MembershipPaymentRow:
        found = self._loader(org_id).run([membership_id])
        if not found:
            raise PaymentNotFound("Членство не найдено")
        return self._rows(found)[0]

    # -------------------------------------------------------------- reading

    def summary(self, org_id: str) -> PaymentsSummary:
        if SUMMARY_TTL_SECONDS > 0:
            hit = _SUMMARY_CACHE.get(str(org_id))
            if hit and hit[0] > time.monotonic() and hit[1].as_of == ps.business_today().isoformat():
                return hit[1]
        loader = self._loader(org_id)
        tracked = set(loader.select(scope="tracked"))
        everything = list(dict.fromkeys(list(tracked) + loader.select(scope="all")))
        entries = loader.run(everything)
        counts = {s: 0 for s in ps.STATUSES}
        students_due: set = set()
        students_expired: set = set()
        tracked_total = 0
        debt_total = advance_total = with_debt = with_advance = 0
        students_debt: set = set()
        for e in entries:
            l = e.ledger
            debt_total += l.debt
            advance_total += l.advance
            if l.debt > 0:
                with_debt += 1
                students_debt.add(e.student.id)
            if l.advance > 0:
                with_advance += 1
            if e.membership.id not in tracked:
                continue
            tracked_total += 1
            counts[l.status.status] += 1
            if l.status.status in (ps.EXPIRED, ps.ENDS_TODAY):
                students_due.add(e.student.id)
            if l.status.status == ps.EXPIRED:
                students_expired.add(e.student.id)
        settings = self.get_settings(org_id)
        result = PaymentsSummary(
            as_of=loader.today.isoformat(),
            due=counts[ps.EXPIRED] + counts[ps.ENDS_TODAY],
            ends_today=counts[ps.ENDS_TODAY],
            expired=counts[ps.EXPIRED],
            expiring=counts[ps.EXPIRING],
            none=counts[ps.NONE],
            active=counts[ps.ACTIVE] + counts[ps.FREE],
            free=counts[ps.FREE],
            tracked_total=tracked_total,
            period_mode=loader.settings.current.mode,
            lessons_per_payment=settings.lessons_per_payment,
            students_due=len(students_due),
            students_expired=len(students_expired),
            debt_total=debt_total,
            advance_total=advance_total,
            memberships_with_debt=with_debt,
            students_with_debt=len(students_debt),
            accounting_start_date=loader.settings.accounting_start,
        )
        if SUMMARY_TTL_SECONDS > 0:
            _SUMMARY_CACHE[str(org_id)] = (time.monotonic() + SUMMARY_TTL_SECONDS, result)
        return result

    @staticmethod
    def _sort_key(bucket: str, by_debt: bool):
        def name(e: ls.MembershipLedger):
            return (person_name(e.student.last_name, e.student.first_name) or "").lower(), e.group.name.lower(), str(e.membership.id)

        if by_debt or bucket == "debt":
            return lambda e: (-e.ledger.debt, *name(e))
        if bucket == ps.BUCKET_DUE:
            # Most overdue first; "ends today" and a spent lessons package have 0 days.
            return lambda e: (-(e.ledger.status.days_overdue or 0), *name(e))
        if bucket == ps.BUCKET_EXPIRING:
            def remaining(e: ls.MembershipLedger):
                r = e.ledger.status
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
        sort: Optional[str] = None,
    ) -> PaymentStatusPage:
        loader = self._loader(org_id)
        scope = "all" if bucket == "debt" else "tracked"
        entries = loader.load(group_id=group_id, teacher_id=teacher_id, search=search, scope=scope)
        if bucket == "debt":
            picked = [e for e in entries if e.ledger.debt > 0]
        else:
            picked = [e for e in entries if ps.in_bucket(e.ledger.status.status, bucket)]
        picked.sort(key=self._sort_key(bucket, sort == "debt"))
        page = picked[offset : offset + limit]
        return PaymentStatusPage(
            items=self._rows(page), total=len(picked), limit=limit, offset=offset, as_of=loader.today.isoformat(),
        )

    def student_memberships(self, org_id: str, student_id: UUID) -> List[MembershipPaymentRow]:
        student = self.db.query(Student.id).filter(Student.id == student_id, Student.org_id == org_id).first()
        if not student:
            raise PaymentNotFound("Ученик не найден")
        loader = self._loader(org_id)
        entries = loader.load(student_id=student_id, scope="all")
        # Active memberships, and closed ones only if they carry money (the ``all`` scope keeps those).
        kept = [e for e in entries if e.membership.is_active or e.payments or e.writeoffs or e.refunds or
                e.transfers_in or e.transfers_out]
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
        found = self._loader(org_id).run([membership_id])
        ledger = found[0].ledger if found else None
        users = self._users(x for p in payments for x in (p.created_by, p.voided_by))
        links = self._links(payments)
        files = load_files(self.db, [p.id for p in payments])
        return [self._payment_out(p, users, links, files, ledger) for p in payments]

    # -------------------------------------------------------------- writing

    def _membership(
        self, org_id: str, membership_id: UUID, *, lock: bool = False, writable: bool = False
    ) -> StudentGroup:
        """The membership of this organisation (``lock``: FOR NO KEY UPDATE).

        ``writable``: the caller is about to ENTER money (create / replace): refused with 409 when the student is
        archived.  A void is not "entering money" and does not ask for it.  The student's status is read by a
        statement of its own, AFTER the membership lock: the archiving holds the student and the membership together, so
        whoever wins the membership lock sees its outcome.  The student row is read, never locked here - the lock order
        student -> membership -> payment is not touched."""
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
            # FOR KEY SHARE that an insert of a payment takes on this row.
            query = query.with_for_update(of=StudentGroup, key_share=True)
        membership = query.first()
        if not membership:
            raise PaymentNotFound("Членство не найдено")
        if writable:
            status = self.db.query(Student.status).filter(Student.id == membership.student_id).scalar()
            if status == "archived":
                raise StudentArchived(ARCHIVED_STUDENT_MESSAGE)
        return membership

    def _lock_family(self, org_id: str, student_id: UUID) -> List[UUID]:
        """student -> all memberships of the student by id (the family of the transfers); returns their ids."""
        self.db.query(Student.id).filter(Student.id == student_id, Student.org_id == org_id).with_for_update(
            key_share=True).first()
        rows = (
            self.db.query(StudentGroup.id)
            .filter(StudentGroup.student_id == student_id)
            .order_by(StudentGroup.id)
            .with_for_update(key_share=True)
            .all()
        )
        return [r[0] for r in rows]

    def _family_check(self, org_id: str, family: Sequence[UUID]) -> None:
        """After a void / replace / refund: no outflow may exceed the money that is left (409 VOID_BREAKS_REFUND)."""
        for entry in self._loader(org_id).run(list(family)):
            if bl.F_OUTFLOW_EXCEEDS in entry.ledger.flags:
                raise VoidBreaksRefund("Сначала отмените возврат: без этой оплаты возвращать нечего")

    # ---- validation

    @staticmethod
    def _check_amount(amount: int) -> int:
        if not isinstance(amount, int) or amount < 1 or amount > MAX_MONEY:
            raise AmountRange(f"Сумма: от 1 до {_num(MAX_MONEY)}")
        return amount

    @staticmethod
    def _check_date(day: date, today: date, what: str = "Дата оплаты") -> None:
        if day > today:
            raise PaidOnRange(f"{what} не может быть в будущем")
        if day < _years_ago(today, MAX_PAID_AGO_YEARS):
            raise PaidOnRange(f"{what} старше 3 лет")

    @staticmethod
    def _method_of(method: Optional[str], *, required: bool = False) -> str:
        if method is None:
            if required:
                raise MethodRequired("Укажите способ оплаты")
            return "other"  # the old front (one release): no method
        if method == "unknown":
            raise MethodRequired("Укажите способ оплаты")
        return method

    @staticmethod
    def _check_reason(reason: Optional[str]) -> str:
        reason = (reason or "").strip()
        if not 3 <= len(reason) <= 500:
            raise ReasonRequired("Причина: от 3 до 500 символов")
        return reason

    # ---- receipts

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

        The number is ``max + 1`` taken inside a SAVEPOINT; two concurrent payments of one centre may pick the same one, the
        unique index refuses the second, and it retries with the new maximum (at most ``RECEIPT_ATTEMPTS`` times).  No lock on
        the organisation.  A concurrent POST with the same Idempotency-Key is refused by its own unique index (409)."""
        for attempt in range(1, RECEIPT_ATTEMPTS + 1):
            payment = build(self._next_receipt_no(org_id))
            try:
                with self.db.begin_nested():
                    self.db.add(payment)
                    self.db.flush()
                return payment
            except IntegrityError as exc:
                reason = str(exc.orig)
                if IDEMPOTENCY_INDEX in reason:  # a concurrent identical request slipped past the check
                    raise DuplicatePayment("Эта оплата уже внесена") from exc
                if RECEIPT_INDEX not in reason:
                    raise
                logger.info("receipt number taken by a concurrent payment (attempt %s)", attempt)
        raise PaymentConflict("Не удалось присвоить номер квитанции. Повторите.")

    def _guard_key(self, org_id: str, key: Optional[UUID]) -> None:
        """A POST with an Idempotency-Key that already made a payment: 409 DUPLICATE_PAYMENT with that payment's id."""
        if key is None:
            return
        existing = (
            self.db.query(StudentPayment.id)
            .filter(StudentPayment.org_id == org_id, StudentPayment.idempotency_key == key)
            .first()
        )
        if existing:
            raise DuplicatePayment("Эта оплата уже внесена", payment_id=str(existing[0]))

    def _guard_duplicate(
        self, org_id: str, membership: StudentGroup, data: PaymentCreate, key: Optional[UUID], *, soft: bool = True
    ) -> None:
        self._guard_key(org_id, key)
        # the same student + date + sum a moment ago is a double click - only for a request WITHOUT a key (a key says it is
        # deliberate: two equal payments with different keys both go in)
        if key is None and soft and SOFT_DUPLICATE_SECONDS > 0:
            recent = (
                self.db.query(StudentPayment.id)
                .filter(
                    StudentPayment.org_id == org_id,
                    StudentPayment.membership_id == membership.id,
                    StudentPayment.voided_at.is_(None),
                    StudentPayment.paid_on == data.paid_on,
                    StudentPayment.amount == data.amount,
                    StudentPayment.created_at >= _utcnow() - timedelta(seconds=SOFT_DUPLICATE_SECONDS),
                )
                .first()
            )
            if recent:
                raise DuplicatePayment("Такая оплата только что внесена", payment_id=str(recent[0]))

    def _insert(
        self, membership: StudentGroup, org_id: str, actor_id: str, data: PaymentCreate, method: str,
        key: Optional[UUID], *, replaces: Optional[StudentPayment] = None,
    ) -> StudentPayment:
        created_at = _utcnow()
        return self._add_with_receipt(
            org_id,
            lambda receipt_no: StudentPayment(
                org_id=org_id,
                membership_id=membership.id,
                student_id=membership.student_id,
                group_id=membership.group_id,
                amount=data.amount,
                currency="UZS",
                paid_on=data.paid_on,
                valid_from=None,
                valid_to=None,
                lessons_count=None,
                note=data.note,
                method=method,
                kind="payment",
                idempotency_key=key,
                replaces_payment_id=replaces.id if replaces else None,
                receipt_no=receipt_no,
                partial_period=False,
                created_by=UUID(str(actor_id)),
                created_at=created_at,
            ),
        )

    def _warnings(
        self, after: bl.Ledger, credit_id: str, paid_on: date, today: date, price_today: Optional[int], *, archived: bool = False
    ) -> List[PaymentWarningOut]:
        out: List[PaymentWarningOut] = []
        if bl.F_PRICE_MISSING in after.flags:
            out.append(PaymentWarningOut(code="price_missing", message="Цена не задана: оплата пока не распределена по периодам"))
        if bl.F_HORIZON in after.flags:
            out.append(PaymentWarningOut(code="horizon_capped",
                                         message="Сумма больше, чем можно разнести по ближайшим периодам; остаток — аванс"))
        if price_today and after.advance > LARGE_OVERPAY_PERIODS * price_today:
            out.append(PaymentWarningOut(
                code="large_overpay",
                message=f"Аванс {_sum(after.advance)} — больше {LARGE_OVERPAY_PERIODS} периодов вперёд: проверьте сумму"))
        for item in pv.price_ahead(after, credit_id, price_today):
            if item.amount is not None:
                out.append(PaymentWarningOut(code="price_change_ahead", message=tx.price_change_warning(
                    item.period_from, item.period_to, item.amount, item.current_amount or 0)))
        if archived:
            out.append(PaymentWarningOut(code="student_archived", message="Ученик в архиве: оплату внести нельзя"))
        if (today - paid_on).days > WARN_PAID_AGO_DAYS:
            out.append(PaymentWarningOut(code="paid_over_60_days_ago", message=ps.WARNING_MESSAGES["paid_over_60_days_ago"]))
        return out

    def _save_snapshot(
        self, org_id: str, payment: StudentPayment, before: bl.Ledger, after: bl.Ledger, price: pricing.Price,
        today: date, method: str,
    ) -> None:
        """Where the money went AT THE MOMENT (the printed receipt); the statement always shows the fact."""
        snap = ReceiptSnapshot(
            computed_at=_utcnow().isoformat(), business_date=today, amount=int(payment.amount), method=method,
            allocations=pv.allocations_of(after, str(payment.id)), debt_before=before.debt, debt_after=after.debt,
            advance_after=after.advance, paid_until_before=before.status.paid_until, paid_until_after=after.status.paid_until,
            price=SnapshotPrice(amount=price.amount, source=price.source), mode=after.mode,
        )
        self.db.add(PaymentReceiptSnapshot(payment_id=payment.id, org_id=UUID(str(org_id)),
                                           snapshot=snap.model_dump(mode="json")))

    def _created(self, org_id: str, payment: StudentPayment, before: bl.Ledger, today: date) -> PaymentCreated:
        found = self._loader(org_id, today).run([payment.membership_id])
        entry = found[0]
        users = self._users([payment.created_by])
        files = load_files(self.db, [payment.id])
        return PaymentCreated(
            payment=self._payment_out(payment, users, None, files, entry.ledger),
            allocation=pv.allocation_summary(before, entry.ledger, str(payment.id)),
            warnings=self._warnings(entry.ledger, str(payment.id), payment.paid_on, today, entry.price.amount),
            row=self._rows([entry])[0],
        )

    @deadlock_guard
    def create_payment(
        self, org_id: str, membership_id: UUID, actor_id: str, data: PaymentCreate, idempotency_key: Optional[UUID] = None
    ) -> PaymentCreated:
        today = ps.business_today()
        try:
            self._check_amount(data.amount)
            self._check_date(data.paid_on, today)
            method = self._method_of(data.method)
            membership = self._membership(org_id, membership_id, lock=True, writable=True)
            self._guard_duplicate(org_id, membership, data, idempotency_key)
            before_entry = self._loader(org_id, today).run([membership_id])
            before = before_entry[0].ledger
            payment = self._insert(membership, org_id, actor_id, data, method, idempotency_key)
            after_entry = self._loader(org_id, today).run([membership_id])[0]
            self._save_snapshot(org_id, payment, before, after_entry.ledger, after_entry.price, today, method)
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        logger.info("payment created id=%s membership=%s actor=%s", payment.id, membership_id, actor_id)
        return self._created(org_id, payment, before, today)

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
        idempotency_key: Optional[UUID] = None,
    ) -> PaymentCreated:
        """Void the old payment and insert the corrected one - one transaction.  The "before" of the answer is the account
        WITHOUT the replaced payment (the same as the preview with ``replace_payment_id``).

        Lock order: student, the memberships of the student by id, then the payment."""
        today = ps.business_today()
        try:
            self._check_amount(data.amount)
            self._check_date(data.paid_on, today)
            method = self._method_of(data.method)
            self._guard_key(org_id, idempotency_key)   # a replay of this replace: the stored result, not ALREADY_VOIDED
            probe = self._probe_payment(org_id, payment_id)
            if probe.kind == "legacy_carry" or probe.carried_from_payment_id is not None:
                raise PaymentConflict("Перенос оплаты нельзя исправить — это не денежная операция",)
            family = self._lock_family(org_id, probe.student_id)
            membership = self._membership(org_id, probe.membership_id, writable=True)
            old = self._lock_payment(org_id, payment_id)
            loader = self._loader(org_id, today)
            loader.load_facts([membership.id])
            before_inp = loader.build_input(membership.id, exclude_payment=old.id)
            before = bl.run_ledger(before_inp)
            old.voided_at = _utcnow()
            old.voided_by = UUID(str(actor_id))
            old.void_reason = (reason or "").strip() or DEFAULT_REPLACE_REASON
            self.db.flush()
            payment = self._insert(membership, org_id, actor_id, data, method, idempotency_key, replaces=old)
            after_entry = self._loader(org_id, today).run([membership.id])[0]
            self._save_snapshot(org_id, payment, before, after_entry.ledger, after_entry.price, today, method)
            self._family_check(org_id, family)
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        logger.info(
            "payment replaced old=%s new=%s membership=%s actor=%s", payment_id, payment.id, membership.id, actor_id,
        )
        return self._created(org_id, payment, before, today)

    @deadlock_guard
    def void_payment(self, org_id: str, payment_id: UUID, actor_id: str, reason: str) -> PaymentVoided:
        """Cancel a payment (no delete).  Under the lock of the family it is checked that no refund is left without money."""
        try:
            reason = self._check_reason(reason)
            probe = self._probe_payment(org_id, payment_id)
            family = self._lock_family(org_id, probe.student_id)
            self._membership(org_id, probe.membership_id)
            payment = self._lock_payment(org_id, payment_id)
            payment.voided_at = _utcnow()
            payment.voided_by = UUID(str(actor_id))
            payment.void_reason = reason
            self.db.flush()
            self._family_check(org_id, family)
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        logger.info("payment voided id=%s actor=%s", payment_id, actor_id)
        users = self._users([payment.created_by, payment.voided_by])
        entry = self._loader(org_id).run([payment.membership_id])[0]
        return PaymentVoided(
            payment=self._payment_out(payment, users, self._links([payment]), load_files(self.db, [payment.id]), entry.ledger),
            row=self._rows([entry])[0],
        )

    # ------------------------------------------------ write-off and refund

    def _audit(self, org_id: str, membership: StudentGroup, actor_id: str, actor_role: str, action: str,
               snapshot: Dict[str, Any]) -> None:
        self.db.add(
            GroupAudit(
                org_id=UUID(str(org_id)), group_id=membership.group_id, actor_id=UUID(str(actor_id)),
                actor_role=actor_role if actor_role in ("owner", "admin", "teacher", "superadmin") else "admin",
                action=action, snapshot=snapshot,
            )
        )

    def _entry_of(self, org_id: str, membership_id: UUID, today: Optional[date] = None) -> ls.MembershipLedger:
        found = self._loader(org_id, today).run([membership_id])
        if not found:
            raise PaymentNotFound("Членство не найдено")
        return found[0]

    def _writeoff_out(self, w: StudentWriteoff) -> WriteoffOut:
        users = self._users([w.created_by, w.voided_by])
        return WriteoffOut(
            id=str(w.id), membership_id=str(w.membership_id), amount=int(w.amount), reason=w.reason,
            effective_on=w.effective_on, created_by=str(w.created_by), created_by_name=_person(users.get(w.created_by)),
            created_at=_iso(w.created_at), voided_at=_iso(w.voided_at),
            voided_by=str(w.voided_by) if w.voided_by else None, voided_by_name=_person(users.get(w.voided_by)),
            void_reason=w.void_reason,
        )

    def _refund_out(self, r: StudentRefund) -> RefundOut:
        users = self._users([r.created_by, r.voided_by])
        return RefundOut(
            id=str(r.id), membership_id=str(r.membership_id), amount=int(r.amount), method=r.method, reason=r.reason,
            note=r.note, effective_on=r.effective_on, created_by=str(r.created_by),
            created_by_name=_person(users.get(r.created_by)), created_at=_iso(r.created_at), voided_at=_iso(r.voided_at),
            voided_by=str(r.voided_by) if r.voided_by else None, voided_by_name=_person(users.get(r.voided_by)),
            void_reason=r.void_reason,
        )

    @deadlock_guard
    def create_writeoff(
        self, org_id: str, membership_id: UUID, actor_id: str, *, amount: Optional[int], whole: bool, reason: str,
        effective_on: Optional[date] = None, actor_role: str = "admin",
    ) -> WriteoffCreated:
        """Write off a debt (≤ the debt now, or ``whole``); the reason 3..500 characters; archived students are allowed."""
        today = ps.business_today()
        try:
            reason = self._check_reason(reason)
            effective_on = effective_on or today
            self._check_date(effective_on, today, "Дата прощения долга")
            membership = self._membership(org_id, membership_id)
            family = self._lock_family(org_id, membership.student_id)
            before = self._entry_of(org_id, membership_id, today).ledger
            if whole:
                amount = before.debt
                if amount < 1:
                    raise WriteoffExceedsDebt("Долга нет — прощать нечего", debt=0)
            if amount is None or amount < 1 or amount > MAX_MONEY:
                raise AmountRange(f"Сумма: от 1 до {_num(MAX_MONEY)}")
            if amount > before.debt:
                raise WriteoffExceedsDebt(
                    f"Простить можно не больше долга: {_sum(before.debt)}" if before.debt else "Долга нет — прощать нечего",
                    debt=before.debt)
            row = StudentWriteoff(
                org_id=UUID(str(org_id)), membership_id=membership.id, student_id=membership.student_id,
                group_id=membership.group_id, amount=amount, reason=reason, effective_on=effective_on,
                created_by=UUID(str(actor_id)), created_at=_utcnow(),
            )
            self.db.add(row)
            self.db.flush()
            after = self._entry_of(org_id, membership_id, today).ledger
            self._audit(org_id, membership, actor_id, actor_role, AUDIT_WRITEOFF, {
                "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                "writeoff_id": str(row.id), "amount": int(amount), "reason": reason, "effective_on": effective_on.isoformat(),
                "debt_before": before.debt, "debt_after": after.debt, "advance_before": before.advance,
                "advance_after": after.advance})
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        logger.info("writeoff created id=%s membership=%s actor=%s", row.id, membership_id, actor_id)
        entry = self._entry_of(org_id, membership_id, today)
        return WriteoffCreated(writeoff=self._writeoff_out(row), debt_before=before.debt, debt_after=after.debt,
                               row=self._rows([entry])[0])

    @deadlock_guard
    def void_writeoff(
        self, org_id: str, writeoff_id: UUID, actor_id: str, reason: str, *, actor_role: str = "admin"
    ) -> WriteoffVoided:
        try:
            reason = self._check_reason(reason)
            probe = (
                self.db.query(StudentWriteoff)
                .filter(StudentWriteoff.id == writeoff_id, StudentWriteoff.org_id == org_id).first()
            )
            if probe is None:
                raise PaymentNotFound("Прощение долга не найдено")
            family = self._lock_family(org_id, probe.student_id)
            row = (
                self.db.query(StudentWriteoff).filter(StudentWriteoff.id == writeoff_id)
                .populate_existing().with_for_update().first()
            )
            if row.voided_at is not None:
                raise AlreadyVoided("Прощение долга уже отменено")
            membership = self._membership(org_id, row.membership_id)
            today = ps.business_today()
            before = self._entry_of(org_id, row.membership_id, today).ledger
            row.voided_at, row.voided_by, row.void_reason = _utcnow(), UUID(str(actor_id)), reason
            self.db.flush()
            after = self._entry_of(org_id, row.membership_id, today).ledger
            self._audit(org_id, membership, actor_id, actor_role, AUDIT_WRITEOFF_VOID, {
                "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                "writeoff_id": str(row.id), "amount": int(row.amount), "reason": reason,
                "debt_before": before.debt, "debt_after": after.debt, "advance_before": before.advance,
                "advance_after": after.advance})
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        logger.info("writeoff voided id=%s actor=%s", writeoff_id, actor_id)
        return WriteoffVoided(writeoff=self._writeoff_out(row),
                              row=self._rows([self._entry_of(org_id, row.membership_id)])[0])

    @deadlock_guard
    def create_refund(
        self, org_id: str, membership_id: UUID, actor_id: str, *, amount: int, method: Optional[str],
        reason: Optional[str], note: Optional[str], effective_on: Optional[date] = None, actor_role: str = "admin",
    ) -> RefundCreated:
        """Return an advance (≤ what can leave the account on that day); the method is mandatory."""
        today = ps.business_today()
        try:
            method = self._method_of(method, required=True)
            effective_on = effective_on or today
            self._check_date(effective_on, today, "Дата возврата")
            if not isinstance(amount, int) or amount < 1 or amount > MAX_MONEY:
                raise AmountRange(f"Сумма: от 1 до {_num(MAX_MONEY)}")
            reason = (reason or "").strip() or None
            note = (note or "").strip() or None
            membership = self._membership(org_id, membership_id)
            family = self._lock_family(org_id, membership.student_id)
            loader = self._loader(org_id, today)
            loader.load_facts([membership_id])
            inp = loader.build_input(membership_id)
            before = bl.run_ledger(inp)
            allowed = min(before.refundable, bl.refundable_at(inp, effective_on))
            if amount > allowed:
                raise RefundExceedsAdvance(
                    f"Вернуть можно не больше {_sum(allowed)}" if allowed else "Возвращать нечего: аванса нет",
                    refundable=allowed)
            row = StudentRefund(
                org_id=UUID(str(org_id)), membership_id=membership.id, student_id=membership.student_id,
                group_id=membership.group_id, amount=amount, method=method, reason=reason, note=note,
                effective_on=effective_on, created_by=UUID(str(actor_id)), created_at=_utcnow(),
            )
            self.db.add(row)
            self.db.flush()
            after = self._entry_of(org_id, membership_id, today).ledger
            self._family_check(org_id, family)
            self._audit(org_id, membership, actor_id, actor_role, AUDIT_REFUND, {
                "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                "refund_id": str(row.id), "amount": int(amount), "method": method, "reason": reason,
                "effective_on": effective_on.isoformat(), "debt_before": before.debt, "debt_after": after.debt,
                "advance_before": before.advance, "advance_after": after.advance})
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        logger.info("refund created id=%s membership=%s actor=%s", row.id, membership_id, actor_id)
        entry = self._entry_of(org_id, membership_id, today)
        return RefundCreated(refund=self._refund_out(row), advance_before=before.advance, advance_after=after.advance,
                             row=self._rows([entry])[0])

    @deadlock_guard
    def void_refund(
        self, org_id: str, refund_id: UUID, actor_id: str, reason: str, *, actor_role: str = "admin"
    ) -> RefundVoided:
        try:
            reason = self._check_reason(reason)
            probe = self.db.query(StudentRefund).filter(StudentRefund.id == refund_id, StudentRefund.org_id == org_id).first()
            if probe is None:
                raise PaymentNotFound("Возврат не найден")
            self._lock_family(org_id, probe.student_id)
            row = (
                self.db.query(StudentRefund).filter(StudentRefund.id == refund_id)
                .populate_existing().with_for_update().first()
            )
            if row.voided_at is not None:
                raise AlreadyVoided("Возврат уже отменён")
            membership = self._membership(org_id, row.membership_id)
            today = ps.business_today()
            before = self._entry_of(org_id, row.membership_id, today).ledger
            row.voided_at, row.voided_by, row.void_reason = _utcnow(), UUID(str(actor_id)), reason
            self.db.flush()
            after = self._entry_of(org_id, row.membership_id, today).ledger
            self._audit(org_id, membership, actor_id, actor_role, AUDIT_REFUND_VOID, {
                "actor_id": str(actor_id), "membership_id": str(membership.id), "student_id": str(membership.student_id),
                "refund_id": str(row.id), "amount": int(row.amount), "method": row.method, "reason": reason,
                "debt_before": before.debt, "debt_after": after.debt, "advance_before": before.advance,
                "advance_after": after.advance})
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        logger.info("refund voided id=%s actor=%s", refund_id, actor_id)
        return RefundVoided(refund=self._refund_out(row), row=self._rows([self._entry_of(org_id, row.membership_id)])[0])

    # ------------------------------------------------ transfer between groups

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
        """Record a transfer: one LINK per closed membership (``membership_transfers``).  The money that travels is derived
        (``billing_ledger.refundable_at`` of the old membership on the transfer day), never stored - correcting a payment in the
        old group changes what arrived in the new one.

        Called by ``AuthService.add_student_to_group`` (mode ``transfer``) inside its transaction: flushes, never commits.
        ``moment`` is the instant the closed memberships get as ``left_at`` (default now).  Returns the ``warnings`` of the
        response: the sum when ``detailed`` (the caller may see money), else one generic sentence; nothing when no money moved."""
        sources = [m for m in sources if m.id != target.id]
        if not sources:
            return []
        if moment is None:
            moment = _utcnow()
        elif moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
        made: List[MembershipTransfer] = []
        for source in sources:
            exists = (
                self.db.query(MembershipTransfer.id)
                .filter(MembershipTransfer.from_membership_id == source.id, MembershipTransfer.to_membership_id == target.id,
                        MembershipTransfer.at == moment)
                .first()
            )
            if exists:
                continue
            link = MembershipTransfer(
                org_id=UUID(str(org_id)), student_id=UUID(str(target.student_id)), from_membership_id=source.id,
                to_membership_id=target.id, at=moment, created_by=UUID(str(actor_id)) if actor_id else None,
                created_at=_utcnow(),
            )
            self.db.add(link)
            made.append(link)
        if not made:
            return []
        self.db.flush()
        loader = self._loader(org_id, ps.business_date(moment))
        loader.load_facts([target.id] + [m.id for m in sources])
        total = sum(loader.x_of(t) for t in made)
        logger.info("transfer recorded to=%s from=%s actor=%s", target.id, [str(m.id) for m in sources], actor_id)
        invalidate_summary(org_id)
        if total <= 0:
            return []
        if not detailed:
            return [{"code": CARRY_CODE, "message": CARRY_GENERIC_MESSAGE}]
        group = self.db.query(Group.name).filter(Group.id == target.group_id).scalar() or ""
        return [{"code": CARRY_CODE, "message": f"Остаток денег {_sum(total)} перенесён в «{group}»"}]

    # ------------------------------------------------------------ preview

    def preview(
        self, org_id: str, membership_id: UUID, *, amount: Optional[int] = None, paid_on: Optional[date] = None,
        replace_payment_id: Optional[UUID] = None,
    ) -> PaymentPreview:
        """«Что закроет»: the debt and the running period today + where the typed sum would go.  Writes nothing."""
        today = ps.business_today()
        paid_on = paid_on or today
        self._check_date(paid_on, today)
        if amount is not None:
            self._check_amount(amount)
        membership = self._membership(org_id, membership_id)
        archived = self.db.query(Student.status).filter(Student.id == membership.student_id).scalar() == "archived"
        loader = self._loader(org_id, today)
        loader.load_facts([membership_id])
        exclude = None
        if replace_payment_id is not None:
            old = self._probe_payment(org_id, replace_payment_id)
            if old.membership_id != membership.id:
                raise PaymentNotFound("Оплата не найдена")
            exclude = old.id
        inp = loader.build_input(membership_id, exclude_payment=exclude)
        before = bl.run_ledger(inp)
        price = loader.books.price(membership_id, membership.group_id, today)
        preview = PaymentPreview(
            as_of=today, mode=before.mode, price=PriceOut(**pricing.price_dict(price)), due=pv.due_block(before),
            suggested_amount=pv.suggested_amount(before, price.amount), amount=amount, result=None, warnings=[],
        )
        warnings: List[PaymentWarningOut] = []
        if bl.F_PRICE_MISSING in before.flags:
            warnings.append(PaymentWarningOut(code="price_missing", message="Цена не задана: оплата пока не распределена по периодам"))
        if archived:
            warnings.append(PaymentWarningOut(code="student_archived", message="Ученик в архиве: оплату внести нельзя"))
        if amount is not None:
            credit = bl.Credit("preview", bl.PAYMENT, paid_on, datetime(9999, 1, 1), amount)
            after = bl.run_ledger(replace(inp, credits=tuple(inp.credits) + (credit,)))
            preview.result = pv.preview_result(before, after, "preview", price.amount)
            more = self._warnings(after, "preview", paid_on, today, price.amount, archived=False)
            seen = {w.code for w in warnings}
            warnings.extend(w for w in more if w.code not in seen or w.code == "price_change_ahead")
        preview.warnings = warnings
        return preview

    def quote(self, org_id: str, membership_id: UUID, **_: Any) -> PaymentQuote:
        """SHIM for the old front (one release): the suggested sum is the debt; the dates are those of the first unpaid period."""
        today = ps.business_today()
        entry = self._entry_of(org_id, membership_id, today)
        l = entry.ledger
        seg = next((s for s in l.segments if s.unpaid > 0 and not s.unpriced), None)
        regime = l.mode
        current = self._loader(org_id, today).settings.current
        suggested = pv.suggested_amount(l, entry.price.amount)
        start = seg.period_start if seg else today
        end = seg.period_end if seg and regime == ps.CALENDAR else None
        text = (f"К оплате {_sum(suggested)}" if suggested else "Оплата не требуется")
        return PaymentQuote(
            mode=regime, billing_anchor=current.anchor, billing_day=current.billing_day, paid_on=today, valid_from=start,
            valid_to=end, lessons_count=current.package_size if regime == ps.LESSONS else None, partial=False,
            monthly_fee=entry.price.amount, price=PriceOut(**pricing.price_dict(entry.price)),
            suggested_amount=suggested or None, basis="full", parts=[], explanation=text,
        )

    # ------------------------------------------------------------ receipt

    def receipt(self, org_id: str, payment_id: UUID) -> ReceiptOut:
        """What the printed receipt shows (the snapshot of the moment of the payment).  A legacy carry-over: 409."""
        payment = self._probe_payment(org_id, payment_id)
        if payment.carried_from_payment_id is not None or payment.kind == "legacy_carry":
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
            self.db.query(StudentPayment.receipt_no, StudentPayment.amount)
            .filter(StudentPayment.org_id == org_id, StudentPayment.id == payment.replaces_payment_id)
            .first()
            if payment.replaces_payment_id
            else None
        )
        users = self._users([payment.created_by])
        snap = self.db.query(PaymentReceiptSnapshot.snapshot).filter(PaymentReceiptSnapshot.payment_id == payment.id).scalar()
        extra: Dict[str, Any] = {"valid_from": payment.valid_from, "valid_to": payment.valid_to}   # legacy receipts
        if isinstance(snap, dict):
            parsed = ReceiptSnapshot.model_validate(snap)
            moved = [x for x in parsed.allocations if x.segment_id != bl.ADVANCE_SEGMENT]
            extra.update(
                # the shim for the old front (one release): the first period the money reached and "paid until"
                valid_from=payment.valid_from or (min(x.period_from for x in moved) if moved else None),
                valid_to=payment.valid_to or parsed.paid_until_after,
                mode=parsed.mode, allocations=parsed.allocations, debt_before=parsed.debt_before,
                debt_after=parsed.debt_after, advance_after=parsed.advance_after,
                paid_until_before=parsed.paid_until_before, paid_until_after=parsed.paid_until_after,
                price_snapshot=parsed.price)
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
            lessons_count=payment.lessons_count,
            partial_period=bool(payment.partial_period),
            method=payment.method or "unknown",
            note=payment.note,
            accepted_by_name=_person(users.get(payment.created_by)),
            created_at=_iso(payment.created_at),
            voided_at=_iso(payment.voided_at),
            void_reason=payment.void_reason,
            replaced_by_receipt_no=replaced_by[0] if replaced_by else None,
            replaces_receipt_no=replaces[0] if replaces else None,
            replaces_amount=int(replaces[1]) if replaces else None,
            **extra,
        )

    # ------------------------------------------------- coverage (payroll, excuses)

    def lesson_coverage(
        self,
        org_id: str,
        membership_ids: Iterable[UUID],
        first_day: date,
        last_day: date,
    ) -> Dict[Tuple[UUID, date, str], LessonCover]:
        """Is each counted lesson dated ``first_day .. last_day`` paid? (payroll)

        ``{(membership_id, lesson_date, lesson_id): LessonCover}`` for every lesson that spends a payment by rule 4.3
        (``_counted_lesson_dates``).  Paid = ``Ledger.lesson_paid``: a day before the first charged day is "grace", a calendar day
        is paid when it is not after "paid until", a lessons-mode lesson when its place in hand-out order is covered by the
        money.  Cost: a fixed number of queries, whatever the number of memberships."""
        ids = {i for i in membership_ids if i}
        if not ids:
            return {}
        rows = (
            self.db.query(StudentGroup, Student, Group)
            .join(Student, Student.id == StudentGroup.student_id)
            .join(Group, Group.id == StudentGroup.group_id)
            .filter(StudentGroup.id.in_(ids), Group.org_id == org_id)
            .all()
        )
        if not rows:
            return {}
        detail: Dict[UUID, List[tuple]] = {}
        self._counted_lesson_dates(org_id, rows, {}, since=first_day, until=last_day, detail_out=detail)
        loader = self._loader(org_id)
        entries = {e.membership.id: e for e in loader.run([m.id for m, _, _ in rows])}
        regimes = loader.settings.regimes
        out: Dict[Tuple[UUID, date, str], LessonCover] = {}
        for membership, _student, _group in rows:
            entry = entries.get(membership.id)
            for day, lesson_id, _attendance in detail.get(membership.id, ()):
                if not first_day <= day <= last_day:
                    continue
                regime = bl.regime_at(regimes, day)
                paid = entry.ledger.lesson_paid(day, lesson_id or "") if entry else False
                out[(membership.id, day, lesson_id)] = LessonCover(
                    None, regime.mode, regime.package_size if regime.mode == ps.LESSONS else None, paid)
        return out

    # ------------------------------------------------- counted lessons

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

    # ------------------------------------------------- cheques

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

