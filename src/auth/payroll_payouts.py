"""
ALGO - LOGIC-A (decision 6, S-8): payouts of salary.  Owner only.

A payout is a FACT against a SAVED sheet (append-only, cancelled by ``voided_*``).  It belongs to (teacher, month): ``PayoutState`` is
computed from the live payouts of the teacher and month against a TARGET - the fresh total when the sheet is stale, else the saved total.
Whatever happens to the data afterwards, what was paid is never rewritten: the balance says «доплатить» or «переплачено».  A payout never
changes the sheet (total, lines, fingerprint).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.auth import PayrollPayout, PayrollSheet, User

from . import payment_status as ps
from . import payments_texts as tx
from .payments_files import staff_name
from .payroll_engine import MAX_FEE
from .payroll_schemas import PayoutCreate, PayoutCreated, PayoutOut, PayoutState, PayoutVoided

logger = logging.getLogger(__name__)

SOFT_DUPLICATE_SECONDS = 60  # the same teacher + month + sum + day + method within a minute is a double click
MAX_PAID_AGO_YEARS = 3
METHODS = ("cash", "card", "bank_transfer", "other")


class PayoutError(Exception):
    status_code = 400
    code: Optional[str] = None

    def __init__(self, message: str, **extra: Any) -> None:
        self.message, self.extra = message, extra
        super().__init__(message)

    @property
    def detail(self) -> Any:
        return {"code": self.code, "message": self.message, **self.extra} if self.code else self.message


class PayoutNotFound(PayoutError):
    status_code = 404


class SheetNotSaved(PayoutError):
    status_code, code = 409, "SHEET_NOT_SAVED"


class DuplicatePayout(PayoutError):
    status_code, code = 409, "DUPLICATE_PAYOUT"


class PayoutAlreadyVoided(PayoutError):
    status_code, code = 409, "ALREADY_VOIDED"


class PayoutAmountRange(PayoutError):
    status_code, code = 422, "AMOUNT_RANGE"


class PayoutDateRange(PayoutError):
    status_code, code = 422, "PAID_ON_RANGE"


class PayoutMethodRequired(PayoutError):
    status_code, code = 422, "METHOD_REQUIRED"


class PayoutReasonRequired(PayoutError):
    status_code, code = 422, "REASON_REQUIRED"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def state_of(*, saved_total: int, fresh_total: Optional[int], items: List[PayoutOut]) -> PayoutState:
    """The balance of a (teacher, month): see the module docstring.  ``items`` oldest first (voided ones listed, not counted)."""
    paid = sum(p.amount for p in items if p.voided_at is None)
    stale = fresh_total is not None and fresh_total != saved_total
    target = fresh_total if stale else saved_total
    balance = target - paid
    if balance < 0:
        name = "overpaid"
    elif balance == 0:
        name = "settled"
    else:
        name = "unpaid" if paid == 0 else "due"
    return PayoutState(
        saved_total=saved_total, fresh_total=fresh_total, target=target, paid=paid, balance=balance, state=name,
        basis="fresh" if stale else "saved",
        text=tx.payout_text(state=name, saved_total=saved_total, fresh_total=fresh_total, target=target, paid=paid, balance=balance),
        items=items)


class PayrollPayoutService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---- reading

    def _out(self, p: PayrollPayout, names: Dict[UUID, Optional[str]]) -> PayoutOut:
        return PayoutOut(
            id=str(p.id), sheet_id=str(p.sheet_id), teacher_id=str(p.teacher_id), month=p.period_month.strftime("%Y-%m"),
            amount=int(p.amount), paid_on=p.paid_on, method=p.method, note=p.note, sheet_revision=p.sheet_revision,
            sheet_total=int(p.sheet_total), created_by_name=names.get(p.created_by), created_at=_iso(p.created_at),
            voided_at=_iso(p.voided_at), void_reason=p.void_reason)

    def _names(self, rows: Iterable[PayrollPayout]) -> Dict[UUID, Optional[str]]:
        ids = {r.created_by for r in rows if r.created_by}
        if not ids:
            return {}
        return {u.id: staff_name(u.first_name, u.last_name) for u in self.db.query(User).filter(User.id.in_(ids)).all()}

    def payouts_of(self, org_id: str, *, month: Optional[date] = None, teacher_ids: Optional[Iterable[UUID]] = None) -> Dict[Tuple[UUID, date], List[PayoutOut]]:
        """``{(teacher, first day of the month): [PayoutOut, oldest first]}`` - one query for any number of teachers."""
        query = self.db.query(PayrollPayout).filter(PayrollPayout.org_id == org_id)
        if month is not None:
            query = query.filter(PayrollPayout.period_month == month)
        if teacher_ids is not None:
            query = query.filter(PayrollPayout.teacher_id.in_(list(teacher_ids)))
        rows = query.order_by(PayrollPayout.created_at, PayrollPayout.id).all()
        names = self._names(rows)
        out: Dict[Tuple[UUID, date], List[PayoutOut]] = {}
        for r in rows:
            out.setdefault((r.teacher_id, r.period_month), []).append(self._out(r, names))
        return out

    def state_for(self, org_id: str, sheet: PayrollSheet, fresh_total: Optional[int],
                  all_payouts: Optional[Dict[Tuple[UUID, date], List[PayoutOut]]] = None) -> PayoutState:
        items = (all_payouts if all_payouts is not None else self.payouts_of(org_id, month=sheet.period_month, teacher_ids=[sheet.teacher_id])
                 ).get((sheet.teacher_id, sheet.period_month), [])
        return state_of(saved_total=int(sheet.total_amount), fresh_total=fresh_total, items=items)

    def list(self, org_id: str, *, month: Optional[date], teacher_id: Optional[UUID]) -> List[PayoutOut]:
        grouped = self.payouts_of(org_id, month=month, teacher_ids=[teacher_id] if teacher_id else None)
        return [p for items in grouped.values() for p in items]

    # ---- writing

    @staticmethod
    def _check(body: PayoutCreate, today: date) -> Tuple[int, date, str]:
        if not isinstance(body.amount, int) or isinstance(body.amount, bool) or body.amount < 1 or body.amount > MAX_FEE:
            raise PayoutAmountRange(f"Сумма: от 1 до {tx.num(MAX_FEE)}")
        paid_on = body.paid_on or today
        if paid_on > today:
            raise PayoutDateRange("Дата выплаты не может быть в будущем")
        try:
            oldest = today.replace(year=today.year - MAX_PAID_AGO_YEARS)
        except ValueError:
            oldest = today.replace(year=today.year - MAX_PAID_AGO_YEARS, day=28)
        if paid_on < oldest:
            raise PayoutDateRange("Дата выплаты старше 3 лет")
        if body.method is None:
            raise PayoutMethodRequired("Укажите способ выплаты")
        return body.amount, paid_on, body.method

    def create(self, org_id: str, sheet_id: UUID, actor_id: str, body: PayoutCreate, key: Optional[UUID], fresh_total_of) -> PayoutCreated:
        """``fresh_total_of(sheet) -> Optional[int]`` is the payroll's fresh calculation of the sheet (called after the write)."""
        today = ps.business_today()
        try:
            amount, paid_on, method = self._check(body, today)
            sheet = (self.db.query(PayrollSheet).filter(PayrollSheet.id == sheet_id, PayrollSheet.org_id == org_id)
                     .with_for_update().populate_existing().first())
            if sheet is None:
                raise SheetNotSaved("Сначала сохраните ведомость")
            if key is not None:
                same = self.db.query(PayrollPayout).filter(PayrollPayout.org_id == org_id, PayrollPayout.idempotency_key == key).first()
                if same is not None:
                    raise DuplicatePayout("Выплата уже внесена", payout_id=str(same.id))
            elif SOFT_DUPLICATE_SECONDS > 0:
                since = _utcnow() - timedelta(seconds=SOFT_DUPLICATE_SECONDS)
                twin = (self.db.query(PayrollPayout).filter(
                    PayrollPayout.org_id == org_id, PayrollPayout.teacher_id == sheet.teacher_id,
                    PayrollPayout.period_month == sheet.period_month, PayrollPayout.amount == amount,
                    PayrollPayout.paid_on == paid_on, PayrollPayout.method == method, PayrollPayout.voided_at.is_(None),
                    PayrollPayout.created_at >= since).first())
                if twin is not None:
                    raise DuplicatePayout("Такая выплата только что внесена", payout_id=str(twin.id))
            row = PayrollPayout(
                org_id=UUID(str(org_id)), sheet_id=sheet.id, teacher_id=sheet.teacher_id, period_month=sheet.period_month,
                amount=amount, paid_on=paid_on, method=method, note=(body.note or "").strip() or None,
                sheet_revision=sheet.revision, sheet_total=int(sheet.total_amount), idempotency_key=key,
                created_by=UUID(str(actor_id)), created_at=_utcnow())
            self.db.add(row)
            self.db.commit()
        except PayoutError:
            self.db.rollback()
            raise
        logger.info("payroll payout created id=%s sheet=%s actor=%s", row.id, sheet_id, actor_id)
        names = self._names([row])
        return PayoutCreated(payout=self._out(row, names), state=self.state_for(org_id, sheet, fresh_total_of(sheet)))

    def void(self, org_id: str, payout_id: UUID, actor_id: str, reason: str, fresh_total_of) -> PayoutVoided:
        try:
            reason = (reason or "").strip()
            if not 3 <= len(reason) <= 500:
                raise PayoutReasonRequired("Причина: от 3 до 500 символов")
            probe = self.db.query(PayrollPayout).filter(PayrollPayout.id == payout_id, PayrollPayout.org_id == org_id).first()
            if probe is None:
                raise PayoutNotFound("Выплата не найдена")
            sheet = (self.db.query(PayrollSheet).filter(PayrollSheet.id == probe.sheet_id).with_for_update().populate_existing().first())
            row = self.db.query(PayrollPayout).filter(PayrollPayout.id == payout_id).populate_existing().with_for_update().first()
            if row.voided_at is not None:
                raise PayoutAlreadyVoided("Выплата уже отменена")
            row.voided_at, row.voided_by, row.void_reason = _utcnow(), UUID(str(actor_id)), reason
            self.db.commit()
        except PayoutError:
            self.db.rollback()
            raise
        logger.info("payroll payout voided id=%s actor=%s", payout_id, actor_id)
        return PayoutVoided(payout=self._out(row, self._names([row])), state=self.state_for(org_id, sheet, fresh_total_of(sheet)))
