"""
ALGO - OPENING-BULK: «Остатки на начало» - the table of every membership and the mass entry of the opening balances.

No new money maths: an opening balance is the same fact ``membership_opening_balances`` that the card of a student writes (LOGIC-A, decision 1);
a batch = N times ``OpeningService.apply`` in ONE transaction (the facts it reads are loaded once for the batch, so the number of queries does not
depend on N).  The dry run is the technique of ``previewPaymentSettings``: the ledger of every touched membership BEFORE (the facts as they are) and
AFTER (a second loader whose openings are replaced by the planned ones; the ledger is a pure function).  The apply repeats the plan under the locks
(organisation -> students by id -> their memberships by id) and writes only when the answer is the one the client saw (``digest``).

Spec: ``artifacts/partner_doc_check/opening_bulk_spec.md`` (the sections «Решения ведущего» and «Решение владельца» win).  Contract:
``openings_schemas.py`` (frozen).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID, uuid4

from sqlalchemy.orm.attributes import flag_modified

from src.models.auth import (
    Group,
    MembershipOpeningBalance,
    MembershipPause,
    MembershipTransfer,
    Organization,
    Student,
    StudentGroup,
    StudentPayment,
    StudentRefund,
    StudentWriteoff,
    User,
)

from . import billing_ledger as bl
from . import ledger_service as ls
from . import membership_periods
from . import payment_status as ps
from . import payments_texts as tx
from . import pricing
from .logica_service import AUDIT_OPENING_SET, AUDIT_OPENING_VOID, OpeningFacts, OpeningService, _Base
from .openings_schemas import (
    OpeningBatchApplied,
    OpeningBatchImpact,
    OpeningBatchItem,
    OpeningBatchPreview,
    OpeningBatchPreviewRequest,
    OpeningBatchRequest,
    OpeningBatchSummary,
    OpeningGroupBlock,
    OpeningGroupRef,
    OpeningItemResult,
    OpeningItemState,
    OpeningLessons,
    OpeningMembershipRow,
    OpeningsList,
    OpeningsSummary,
    OpeningStudentRef,
    OpeningTotals,
    OpeningValue,
    OpeningWarning,
)
from .payments_files import person_name
from .payments_schemas import PriceOut
from .payments_service import (
    ConcurrentEdit,
    PaymentConflict,
    PaymentError,
    PaymentInvalid,
    PaymentNotFound,
    ReasonRequired,
    VoidBreaksRefund,
    _person,
    deadlock_guard,
    invalidate_summary,
)

logger = logging.getLogger(__name__)

MAX_ITEMS = 2000                 # rows of one batch
MAX_TABLE_ROWS = 2000            # rows of the table / the file without a group
MAX_IMPORT_BYTES = 5 * 1024 * 1024
KEEP_BATCHES = 20                # the idempotency keys kept in the settings of the centre
MAX_LESSONS_LEFT = 400
LESSONS_SEARCH_DAYS = 400
PAID_UNTIL_FAR_DAYS = 120
DEBT_LARGE_PRICES = 12
STATE_KEY = "openings_state"
BATCHES_KEY = "openings_batches"
STATES = ("none", "pending", "done", "skipped")


# ---- errors (the codes of the contract) -----------------------------------------------------------------------------


class OpeningNotFound(PaymentNotFound):
    code = "NOT_FOUND"


class StaleItems(PaymentConflict):
    code = "STALE_ITEMS"


class PreviewStale(PaymentConflict):
    code = "PREVIEW_STALE"


class IdempotencyMismatch(PaymentConflict):
    code = "IDEMPOTENCY_MISMATCH"


class BatchInvalid(PaymentInvalid):
    code = "BATCH_INVALID"


class TooManyItems(PaymentInvalid):
    code = "TOO_MANY_ITEMS"


class TooManyRows(PaymentInvalid):
    code = "TOO_MANY_ROWS"


class EmptyBatch(PaymentInvalid):
    code = "EMPTY_BATCH"


# ---- the table ------------------------------------------------------------------------------------------------------


@dataclass
class TableRow:
    """One membership of the table with everything a row needs (loaded for the whole centre in a fixed number of queries)."""

    m: StudentGroup
    s: Student
    g: Group
    teacher: Optional[User]
    first_day: date
    floor: date
    left_on: Optional[date]
    opening: Optional[MembershipOpeningBalance]
    money: bool
    transfer: bool
    free: bool
    paused: bool
    price: pricing.Price
    missing: bool

    @property
    def left(self) -> bool:
        return self.left_on is not None

    @property
    def version(self) -> str:
        return str(self.opening.id) if self.opening is not None else "-"

    @property
    def needs_review(self) -> bool:
        """The stored opening no longer fits the range (the accounting start moved after it was entered): shown, never changed silently."""
        o = self.opening
        return o is not None and bool((o.paid_until and o.paid_until < self.floor) or (o.debt_on and o.debt_on > self.floor))

    @property
    def editable(self) -> bool:
        return not (self.transfer or self.free)

    @property
    def student_name(self) -> str:
        return person_name(self.s.last_name, self.s.first_name) or ""


def value_of(row: Optional[MembershipOpeningBalance]) -> Optional[OpeningValue]:
    if row is None:
        return None
    return OpeningValue(paid_until=row.paid_until, debt_amount=int(row.debt_amount) if row.debt_amount else None, debt_on=row.debt_on)


def _state_of(block: Dict[str, Any]) -> str:
    value = block.get(STATE_KEY)
    return value if value in STATES else "none"


def _short(day: date) -> str:
    return day.strftime("%d.%m")


class OpeningsService(_Base):
    # ------------------------------------------------------------------------------------------------ the table

    def _table(self, org_id: str, today: date) -> Tuple[ls.LedgerSettings, List[TableRow], List[TableRow], str]:
        """``(settings, tracked rows, left rows, state)``: every row of the centre, ~10 queries whatever the number of students."""
        settings = ls.load_settings(self.db, org_id)
        start = settings.accounting_start
        base = (
            self.db.query(StudentGroup, Student, Group, User)
            .join(Student, Student.id == StudentGroup.student_id)
            .join(Group, Group.id == StudentGroup.group_id)
            .outerjoin(User, User.id == Group.teacher_id)
            .filter(Student.org_id == org_id, Group.org_id == org_id, Student.status != "archived", Group.status == "active")
        )
        tracked = base.filter(StudentGroup.is_active.is_(True)).all()
        left = []
        for m, s, g, u in base.filter(StudentGroup.is_active.is_(False), StudentGroup.left_at.isnot(None)).all():
            if ps.business_date(m.left_at) < start:
                left.append((m, s, g, u))
        everyone = tracked + left
        ids = [m.id for m, *_ in everyone]
        periods = membership_periods.periods_by_membership(self.db, ids)
        openings = {o.membership_id: o for o in self.db.query(MembershipOpeningBalance).filter(
            MembershipOpeningBalance.org_id == org_id, MembershipOpeningBalance.voided_at.is_(None))}
        money = set()
        for model, extra in ((StudentPayment, StudentPayment.kind == "payment"), (StudentWriteoff, None), (StudentRefund, None)):
            query = self.db.query(model.membership_id).filter(model.org_id == org_id, model.voided_at.is_(None))
            if extra is not None:
                query = query.filter(extra)
            money.update(r[0] for r in query.distinct())
        transfers = {r[0] for r in self.db.query(MembershipTransfer.to_membership_id).filter(
            MembershipTransfer.org_id == org_id, MembershipTransfer.voided_at.is_(None)).distinct()}
        paused = {r[0] for r in self.db.query(MembershipPause.membership_id).filter(
            MembershipPause.org_id == org_id, MembershipPause.voided_at.is_(None), MembershipPause.first_day <= today,
            MembershipPause.last_day >= today).distinct()}
        books = pricing.load_books(self.db, {g.id for _, _, g, _ in everyone}, ids)
        block = self.pay._settings_dict(org_id)
        out: List[TableRow] = []
        for m, s, g, u in everyone:
            spans = periods.get(m.id) or []
            first_day = ps.business_date(spans[0].joined_at) if spans else (ps.business_date(m.joined_at) if m.joined_at else today)
            price = books.price(m.id, g.id, today)
            row = TableRow(
                m=m, s=s, g=g, teacher=u, first_day=first_day, floor=OpeningService.floor_of(first_day, start),
                left_on=None if m.is_active else ps.business_date(m.left_at), opening=openings.get(m.id), money=m.id in money,
                transfer=m.id in transfers, free=price.amount == 0, paused=m.id in paused, price=price, missing=False)
            row.missing = (m.is_active and row.editable and row.opening is None and not row.money and first_day < start)
            out.append(row)
        n = len(tracked)
        return settings, out[:n], out[n:], _state_of(block)

    @staticmethod
    def _totals(tracked: Sequence[TableRow], left: Sequence[TableRow], shown: int) -> OpeningTotals:
        return OpeningTotals(
            memberships=len(tracked), with_opening=sum(1 for r in tracked if r.opening is not None),
            missing=sum(1 for r in tracked if r.missing), left_available=len(left), shown=shown,
            needs_review=sum(1 for r in tracked if r.needs_review))

    def _row_out(self, r: TableRow, mode: str) -> OpeningMembershipRow:
        return OpeningMembershipRow(
            membership_id=str(r.m.id),
            student=OpeningStudentRef(id=str(r.s.id), name=r.student_name, display_id=r.s.display_id, class_level=r.s.class_level),
            group=OpeningGroupRef(id=str(r.g.id), name=r.g.name), mode=mode, price=PriceOut(**pricing.price_dict(r.price)),
            enrolled_on=r.first_day, status="left" if r.left else "active", left_on=r.left_on, paused=r.paused, opening=value_of(r.opening),
            version=r.version, has_money_records=r.money, missing=r.missing, needs_review=r.needs_review, floor=r.floor, only_debt=r.left, editable=r.editable,
            reason_not_editable="transfer" if r.transfer else ("free" if r.free else None))

    @staticmethod
    def _matches(r: TableRow, words: List[str]) -> bool:
        hay = " ".join(x for x in (r.s.last_name, r.s.first_name, r.s.display_id) if x).casefold()
        return all(w in hay for w in words)

    def rows(self, org_id: str, *, group_id: Optional[UUID] = None, missing_only: bool = False, q: Optional[str] = None,
             include_left: bool = False, limit: int = MAX_TABLE_ROWS,
             too_many: str = "Слишком много учеников — выберите группу") -> Tuple[ls.LedgerSettings, List[TableRow], OpeningTotals, str, date]:
        """The filtered rows of the table (sorted by group, then by the name of the student) + the centre-wide totals."""
        today = ps.business_today()
        if group_id is not None and self.db.query(Group.id).filter(Group.id == group_id, Group.org_id == org_id).first() is None:
            raise PaymentNotFound("Группа не найдена")
        settings, tracked, left, state = self._table(org_id, today)
        rows = list(tracked) + (list(left) if include_left else [])
        if group_id is not None:
            rows = [r for r in rows if r.g.id == group_id]
        words = [w.casefold() for w in (q or "").split()]
        if words:
            rows = [r for r in rows if self._matches(r, words)]
        if missing_only:
            rows = [r for r in rows if r.missing]
        if group_id is None and len(rows) > limit:
            raise TooManyRows(too_many)
        rows.sort(key=lambda r: ((r.g.name or "").casefold(), str(r.g.id), r.left, r.student_name.casefold(), str(r.m.id)))
        return settings, rows, self._totals(tracked, left, len(rows)), state, today

    def list(self, org_id: str, *, group_id: Optional[UUID] = None, missing_only: bool = False, q: Optional[str] = None,
             include_left: bool = False) -> OpeningsList:
        settings, rows, totals, state, today = self.rows(org_id, group_id=group_id, missing_only=missing_only, q=q, include_left=include_left)
        mode = settings.current.mode
        blocks: Dict[UUID, OpeningGroupBlock] = {}
        for r in rows:
            block = blocks.get(r.g.id)
            if block is None:
                block = blocks[r.g.id] = OpeningGroupBlock(id=str(r.g.id), name=r.g.name, teacher_name=_person(r.teacher), memberships=[])
            block.memberships.append(self._row_out(r, mode))
            block.total += 1
            block.with_opening += 1 if r.opening is not None else 0
            block.missing += 1 if r.missing else 0
        return OpeningsList(
            as_of=today, accounting_start=settings.accounting_start, state=state, period_mode=mode,
            paid_until_max=today + timedelta(days=366), totals=totals, groups=list(blocks.values()))

    def summary(self, org_id: str) -> OpeningsSummary:
        today = ps.business_today()
        settings, tracked, left, state = self._table(org_id, today)
        return OpeningsSummary(accounting_start=settings.accounting_start, state=state, totals=self._totals(tracked, left, 0))

    # ------------------------------------------------------------------------------------------------ the state

    def set_state(self, org_id: str, actor_id: str, state: str) -> OpeningsSummary:
        """«Готово» / «Не нужно» (the owner, an admin with the right to money - the router checks).  ``pending`` is the release script's."""
        if state not in ("done", "skipped"):
            raise PaymentInvalid("Состояние: done или skipped")
        org = self.db.query(Organization).filter(Organization.id == org_id).with_for_update().first()
        if org is None:
            raise PaymentNotFound("Организация не найдена")
        settings = dict(org.settings or {})
        block = dict(settings.get("payments") or {})
        block[STATE_KEY] = state
        block["openings_state_by"] = str(actor_id)
        block["openings_state_at"] = ps.business_today().isoformat()
        org.settings = {**settings, "payments": block}
        flag_modified(org, "settings")
        self.db.commit()
        logger.info("openings state org=%s actor=%s state=%s", org_id, actor_id, state)
        return self.summary(org_id)

    # ------------------------------------------------------------------------------------------------ the plan

    def _lock(self, student_ids: Sequence[UUID]) -> None:
        """Students by id, then every membership of those students by id (the family of the transfers): the order of the project."""
        ids = list(student_ids)
        self.db.query(Student.id).filter(Student.id.in_(ids)).order_by(Student.id).with_for_update(key_share=True).all()
        self.db.query(StudentGroup.id).filter(StudentGroup.student_id.in_(ids)).order_by(StudentGroup.id).with_for_update(key_share=True).all()

    def _plan(self, org_id: str, items: Sequence[OpeningBatchItem], today: date, *, lock: bool = False, max_items: int = MAX_ITEMS) -> "Plan":
        if not items:
            raise EmptyBatch("Нечего проверять: список строк пуст")
        if len(items) > max_items:
            raise TooManyItems(f"Не больше {MAX_ITEMS} строк за один раз — сохраните по группам")
        parsed: List[Tuple[OpeningBatchItem, Optional[UUID]]] = []
        for it in items:
            try:
                parsed.append((it, UUID(str(it.membership_id))))
            except ValueError:
                parsed.append((it, None))
        ids = list(dict.fromkeys(mid for _, mid in parsed if mid is not None))
        found = {
            row[0]: row[1] for row in self.db.query(StudentGroup.id, StudentGroup.student_id)
            .join(Student, Student.id == StudentGroup.student_id).join(Group, Group.id == StudentGroup.group_id)
            .filter(StudentGroup.id.in_(ids), Student.org_id == org_id, Group.org_id == org_id)
        } if ids else {}
        unknown = [str(it.membership_id) for it, mid in parsed if mid is None or mid not in found]
        if unknown:
            raise OpeningNotFound("Членство не найдено", membership_ids=list(dict.fromkeys(unknown)))
        if lock:
            self._lock(sorted(set(found.values()), key=str))
        self.db.expire_all()      # whatever this session read before the locks is stale now
        a = ls.LedgerLoader(self.db, str(org_id), today)
        a.load_facts(list(found))
        plan = Plan(org_id=str(org_id), today=today, a=a, accounting_start=a.settings.accounting_start, mode=a.settings.current.mode)
        counts: Dict[UUID, int] = {}
        for _, mid in parsed:
            counts[mid] = counts.get(mid, 0) + 1
        for it, mid in parsed:
            plan.items.append(self._resolve(plan, it, mid, duplicate=counts[mid] > 1))
        plan.ledgers_before = {r.membership.id: r.ledger for r in a.run(list(a.members))}
        for p in plan.items:
            p.before = self._state(plan.ledgers_before.get(p.mid), p.existing)
        movers = [p for p in plan.items if p.status == "ok" and p.changed]
        if movers:
            b = ls.LedgerLoader(self.db, str(org_id), today, a.settings)
            b.load_facts(list(found))
            for p in movers:
                b.openings[p.mid] = SimpleNamespace(paid_until=p.target[0], debt_amount=p.target[1], debt_on=p.target[2]) if p.target else None
            plan.ledgers_after = {r.membership.id: r.ledger for r in b.run(list(b.members))}
            for p in movers:
                ledger = plan.ledgers_after.get(p.mid)
                if ledger is not None and bl.F_OUTFLOW_EXCEEDS in ledger.flags:
                    p.fail("VOID_BREAKS_REFUND", "Сначала отмените возврат: без этого остатка возвращать нечего")
        else:
            plan.ledgers_after = dict(plan.ledgers_before)
        for p in plan.items:
            if p.status != "error":
                ledger = plan.ledgers_after.get(p.mid)
                p.after = self._state(ledger, self._target_value(p) if p.changed else p.existing)
        plan.finish()
        return plan

    @staticmethod
    def _target_value(p: "PlanItem") -> Optional[OpeningValue]:
        if not p.target:
            return None
        return OpeningValue(paid_until=p.target[0], debt_amount=p.target[1], debt_on=p.target[2])

    @staticmethod
    def _state(ledger: Optional[bl.Ledger], opening) -> Optional[OpeningItemState]:
        if ledger is None:
            return None
        r = ledger.status
        value = opening if isinstance(opening, OpeningValue) or opening is None else value_of(opening)
        return OpeningItemState(opening=value, debt=ledger.debt, advance=ledger.advance, paid_until=r.paid_until, status=r.status,
                                lessons_left=r.lessons_left)

    def _resolve(self, plan: "Plan", it: OpeningBatchItem, mid: UUID, *, duplicate: bool) -> "PlanItem":
        """The verdict on one item (no queries: everything comes from the loader of the plan)."""
        a = plan.a
        membership, student, group, _ = a.members[mid]
        p = PlanItem(item=it, mid=mid, student=student, group=group, membership=membership)
        spans = a.stays.get(mid) or []
        p.first_day = spans[0].first if spans else (ps.business_date(membership.joined_at) if membership.joined_at else plan.today)
        p.existing = a.openings.get(mid)
        p.has_money = bool(a.payments.get(mid) or a.writeoffs.get(mid) or a.refunds.get(mid))
        p.transfer_target = bool(a.t_in.get(mid))
        price = a.books.price(mid, group.id, plan.today)
        p.price = price
        start = plan.accounting_start
        if duplicate:
            return p.fail("DUPLICATE_ITEM", "Эта строка встречается в пакете дважды — ни одна не применена")
        if student.status == "archived":
            return p.fail("STUDENT_ARCHIVED", "Ученик в архиве — сначала верните его")
        left_on = None if membership.is_active else (ps.business_date(membership.left_at) if membership.left_at else None)
        if group.status != "active" or (not membership.is_active and not (left_on is not None and left_on < start)):
            return p.fail("MEMBERSHIP_CLOSED", "Ученик уже не в этой группе")
        p.left = not membership.is_active
        if it.version != p.version:
            p.stale = True
            return p.fail("STALE_VERSION", "Строку уже изменили: обновите таблицу")
        paid_until, debt_amount, debt_on = it.paid_until, it.debt_amount, it.debt_on
        if it.clear:
            if any(x is not None for x in (paid_until, debt_amount, debt_on, it.lessons_left)):
                return p.fail("CLEAR_WITH_VALUES", "«Убрать остаток» нельзя сочетать со значениями")
            paid_until = debt_amount = debt_on = None
        else:
            if it.lessons_left is not None:
                if paid_until is not None:
                    return p.fail("LESSONS_AND_PAID_UNTIL", "Укажите либо «осталось уроков», либо «оплачено до»")
                if plan.mode != ps.LESSONS:
                    return p.fail("LESSONS_NOT_APPLICABLE", "«Осталось уроков» — только для центра с оплатой пакетами уроков")
                if p.left:
                    return p.fail("OPENING_RANGE", "Для ушедших — только долг на начало")
                try:
                    paid_until = self._lessons_to_date(plan, p, it.lessons_left)
                except PaymentError as exc:
                    return p.fail(exc.code or "LESSONS_RANGE", exc.message)
                p.lessons = (it.lessons_left, paid_until)
            if p.left and paid_until is not None:
                return p.fail("OPENING_RANGE", "Для ушедших — только долг на начало")
            if paid_until is None and debt_amount is None and debt_on is None and it.lessons_left is None:
                return p.fail("EMPTY_ITEM", "Строка пустая: укажите значения или уберите остаток")
            if debt_amount is not None and debt_on is None:
                debt_on = max(p.first_day, start) - timedelta(days=1)       # the default day of a debt
                if p.left:
                    debt_on = min(debt_on, start - timedelta(days=1))
            body = SimpleNamespace(paid_until=paid_until, debt_amount=debt_amount, debt_on=debt_on)
            try:
                if not (paid_until is None and debt_amount is None and debt_on is None):
                    OpeningService(self.db).validate(body, first_day=p.first_day, accounting_start=start, today=plan.today)
            except PaymentError as exc:
                return p.fail(exc.code or "OPENING_RANGE", exc.message)
            if paid_until is not None and paid_until == start - timedelta(days=1) and debt_amount is None:
                p.warn("PAID_UNTIL_AT_START", f"«Оплачено до {_short(start - timedelta(days=1))}»: на начало учёта долга нет (строка отмечена как проверенная)")
        target = (paid_until, debt_amount, debt_on)
        empty = paid_until is None and debt_amount is None and debt_on is None
        if not empty:
            if p.transfer_target:
                return p.fail("OPENING_NOT_ALLOWED", "Остаток нельзя внести: ученик пришёл переводом")
            if price.amount == 0:
                return p.fail("FREE_MEMBERSHIP", "Бесплатное место: остаток не учитывается")
        p.target = None if empty else target
        current = None if p.existing is None else (p.existing.paid_until, int(p.existing.debt_amount or 0) or None, p.existing.debt_on)
        if p.target == current:
            p.kind, p.changed = "unchanged", False
            p.status = "noop"
        else:
            p.changed = True
            p.status = "ok"
            if p.target is None:
                p.kind = "cleared"
                p.warn("CLEARS_EXISTING", "Уберёт внесённый остаток")
            else:
                p.kind = "both" if (p.target[0] and p.target[1]) else ("paid_until" if p.target[0] else "debt")
            if p.has_money:
                p.needs_reason = True
                p.warn("HAS_MONEY", "У ученика уже есть оплаты: нужна причина")
        if p.target is not None:
            if price.amount is None:
                p.warn("NO_PRICE", "У группы нет цены: начисления не пойдут, пока цена не задана")
            if p.target[0] is not None and p.target[0] > plan.today + timedelta(days=PAID_UNTIL_FAR_DAYS):
                p.warn("PAID_UNTIL_FAR", "«Оплачено до» дальше чем через 4 месяца — проверьте дату")
            if p.target[1] and price.amount and p.target[1] > DEBT_LARGE_PRICES * price.amount:
                p.warn("DEBT_LARGE", "Долг больше 12 месячных цен — проверьте сумму")
        return p

    def _lessons_to_date(self, plan: "Plan", p: "PlanItem", left: int) -> Optional[date]:
        """«Осталось K уроков» -> «оплачено до»: the day of the K-th lesson of the group from ``max(first_day, accounting_start)`` (the schedule
        helper of the ledger).  K = 0: nothing is left = «оплачено до» the day before the accounting start (a reviewed row, nothing owed)."""
        if not 0 <= left <= MAX_LESSONS_LEFT:
            raise PaymentInvalid(f"Осталось уроков: от 0 до {MAX_LESSONS_LEFT}", )
        if left == 0:
            return plan.accounting_start - timedelta(days=1)
        first = max(p.first_day, plan.accounting_start)
        days = sorted(plan.a._lesson_days(p.group, first, first + timedelta(days=LESSONS_SEARCH_DAYS)))
        if len(days) < left:
            err = PaymentInvalid(f"В расписании группы на год вперёд только {len(days)} {tx.lessons_word(len(days))}")
            err.code = "LESSONS_RANGE"
            raise err
        return days[left - 1]

    # ------------------------------------------------------------------------------------------------ preview

    def _preview_of(self, plan: "Plan") -> OpeningBatchPreview:
        results = [p.out(plan.today) for p in plan.items]
        return OpeningBatchPreview(
            ok=not any(p.status == "error" for p in plan.items), digest=plan.digest, accounting_start=plan.accounting_start,
            summary=plan.summary, impact=plan.impact, needs_reason=[str(p.mid) for p in plan.items if p.needs_reason], items=results)

    def preview(self, org_id: str, request: OpeningBatchPreviewRequest) -> OpeningBatchPreview:
        today = ps.business_today()
        plan = self._plan(org_id, request.items, today)
        return self._preview_of(plan)

    def plan_preview(self, org_id: str, items: Sequence[OpeningBatchItem]) -> OpeningBatchPreview:
        return self._preview_of(self._plan(org_id, items, ps.business_today()))

    def _preview_with(self, org_id: str, items: Sequence[OpeningBatchItem], *, max_items: int) -> OpeningBatchPreview:
        """The classification of a file: every row, however many (the apply is limited to ``MAX_ITEMS`` changes)."""
        return self._preview_of(self._plan(org_id, items, ps.business_today(), max_items=max_items))

    # ------------------------------------------------------------------------------------------------ apply

    @staticmethod
    def _body_hash(request: OpeningBatchRequest) -> str:
        body = {"items": sorted((i.model_dump(mode="json") for i in request.items), key=lambda d: (d["membership_id"], json.dumps(d, sort_keys=True))),
                "reason": (request.reason or "").strip() or None, "digest": request.digest}
        return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @deadlock_guard
    def apply(self, org_id: str, actor_id: str, actor_role: str, request: OpeningBatchRequest) -> OpeningBatchApplied:
        today = ps.business_today()
        key = str(request.idempotency_key)
        reason = (request.reason or "").strip() or None
        try:
            if not request.items:
                raise EmptyBatch("Нечего сохранять: список строк пуст")
            if len(request.items) > MAX_ITEMS:
                raise TooManyItems(f"Не больше {MAX_ITEMS} строк за один раз — сохраните по группам")
            # (1) the centre is locked first: one batch at a time per centre (and the ring of the idempotency keys is read under the lock)
            org = self.db.query(Organization).filter(Organization.id == org_id).with_for_update().first()
            if org is None:
                raise PaymentNotFound("Организация не найдена")
            body_hash = self._body_hash(request)
            block = dict((org.settings or {}).get("payments") or {})
            for old in block.get(BATCHES_KEY) or []:
                if old.get("key") == key:
                    if old.get("body") != body_hash:
                        raise IdempotencyMismatch("Этот ключ уже использован для другого пакета")
                    self.db.rollback()
                    answer = OpeningBatchApplied.model_validate({**old["answer"], "replayed": True})
                    return answer
            # (2) the plan under the locks of the students and the memberships
            plan = self._plan(org_id, request.items, today, lock=True)
            stale = [p for p in plan.items if p.stale]
            if stale:
                fresh = [{"membership_id": str(p.mid), "version": p.version, "opening": value_of(p.existing).model_dump(mode="json") if p.existing else None}
                         for p in stale]
                raise StaleItems("Кто-то уже изменил эти строки — обновите таблицу", membership_ids=[str(p.mid) for p in stale], fresh=fresh)
            if plan.digest != request.digest:
                raise PreviewStale("Данные изменились после проверки — проверьте ещё раз", preview=self._preview_of(plan).model_dump(mode="json"))
            bad = [p for p in plan.items if p.status == "error"]
            if bad:
                raise BatchInvalid("В пакете есть строки с ошибкой — ничего не записано", items=[p.out(today).model_dump(mode="json") for p in bad])
            need = [str(p.mid) for p in plan.items if p.needs_reason]
            if need and not (reason and 3 <= len(reason) <= 500):
                raise ReasonRequired("У учеников уже есть оплаты: укажите причину изменения остатка (от 3 до 500 символов)", membership_ids=need)
            # (3) the writes: N times OpeningService.apply, the facts already known, one flush
            batch_id = uuid4()
            service = OpeningService(self.db)
            written: List[Tuple[PlanItem, Optional[MembershipOpeningBalance]]] = []
            for p in plan.items:
                if not p.changed:
                    continue
                facts = OpeningFacts(first_day=p.first_day, accounting_start=plan.accounting_start, existing=p.existing,
                                     has_money=p.has_money, transfer_target=p.transfer_target)
                body = SimpleNamespace(paid_until=p.target[0] if p.target else None, debt_amount=p.target[1] if p.target else None,
                                       debt_on=p.target[2] if p.target else None)
                row, changed = service.apply(org_id, p.membership, actor_id, actor_role, body, source="manual", today=today, reason=reason,
                                             facts=facts, flush=False)
                if not changed:  # cannot happen: the plan said it changes
                    raise ConcurrentEdit("Данные изменились во время записи — повторите")
                written.append((p, row))
            self.db.flush()
            # (4) the check of the family (one pass) and one audit row per membership
            changed_ids = [p.mid for p, _ in written]
            if changed_ids:
                for entry in ls.LedgerLoader(self.db, str(org_id), today).run(changed_ids):
                    if bl.F_OUTFLOW_EXCEEDS in entry.ledger.flags:
                        raise VoidBreaksRefund("Сначала отмените возврат: без этого остатка возвращать нечего")
            for p, row in written:
                self._audit(org_id, p.membership, actor_id, actor_role, AUDIT_OPENING_SET if row else AUDIT_OPENING_VOID, {
                    "actor_id": str(actor_id), "membership_id": str(p.membership.id), "student_id": str(p.membership.student_id),
                    "opening_id": str(row.id) if row else None,
                    "paid_until": row.paid_until.isoformat() if row and row.paid_until else None,
                    "debt_amount": int(row.debt_amount) if row and row.debt_amount else None,
                    "debt_on": row.debt_on.isoformat() if row and row.debt_on else None,
                    "reason": reason if p.has_money else None, "debt_before": p.before.debt, "debt_after": p.after.debt,
                    "advance_before": p.before.advance, "advance_after": p.after.advance,
                    "via": "batch", "batch_id": str(batch_id), "idempotency_key": key})
            # (5) the answer is remembered under the key (the ring lives in the settings of the centre) - in the same transaction
            state = _state_of(block)
            if state in ("none", "pending"):        # the last «не заполнено» row is gone: the checklist is done (the buttons stay for the rest)
                _, tracked, _, _ = self._table(org_id, today)
                if not any(r.missing for r in tracked):
                    state = block[STATE_KEY] = "done"
                    block["openings_state_by"], block["openings_state_at"] = "auto", today.isoformat()
            answer = OpeningBatchApplied(batch_id=str(batch_id), replayed=False, summary=plan.summary, impact=plan.impact, changed=len(written),
                                         state=state)
            ring = list(block.get(BATCHES_KEY) or [])
            ring.append({"key": key, "body": body_hash, "at": today.isoformat(), "answer": answer.model_dump(mode="json")})
            block[BATCHES_KEY] = ring[-KEEP_BATCHES:]
            org.settings = {**(org.settings or {}), "payments": block}
            flag_modified(org, "settings")
            self.db.commit()
        except PaymentError:
            self.db.rollback()
            raise
        invalidate_summary(org_id)
        logger.info("openings batch org=%s actor=%s batch=%s changed=%s", org_id, actor_id, batch_id, len(written))
        return answer

    # ------------------------------------------------------------------------------------------------ Excel

    def export(self, org_id: str, **filters) -> Tuple[bytes, str]:
        from .openings_excel import build_export

        return build_export(self, org_id, **filters)

    def import_preview(self, org_id: str, data: bytes):
        from .openings_excel import import_preview

        return import_preview(self, org_id, data)


