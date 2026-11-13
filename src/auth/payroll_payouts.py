"""
ALGO - LOGIC-A (decision 6, S-8): payouts of salary.  Owner only.

A payout is a FACT against a SAVED sheet (append-only, cancelled by ``voided_*``).  It belongs to (teacher, month): ``PayoutState`` is
computed from the live payouts of the teacher and month against a TARGET - the SAVED total (Money-1 A1b: it used to be the fresh total when the sheet was stale; now the change after the
save is a line of its own, ``changed_by`` «изменилось на ±N», with the button «Пересохранить»).
Whatever happens to the data afterwards, what was paid is never rewritten: the balance says «доплатить» or «переплачено».  A payout never
changes the sheet (total, lines, fingerprint).

THE ADVANCE IS CARRIED (R3 3, decision of the lead 02.11): what a month is overpaid by (an advance the owner confirmed) is NOT lost: it is taken off the same teacher's NEXT month
(``PayoutState.carry_in`` / ``carry_used``: «Аванс прошлого месяца»; ``target`` is the total after it); an advance larger than the next total goes on to the month after
(``advance_out``).  The chain is computed from what is FROZEN: the SAVED totals of the earlier months and their LIVE payouts - a stale sheet of a past month never changes the
next month behind the owner's back, the sheet of a past month is not touched, and voiding an advance payout takes the carry away at once (nothing is stored, it is derived).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from src.models.auth import PayrollPayout, PayrollSheet, User

from . import business_clock as bc
from . import payment_status as ps
from . import payments_texts as tx
from .payments_files import staff_name
from .payroll_engine import MAX_FEE
from .payroll_schemas import PayoutCreate, PayoutCreated, PayoutOut, PayoutState, PayoutVoided

logger = logging.getLogger(__name__)

SOFT_DUPLICATE_SECONDS = 60  # the same teacher + month + sum + day + method within a minute is a double click
MAX_PAID_AGO_YEARS = ps.MAX_PAID_AGO_YEARS
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


class OverpayConfirmRequired(PayoutError):
    """MN-32: the payout is bigger than what is left to pay; ``overpay`` is by how many sum, ``balance`` what was left (>= 0).  Also the answer to a confirmation of ANOTHER sum than the real one (R3 7)."""

    status_code, code = 409, "OVERPAY_CONFIRM_REQUIRED"


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
    return bc.utc_now()   # MN-03 / DM-005: the one clock (``set_clock`` of the tests freezes it)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def state_of(*, saved_total: int, fresh_total: Optional[int], items: List[PayoutOut], carry_in: int = 0, month_open: bool = False) -> PayoutState:
    """The balance of a (teacher, month): see the module docstring.  ``items`` oldest first (voided ones listed, not counted).  ``carry_in``: the advance of the earlier months.

    Money-1 A1b: the TARGET is always the SAVED total (the sheet the owner looked at and saved is the truth); what changed in the data afterwards is told by ``changed_by`` (fresh - saved)
    and ``change_text`` («изменилось на +N» + «Пересохранить»), never silently put into the sum to pay.  One number per month - the one that ``carry_ins`` hands on to the next month, too.

    R3 #20 (an advance in the middle of the month): while the month is OPEN (the sheet is not saved after the month ended: an advance on the 15th, the month still running, or reopened) the saved
    total is only a stage of it - the target is what the data says NOW (``basis`` ``fresh``): «месяц открыт: начислено сейчас N, выплачено M, к выплате N−M», never «Выплачено полностью»
    while N > M; a payout above it needs the confirmation of an overpay.  A CLOSED month keeps the saved total as the target.  An open month hands no advance on (``carry_ins``): it is not frozen yet."""
    paid = sum(p.amount for p in items if p.voided_at is None)
    changed_by = fresh_total - saved_total if fresh_total is not None and fresh_total != saved_total else 0
    fresh_target = month_open and fresh_total is not None
    total = fresh_total if fresh_target else saved_total
    carry_used = min(max(carry_in, 0), max(total, 0))
    target = total - carry_used
    balance = target - paid
    if balance < 0:
        name = "overpaid"
    elif balance == 0:
        name = "settled"
    else:
        name = "unpaid" if paid == 0 else "due"
    return PayoutState(
        saved_total=saved_total, fresh_total=fresh_total, target=target, paid=paid, balance=balance, state=name,
        basis="fresh" if fresh_target else "saved", changed_by=changed_by, change_text=tx.payout_change_text(changed_by) if changed_by else None,
        text=(tx.open_month_payout_text(state=name, accrued=total, paid=paid, balance=balance, carry_used=carry_used) if fresh_target
              else tx.payout_text(state=name, saved_total=saved_total, fresh_total=fresh_total, target=target, paid=paid, balance=balance, carry_used=carry_used)),
        items=items, carry_in=max(carry_in, 0), carry_used=carry_used, advance_out=0 if fresh_target else max(0, max(carry_in, 0) + paid - total))


def _once_per_revision(fresh_total_of):
    """The fresh total of a sheet is needed twice by a payout (the balance before the write, the state after it): one calculation per (sheet, revision)."""
    done: Dict[Tuple[UUID, int], Optional[int]] = {}

    def get(sheet):
        key = (sheet.id, sheet.revision)
        if key not in done:
            done[key] = fresh_total_of(sheet)
        return done[key]

    return get


def lock_advance_chain(db, org_id: str, teacher_id: UUID) -> None:
    """Money-1 Г-5 / Р-8: ONE lock per teacher for every write that moves his money from month to month - a payout (create, void) and a save or a recalculation of a sheet.  The advance that a month
    hands on is read from the other months, whose sheets are not locked: two tabs (an advance of month M and a payout of M+1, or a re-save of M-1) each saw the other's money as not yet there.
    Taken BEFORE the lock of the sheet, by every writer, so the order never crosses."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"payroll-advance:{org_id}:{teacher_id}"})


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

    def carry_ins(self, org_id: str, teacher_ids: Iterable[UUID], month: date) -> Dict[UUID, int]:
        """R3 3: ``{teacher: the advance that arrives in ``month``}`` - one pair of queries for any number of teachers.  Walks the months before ``month`` (any gap passes the advance
        through): ``carry = max(0, carry + paid_live - saved_total)``; a month without a saved sheet counts as 0, a month without payouts as 0 paid."""
        ids = list(teacher_ids)
        if not ids:
            return {}
        sheets = self.db.query(PayrollSheet.teacher_id, PayrollSheet.period_month, PayrollSheet.total_amount).filter(
            PayrollSheet.org_id == org_id, PayrollSheet.teacher_id.in_(ids), PayrollSheet.period_month < month).all()
        paid = self.db.query(PayrollPayout.teacher_id, PayrollPayout.period_month, func.sum(PayrollPayout.amount)).filter(
            PayrollPayout.org_id == org_id, PayrollPayout.teacher_id.in_(ids), PayrollPayout.period_month < month,
            PayrollPayout.voided_at.is_(None)).group_by(PayrollPayout.teacher_id, PayrollPayout.period_month).all()
        months: Dict[UUID, Dict[date, List[int]]] = {}
        for teacher, first, total in sheets:
            months.setdefault(teacher, {}).setdefault(first, [0, 0])[0] = int(total)
        for teacher, first, amount in paid:
            months.setdefault(teacher, {}).setdefault(first, [0, 0])[1] = int(amount)
        from .closed_period import ClosedPeriods

        periods = ClosedPeriods.of(self.db, org_id)
        out: Dict[UUID, int] = {}
        for teacher in ids:
            carry = 0
            for first in sorted(months.get(teacher, {})):
                if not periods.in_advance_chain(teacher, first):
                    continue                                       # R3 #20: a month saved inside itself is only a stage: it neither takes nor hands on an advance (Г-5: a reopened one is in the chain)
                total, got = months[teacher][first]
                carry = max(0, carry + got - total)
            out[teacher] = carry
        return out

    def carry_chain(self, org_id: str, teacher_id: UUID, months: Iterable[date]) -> Dict[date, int]:
        """Money-1 R3 (К11): ``carry_ins`` of ONE teacher for several months in one pass - the same walk over the earlier months (an open month neither takes nor hands on an advance), two queries
        and one load of the closed months for any number of months."""
        wanted = sorted(set(months))
        if not wanted:
            return {}
        sheets = {r[0]: int(r[1]) for r in self.db.query(PayrollSheet.period_month, PayrollSheet.total_amount).filter(
            PayrollSheet.org_id == org_id, PayrollSheet.teacher_id == teacher_id, PayrollSheet.period_month < wanted[-1])}
        paid = {r[0]: int(r[1]) for r in self.db.query(PayrollPayout.period_month, func.sum(PayrollPayout.amount)).filter(
            PayrollPayout.org_id == org_id, PayrollPayout.teacher_id == teacher_id, PayrollPayout.period_month < wanted[-1],
            PayrollPayout.voided_at.is_(None)).group_by(PayrollPayout.period_month)}
        from .closed_period import ClosedPeriods

        periods = ClosedPeriods.of(self.db, org_id)
        events = sorted(set(sheets) | set(paid))
        out: Dict[date, int] = {}
        carry, at = 0, 0
        for month in wanted:
            while at < len(events) and events[at] < month:
                first = events[at]
                at += 1
                if not periods.in_advance_chain(teacher_id, first):
                    continue
                carry = max(0, carry + paid.get(first, 0) - sheets.get(first, 0))
            out[month] = carry
        return out

    def state_in_month(self, org_id: str, teacher_id: UUID, month: date, *, saved_total: int, fresh_total: Optional[int], items: List[PayoutOut], carry_in: int) -> PayoutState:
        """THE payout state of a (teacher, month): the sheet, its payout block, the Excel and the LIST of the month (the row of the teacher; its header is the sum of the rows) all call this one function
        (stage walk D-2: the list called ``state_of`` without the month's openness - an open month showed «переплачено 105 000» against the sheet's «29 166»).  R3 #20: an OPEN month (not saved after
        it ended, or reopened) is by the fresh total, a CLOSED one by the saved total."""
        from .closed_period import ClosedPeriods

        month_open = ClosedPeriods.of(self.db, org_id).closed_for([teacher_id], month) is None
        return state_of(saved_total=saved_total, fresh_total=fresh_total, items=items, carry_in=carry_in, month_open=month_open)

    def state_for(self, org_id: str, sheet: PayrollSheet, fresh_total: Optional[int],
                  all_payouts: Optional[Dict[Tuple[UUID, date], List[PayoutOut]]] = None, carry_in: Optional[int] = None) -> PayoutState:
        items = (all_payouts if all_payouts is not None else self.payouts_of(org_id, month=sheet.period_month, teacher_ids=[sheet.teacher_id])
                 ).get((sheet.teacher_id, sheet.period_month), [])
        if carry_in is None:
            carry_in = self.carry_ins(org_id, [sheet.teacher_id], sheet.period_month).get(sheet.teacher_id, 0)
        return self.state_in_month(org_id, sheet.teacher_id, sheet.period_month, saved_total=int(sheet.total_amount), fresh_total=fresh_total, items=items, carry_in=carry_in)

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
        if paid_on < ps._years_ago(today, MAX_PAID_AGO_YEARS):   # MN-34: the payments' own rule (29 February included)
            raise PayoutDateRange("Дата выплаты старше 3 лет")
        if body.method is None:
            raise PayoutMethodRequired("Укажите способ выплаты")
        return body.amount, paid_on, body.method

    def create(self, org_id: str, sheet_id: UUID, actor_id: str, body: PayoutCreate, key: Optional[UUID], fresh_total_of) -> PayoutCreated:
        """``fresh_total_of(sheet) -> Optional[int]`` is the payroll's fresh calculation of the sheet (asked once per revision of the sheet in a request)."""
        today = ps.business_today()
        fresh_total_of = _once_per_revision(fresh_total_of)
        try:
            amount, paid_on, method = self._check(body, today)
            # wave C MN-33: the calculation of the month does not depend on the lock (it reads lessons, payments and rates, not the payouts): it runs BEFORE the sheet is
            # locked; under the lock the same call finds the answer - unless the sheet got another revision meanwhile (then it is calculated again, under the lock, as before).
            probe = self.db.query(PayrollSheet).filter(PayrollSheet.id == sheet_id, PayrollSheet.org_id == org_id).first()
            if probe is not None:
                fresh_total_of(probe)
            if probe is not None:
                lock_advance_chain(self.db, org_id, probe.teacher_id)
            sheet = (self.db.query(PayrollSheet).filter(PayrollSheet.id == sheet_id, PayrollSheet.org_id == org_id)
                     .with_for_update().populate_existing().first())
            if sheet is None:
                raise SheetNotSaved("Сначала сохраните зарплату за месяц")
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
            # MN-32: the sheet is locked, so a parallel payout is already counted; an advance is written only on an explicit yes to THE SUM the owner saw (R3 7): another real
            # overpay (a payout from another tab meanwhile) is the question again, with the new sum.  The balance is after the advance of the earlier months (R3 3).
            left = max(self.state_for(org_id, sheet, fresh_total_of(sheet)).balance, 0)
            excess = max(amount - left, 0)
            if excess > 0 and not (body.confirm_overpay and body.overpay == excess):
                raise OverpayConfirmRequired(f"Выплата больше остатка на {tx.money(excess)}. Если окно не предложило записать выплату вперёд — обновите страницу и повторите",
                                             overpay=excess, balance=left)
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
            lock_advance_chain(self.db, org_id, probe.teacher_id)
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