# ============================================================================================================
# The plan (the result of the planner: the verdicts + the effect)
# ============================================================================================================


@dataclass
class PlanItem:
    item: OpeningBatchItem
    mid: UUID
    student: Student
    group: Group
    membership: StudentGroup
    first_day: date = date.min
    existing: Optional[MembershipOpeningBalance] = None
    has_money: bool = False
    transfer_target: bool = False
    left: bool = False
    price: Optional[pricing.Price] = None
    status: str = "ok"
    kind: str = "unchanged"
    code: Optional[str] = None
    message: Optional[str] = None
    changed: bool = False
    stale: bool = False
    needs_reason: bool = False
    target: Optional[Tuple[Optional[date], Optional[int], Optional[date]]] = None
    lessons: Optional[Tuple[int, Optional[date]]] = None
    warnings: List[OpeningWarning] = field(default_factory=list)
    before: Optional[OpeningItemState] = None
    after: Optional[OpeningItemState] = None

    @property
    def version(self) -> str:
        return str(self.existing.id) if self.existing is not None else "-"

    def fail(self, code: str, message: str) -> "PlanItem":
        self.status, self.kind, self.code, self.message, self.changed, self.needs_reason, self.target = "error", "error", code, message, False, False, None
        return self

    def warn(self, code: str, message: str) -> None:
        self.warnings.append(OpeningWarning(code=code, message=message))

    def out(self, today: date) -> OpeningItemResult:
        lessons = None
        if self.lessons is not None:
            k, day = self.lessons
            lessons = OpeningLessons(lessons_left=k, paid_until=day, text=(
                f"осталось {k} {tx.lessons_word(k)} → оплачено до {_short(day)}" if k else f"осталось 0 уроков → долга на начало нет (оплачено до {_short(day)})"))
        value = None
        if self.target is not None:
            value = OpeningValue(paid_until=self.target[0], debt_amount=self.target[1], debt_on=self.target[2])
        return OpeningItemResult(
            membership_id=str(self.mid), student_name=person_name(self.student.last_name, self.student.first_name) or "", group_name=self.group.name,
            status=self.status, kind=self.kind, code=self.code, message=self.message, changed=self.changed, target=value, lessons=lessons,
            before=self.before, after=None if self.status == "error" else self.after, has_money_records=self.has_money,
            needs_reason=self.needs_reason, warnings=self.warnings)


@dataclass
class Plan:
    org_id: str
    today: date
    a: ls.LedgerLoader
    accounting_start: date
    mode: str
    items: List[PlanItem] = field(default_factory=list)
    ledgers_before: Dict[UUID, bl.Ledger] = field(default_factory=dict)
    ledgers_after: Dict[UUID, bl.Ledger] = field(default_factory=dict)
    summary: OpeningBatchSummary = field(default_factory=OpeningBatchSummary)
    impact: OpeningBatchImpact = field(default_factory=OpeningBatchImpact)
    digest: str = ""

    def finish(self) -> None:
        s = OpeningBatchSummary(items=len(self.items))
        for p in self.items:
            if p.status == "error":
                s.errors += 1
            elif p.changed:
                s.changed += 1
                if p.kind in ("paid_until", "both"):
                    s.paid_until += 1
                if p.kind in ("debt", "both"):
                    s.debt += 1
                    s.debt_total_added += p.target[1] or 0
                if p.kind == "cleared":
                    s.cleared += 1
            else:
                s.unchanged += 1
        self.summary = s
        before, after = self.ledgers_before, self.ledgers_after
        scope = [m for m in before if m in after]
        mover_ids = {p.mid for p in self.items if p.status == "ok" and p.changed}
        self.impact = OpeningBatchImpact(
            memberships_changed=len(mover_ids),
            memberships_with_new_debt=sum(1 for m in scope if after[m].debt > before[m].debt),
            memberships_with_changed_advance=sum(1 for m in scope if after[m].advance != before[m].advance),
            debt_total_before=sum(before[m].debt for m in scope), debt_total_after=sum(after[m].debt for m in scope),
            debt_delta=sum(after[m].debt - before[m].debt for m in scope),
            debtors_before=sum(1 for m in scope if before[m].debt > 0), debtors_after=sum(1 for m in scope if after[m].debt > 0),
            advance_delta=sum(after[m].advance - before[m].advance for m in scope),
            refundable_delta=sum(after[m].refundable - before[m].refundable for m in scope),
            indirect_changed=sum(1 for m in scope if m not in mover_ids and (
                after[m].debt != before[m].debt or after[m].advance != before[m].advance or after[m].refundable != before[m].refundable)))
        plain = []
        for p in sorted(self.items, key=lambda x: str(x.mid)):
            def st(x):
                return None if x is None else [x.debt, x.advance, x.paid_until.isoformat() if x.paid_until else None, x.status, x.lessons_left]
            plain.append([str(p.mid), p.status, p.code, p.kind, [d.isoformat() if isinstance(d, date) else d for d in (p.target or ())],
                          st(p.before), st(p.after), p.version])
        blob = json.dumps({"items": plain, "impact": self.impact.model_dump(mode="json"), "start": self.accounting_start.isoformat()},
                          sort_keys=True, ensure_ascii=False)
        self.digest = hashlib.sha256(blob.encode()).hexdigest()
