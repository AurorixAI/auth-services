"""
ALGO - the price of a group and the exception of one student (ROADMAP #105).

Until #105 the price lived in ``student_groups.monthly_fee`` only.  Now:

* ``group_prices`` - the price of a GROUP and the business day (Tashkent) from which it applies.  The price on
  day *d* is the row with the greatest ``valid_from <= d``; no such row - "цена не задана";
* ``membership_price_overrides`` - the explicit exception of ONE student: a sum with a mandatory reason, or
  ``amount NULL`` = "from this day the price of the group again".  It hangs on the membership, so it is not
  carried by a transfer: in the new group the student takes the price of the new group.

The effective price of a membership on a day is ONE rule (:meth:`PriceBooks.price` / :func:`effective_price`):
the last override with ``valid_from <= d`` if it exists and has an amount, else the price of the group on *d*,
else none.  It also tells where the price came from (``group`` / ``override`` / ``none``).

The meaning of the sum follows the centre's billing mode: "Оплата в месяц" for calendar periods, "Стоимость пакета"
for lesson packages (``payments.lessons_per_payment``).

Batch loading: :func:`load_books` reads the history of any number of groups and memberships in TWO queries, after
which any number of days is resolved without the database (payroll prices every lesson of a month this way).

The old column ``student_groups.monthly_fee`` stays for the rollback only: nothing here or anywhere else in the
code reads or writes it.

Writes
------
* :meth:`PricingService.set_group_price` - one transaction under ``SELECT ... FOR NO KEY UPDATE`` of the GROUP
  row.  That is the only row a price write holds, so it can not take part in a deadlock with the membership
  writers (student -> memberships -> groups FOR UPDATE, see ``AuthService._lock_groups_for_membership_change``): a
  price write never waits for anything after it got the group lock.  ``FOR NO KEY UPDATE`` does not block the
  ``FOR KEY SHARE`` of the inserts that point at the group (attendance, payments).  The same ``valid_from`` again
  replaces the amount of that day - a deliberate correction of a row that existed when the request was made.  A row
  of that day that somebody wrote WHILE this request waited for the lock was not seen by the caller: that is a 409
  ``PRICE_CONFLICT`` ("the price was just changed, refresh and repeat"), not a silent overwrite; the same request
  twice (a double click) is simply done.  A unique-key collision that still slips through, and a deadlock, are the
  same 409, never a 500.  ``dry_run`` computes the impact and writes nothing.
* :meth:`PricingService.set_override` - under ``FOR NO KEY UPDATE`` of the membership (the same lock as every other
  writer of money of a membership), refused for an archived student.  :func:`write_override` is the bare write used
  inside another transaction (adding a student to a group with an individual price).  The audit row points at the
  group (FOR KEY SHARE): a membership change that holds the group FOR UPDATE and then asks for this membership
  (``remove_student_from_group``) can close a lock cycle, which PostgreSQL breaks as a deadlock - a 409 here too.

Impact of a change of the group price (what the confirmation window shows): ``students_following`` (active
students of the group who follow the group price on the day the change reaches them), ``students_with_override``
(who do not) and ``stale_payroll_sheets`` - the saved payroll sheets that would change: for every month from
``valid_from`` on that has a saved sheet with a line of this group, the data fingerprint of the sheet's teacher is
computed twice, with the current prices and with the new one (``PayrollService.sheets_changed_by_group_price``);
a sheet whose fingerprint differs would turn "данные изменились".  Payments already entered never change: the
amount is stored in the payment itself.

Audit trail
-----------
Every write of a group price and of an individual price leaves ONE row in ``group_audit`` (``group_price_set`` /
``membership_price_set``), inserted in the SAME transaction as the price row: both are committed or both are rolled
back, whatever fails in between.  That covers a new row, a same-day correction and "вернуть цену группы" (amount
NULL).  The request that changes nothing - the same amount, note / reason and ``valid_from`` as the row already there
(a double click) - writes no second audit row and does not touch the price row either.  ``snapshot`` (JSONB):

* group price: ``actor_id``, ``group_id``, ``valid_from`` (ISO date), ``old_amount``, ``new_amount``, ``note``,
  ``old_note``, ``corrected`` (the day had a row that was replaced) and ``impact`` (``students_following``,
  ``students_with_override``, ``stale_payroll_sheets`` - the numbers the confirmation window showed);
* individual price: ``actor_id``, ``group_id``, ``membership_id``, ``student_id``, ``valid_from``, ``old_amount``,
  ``old_source`` (``group`` / ``override`` / ``none``), ``new_amount`` (NULL = "the group price again"), ``reason``,
  ``old_reason``, ``corrected``.

``old_amount`` is the price in force on ``valid_from`` just BEFORE the write: the row of that very day that is being
replaced, otherwise the last step before it (NULL if there is none).  For an individual price it is the EFFECTIVE price
of the membership that day, so ``old_source`` says whether it was the group price or an exception (a replaced
"group price again" row falls to the group price).  The trail is read through the ``changes`` of the ``history`` items
(:meth:`PricingService.group_prices`, :meth:`PricingService.override_history`) - money roles only.

Money never reaches a teacher: the only callers are the endpoints under ``require_payments_access`` and the
services that build money responses.  Logs carry ids and the actor, never amounts.
"""
from __future__ import annotations

import functools
import logging
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from src.models.auth import (
    Group,
    GroupAudit,
    GroupPrice,
    MembershipPriceOverride,
    OrganizationMember,
    Student,
    StudentGroup,
    User,
)

from . import payment_status as ps
from .payments_files import staff_name

logger = logging.getLogger(__name__)

SOURCE_GROUP = "group"
SOURCE_OVERRIDE = "override"
SOURCE_NONE = "none"

ACTION_GROUP_PRICE = "group_price_set"  # group_audit.action of a write of a group price
ACTION_MEMBERSHIP_PRICE = "membership_price_set"  # ... of an individual price (also "the group price again")

# "Always so": the valid_from of the prices the migration made from the old per-student fees (past months are
# calculated as they were).  Also the earliest day a price can start.
EPOCH = date(2000, 1, 1)
MAX_AHEAD_DAYS = 366  # a price announced up to a year ahead
MAX_MONEY = 10_000_000_000  # the bound of every sum of the payments module
MAX_NOTE = 200
MIN_REASON = 3
MAX_REASON = 200

PRICE_CONFLICT_MESSAGE = "Цену только что изменили. Обновите страницу и повторите."
ARCHIVED_STUDENT_MESSAGE = "Ученик в архиве — сначала верните его"


# ============================================================================
# Errors (mapped to HTTP by the router)
# ============================================================================


class PriceError(Exception):
    status_code = 400
    code: Optional[str] = None

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        self.message = message
        if code:
            self.code = code
        super().__init__(message)

    @property
    def detail(self) -> Any:
        return {"code": self.code, "message": self.message} if self.code else self.message


class PriceNotFound(PriceError):
    status_code = 404


class PriceInvalid(PriceError):
    status_code = 422


class PriceConflict(PriceError):
    status_code = 409


def _guard(method):
    """A deadlock PostgreSQL still finds is a 409 to repeat, never a 500."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except OperationalError as exc:
            if getattr(getattr(exc, "orig", None), "pgcode", None) != "40P01":
                raise
            self.db.rollback()
            logger.warning("deadlock in %s; the request was rolled back", method.__name__)
            raise PriceConflict(PRICE_CONFLICT_MESSAGE, code="PRICE_CONFLICT") from exc

    return wrapper


# ============================================================================
# The rule: books and the effective price
# ============================================================================


def _uuid(value: Any) -> Optional[UUID]:
    if value is None or isinstance(value, UUID):
        return value
    return UUID(str(value))


@dataclass(frozen=True)
class Price:
    """The effective price of a membership on one day.

    ``source``: ``group`` (the price of the group), ``override`` (the individual price), ``none`` (not set).
    ``valid_from`` - the day the row that gave the price started.  ``group_amount`` - the price of the group on
    that day (what the student would pay without the exception; None when the group has no price).  ``reason`` -
    the reason of an individual price.
    """

    amount: Optional[int]
    source: str
    valid_from: Optional[date] = None
    group_amount: Optional[int] = None
    reason: Optional[str] = None

    @property
    def is_set(self) -> bool:
        return self.amount is not None


NO_PRICE = Price(None, SOURCE_NONE)


class GroupBook:
    """The price history of ONE group: ``at(day)`` is the row with the greatest ``valid_from <= day``."""

    __slots__ = ("_days", "_amounts")

    def __init__(self, steps: Iterable[Tuple[date, int]] = ()) -> None:
        ordered = sorted(steps)
        self._days: List[date] = [day for day, _ in ordered]
        self._amounts: List[int] = [int(amount) for _, amount in ordered]

    def at(self, day: date) -> Optional[Tuple[date, int]]:
        i = bisect_right(self._days, day) - 1
        return None if i < 0 else (self._days[i], self._amounts[i])

    def with_step(self, valid_from: date, amount: int) -> "GroupBook":
        """The history with one more step (the same day is replaced) - for "what if" calculations."""
        steps = {day: value for day, value in zip(self._days, self._amounts)}
        steps[valid_from] = int(amount)
        return GroupBook(steps.items())

    def steps(self) -> List[Tuple[date, int]]:
        return list(zip(self._days, self._amounts))


class OverrideBook:
    """The exception history of ONE membership: ``at(day)`` is the row with the greatest ``valid_from <= day``."""

    __slots__ = ("_days", "_rows")

    def __init__(self, rows: Iterable[Tuple[date, Optional[int], Optional[str]]] = ()) -> None:
        ordered = sorted(rows, key=lambda row: row[0])
        self._days: List[date] = [row[0] for row in ordered]
        self._rows: List[Tuple[date, Optional[int], Optional[str]]] = [
            (day, None if amount is None else int(amount), reason) for day, amount, reason in ordered
        ]

    def at(self, day: date) -> Optional[Tuple[date, Optional[int], Optional[str]]]:
        i = bisect_right(self._days, day) - 1
        return None if i < 0 else self._rows[i]


_EMPTY_GROUP = GroupBook()
_EMPTY_OVERRIDES = OverrideBook()


@dataclass
class PriceBooks:
    """The loaded histories of a set of groups and memberships; resolves prices without the database."""

    groups: Dict[UUID, GroupBook] = field(default_factory=dict)
    overrides: Dict[UUID, OverrideBook] = field(default_factory=dict)

    def group_book(self, group_id: Any) -> GroupBook:
        return self.groups.get(_uuid(group_id), _EMPTY_GROUP)

    def override_book(self, membership_id: Any) -> OverrideBook:
        return self.overrides.get(_uuid(membership_id), _EMPTY_OVERRIDES)

    def group_price(self, group_id: Any, day: date) -> Optional[Tuple[date, int]]:
        return self.group_book(group_id).at(day)

    def price(self, membership_id: Any, group_id: Any, day: date) -> Price:
        """THE rule of the effective price (see the module docstring)."""
        group_row = self.group_book(group_id).at(day)
        group_amount = group_row[1] if group_row else None
        override = self.override_book(membership_id).at(day)
        if override is not None and override[1] is not None:
            return Price(override[1], SOURCE_OVERRIDE, override[0], group_amount, override[2])
        if group_row is not None:
            return Price(group_row[1], SOURCE_GROUP, group_row[0], group_amount)
        return NO_PRICE

    def with_group_books(self, books: Dict[UUID, GroupBook]) -> "PriceBooks":
        """The same books with some group histories replaced ("what if the price of this group were ...")."""
        return PriceBooks({**self.groups, **books}, self.overrides)


def load_books(db: Session, group_ids: Iterable[Any], membership_ids: Iterable[Any]) -> PriceBooks:
    """Price history of many groups and memberships in TWO queries (no query per group, student or day)."""
    gids = {_uuid(g) for g in group_ids if g is not None}
    mids = {_uuid(m) for m in membership_ids if m is not None}
    group_steps: Dict[UUID, List[Tuple[date, int]]] = {g: [] for g in gids}
    override_rows: Dict[UUID, List[Tuple[date, Optional[int], Optional[str]]]] = {m: [] for m in mids}
    if gids:
        for group_id, valid_from, amount in db.query(
            GroupPrice.group_id, GroupPrice.valid_from, GroupPrice.amount
        ).filter(GroupPrice.group_id.in_(gids)):
            group_steps[group_id].append((valid_from, int(amount)))
    if mids:
        for membership_id, valid_from, amount, reason in db.query(
            MembershipPriceOverride.membership_id,
            MembershipPriceOverride.valid_from,
            MembershipPriceOverride.amount,
            MembershipPriceOverride.reason,
        ).filter(MembershipPriceOverride.membership_id.in_(mids)):
            override_rows[membership_id].append((valid_from, amount, reason))
    return PriceBooks(
        {g: GroupBook(steps) for g, steps in group_steps.items()},
        {m: OverrideBook(rows) for m, rows in override_rows.items()},
    )


def effective_price(db: Session, membership: StudentGroup, day: date, *, books: Optional[PriceBooks] = None) -> Price:
    """The price of ``membership`` on the business day ``day``.

    ``books`` (from :func:`load_books`) answers without a query; without them the history of this one
    membership and its group is loaded.
    """
    if books is None:
        books = load_books(db, [membership.group_id], [membership.id])
    return books.price(membership.id, membership.group_id, day)


def price_dict(price: Price) -> Dict[str, Any]:
    """The ``price`` object of the money responses."""
    return {
        "amount": price.amount,
        "source": price.source,
        "valid_from": price.valid_from,
        "group_amount": price.group_amount,
        "reason": price.reason,
    }


# ============================================================================
# Validation
# ============================================================================


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def check_amount(amount: Any) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise PriceInvalid("Сумма должна быть целым числом")
    if amount < 0 or amount > MAX_MONEY:
        raise PriceInvalid(f"Сумма — от 0 до {MAX_MONEY:,}".replace(",", " "))
    return amount


def check_valid_from(valid_from: date, today: date) -> date:
    if valid_from < EPOCH:
        raise PriceInvalid("Дата «действует с» слишком далеко в прошлом")
    if valid_from > today + timedelta(days=MAX_AHEAD_DAYS):
        raise PriceInvalid("Дата «действует с» слишком далеко в будущем — не позже чем через год")
    return valid_from


def clean_text(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def check_override_input(amount: Optional[int], reason: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """The rules of an individual price: a sum needs a reason of 3..200 characters; "back to the group price" does not."""
    reason = clean_text(reason)
    if amount is None:
        if reason is not None and len(reason) > MAX_REASON:
            raise PriceInvalid(f"Причина — не более {MAX_REASON} символов")
        return None, reason
    check_amount(amount)
    if reason is None or len(reason) < MIN_REASON:
        raise PriceInvalid(
            f"Укажите причину индивидуальной цены — от {MIN_REASON} до {MAX_REASON} символов", code="REASON_REQUIRED"
        )
    if len(reason) > MAX_REASON:
        raise PriceInvalid(f"Причина — не более {MAX_REASON} символов")
    return amount, reason


def _audit_role(db: Session, org_id: UUID, actor: UUID) -> str:
    """The ``group_audit.actor_role`` of the author of a price write: his role in the centre (owner / admin - the
    only ones the money guard lets in), a platform superadmin, else admin (like ``student_archive.actor_role_for_audit``)."""
    role = (
        db.query(OrganizationMember.org_role)
        .filter(
            OrganizationMember.org_id == org_id,
            OrganizationMember.user_id == actor,
            OrganizationMember.is_active.is_(True),
        )
        .scalar()
    )
    if role in ("owner", "admin"):
        return role
    return "superadmin" if db.query(User.role).filter(User.id == actor).scalar() == "superadmin" else "admin"


def _add_audit(
    db: Session,
    *,
    org_id: UUID,
    group_id: UUID,
    teacher_id: Optional[UUID],
    actor: UUID,
    action: str,
    snapshot: Dict[str, Any],
    at: datetime,
) -> GroupAudit:
    """The audit row of a price write.  Flushed, never committed: it lives and dies with the transaction of the write."""
    row = GroupAudit(
        org_id=org_id,
        group_id=group_id,
        actor_id=actor,
        actor_role=_audit_role(db, org_id, actor),
        action=action,
        assigned_teacher_id=teacher_id,
        snapshot=snapshot,
        created_at=at,
    )
    db.add(row)
    db.flush()
    return row


def write_override(
    db: Session,
    membership_id: Any,
    actor_id: Optional[str],
    *,
    amount: Optional[int],
    valid_from: date,
    reason: Optional[str],
    seen_before: Optional[datetime] = None,
) -> MembershipPriceOverride:
    """Insert (or, for the same day, replace) an exception row inside the caller's transaction, with its audit row.

    The caller holds the lock of the membership and has validated the input (:func:`check_override_input`).
    ``seen_before`` - the instant the request was made: a row of that day written at or after it is one the caller
    has not seen (somebody wrote it while this request waited for the lock) - 409, unless it says the same thing.
    The same ``amount`` and ``reason`` as the row of that day is a repeated request: nothing is written, no audit row.
    Otherwise ONE ``membership_price_set`` row goes to ``group_audit`` (module docstring) in the caller's
    transaction.  Nothing is committed here; a write without an author is refused (the trail has no row without one).
    """
    actor = _uuid(actor_id)
    if actor is None:
        raise ValueError("A price write needs its author: the audit trail has no row without an actor")
    mid = _uuid(membership_id)
    row = (
        db.query(MembershipPriceOverride)
        .filter(MembershipPriceOverride.membership_id == mid, MembershipPriceOverride.valid_from == valid_from)
        .first()
    )
    if row is not None:
        if row.amount == amount and (row.reason or None) == (reason or None):
            return row  # the same request twice: nothing changes, nothing to audit
        if seen_before is not None and row.created_at is not None and row.created_at >= seen_before:
            raise PriceConflict(PRICE_CONFLICT_MESSAGE, code="PRICE_CONFLICT")
    ctx = (
        db.query(StudentGroup.group_id, StudentGroup.student_id, Group.org_id, Group.teacher_id)
        .join(Group, Group.id == StudentGroup.group_id)
        .filter(StudentGroup.id == mid)
        .first()
    )
    if ctx is None:
        raise PriceNotFound("Членство не найдено")
    # what the membership paid that day before this write (the row being replaced is part of the books)
    old = load_books(db, [ctx.group_id], [mid]).price(mid, ctx.group_id, valid_from)
    corrected, old_reason = row is not None, (row.reason if row is not None else None)
    now = _utcnow()
    if row is not None:
        row.amount, row.reason, row.created_by, row.created_at = amount, reason, actor, now
    else:
        row = MembershipPriceOverride(
            membership_id=mid, amount=amount, valid_from=valid_from, reason=reason, created_by=actor, created_at=now
        )
        db.add(row)
    db.flush()
    _add_audit(
        db,
        org_id=ctx.org_id,
        group_id=ctx.group_id,
        teacher_id=ctx.teacher_id,
        actor=actor,
        action=ACTION_MEMBERSHIP_PRICE,
        snapshot={
            "actor_id": str(actor),
            "group_id": str(ctx.group_id),
            "membership_id": str(mid),
            "student_id": str(ctx.student_id),
            "valid_from": valid_from.isoformat(),
            "old_amount": old.amount,
            "old_source": old.source,
            "old_reason": old_reason,
            "new_amount": amount,
            "reason": reason,
            "corrected": corrected,
        },
        at=now,
    )
    return row


# ============================================================================
# Service
# ============================================================================


class PricingService:
    def __init__(self, db: Session) -> None:
        self.db = db

    # ---------------------------------------------------------------- reading

    def _group(self, org_id: str, group_id: UUID, *, lock: bool = False) -> Group:
        query = self.db.query(Group).filter(Group.id == group_id, Group.org_id == org_id)
        if lock:
            # FOR NO KEY UPDATE: exclusive among price writers (and the FOR UPDATE of the membership changes), but
            # the FOR KEY SHARE of inserts that point at the group (attendance, payments) does not wait for it.
            query = query.with_for_update(of=Group, key_share=True).populate_existing()
        group = query.first()
        if group is None:
            raise PriceNotFound("Группа не найдена")
        return group

    def _label(self, org_id: str) -> Tuple[str, str]:
        """``(period_mode, price_label)``: what the sum means in this centre."""
        from .payments_service import PaymentService  # lazy: pulls in the status maths

        mode = PaymentService(self.db).get_settings(org_id).period_mode
        return mode, ("Оплата в месяц" if mode == ps.CALENDAR else "Стоимость пакета")

    def _names(self, ids: Iterable[Optional[UUID]]) -> Dict[UUID, Optional[str]]:
        wanted = {i for i in ids if i}
        if not wanted:
            return {}
        return {
            u.id: staff_name(u.first_name, u.last_name)
            for u in self.db.query(User).filter(User.id.in_(wanted)).all()
        }

    def _audit_by_day(
        self, group_id: UUID, action: str, *, membership_id: Optional[UUID] = None
    ) -> Tuple[Dict[Optional[str], List[GroupAudit]], Dict[UUID, Optional[str]]]:
        """The audit rows of ``action`` of a group (of one membership), newest first, keyed by the ``valid_from`` (ISO
        date) of their snapshot - and the names of their authors.  Two queries however many rows there are."""
        query = self.db.query(GroupAudit).filter(GroupAudit.group_id == group_id, GroupAudit.action == action)
        if membership_id is not None:
            query = query.filter(GroupAudit.snapshot["membership_id"].astext == str(membership_id))
        by_day: Dict[Optional[str], List[GroupAudit]] = {}
        rows = query.order_by(GroupAudit.created_at.desc(), GroupAudit.id.desc()).all()
        for row in rows:
            by_day.setdefault((row.snapshot or {}).get("valid_from"), []).append(row)
        return by_day, self._names(row.actor_id for row in rows)

    def group_prices(self, org_id: str, group_id: UUID) -> Dict[str, Any]:
        """``current`` (on the business today) and the whole ``history`` of a group, newest first.

        Every ``history`` item carries ``changes`` - the audit trail of THAT ``valid_from`` (``group_audit``, action
        ``group_price_set``), newest first: ``at`` (naive UTC, ISO), ``actor_name``, ``old_amount`` (in force on that
        day before the write; null - none), ``new_amount`` and ``note``.  The first item is the write that created the
        row, the next ones are its corrections; a row made by the migration has none.  Money roles only: the
        endpoint is under ``require_payments_access``."""
        group = self._group(org_id, group_id)
        today = ps.business_today()
        rows = (
            self.db.query(GroupPrice)
            .filter(GroupPrice.group_id == group.id)
            .order_by(GroupPrice.valid_from.desc())
            .all()
        )
        book = GroupBook((r.valid_from, r.amount) for r in rows)
        current = book.at(today)
        names = self._names(r.created_by for r in rows)
        mode, label = self._label(org_id)
        by_day = {r.valid_from: r for r in rows}
        trail, trail_names = self._audit_by_day(group.id, ACTION_GROUP_PRICE)
        return {
            "group_id": str(group.id),
            "group_name": group.name,
            "as_of": today.isoformat(),
            "period_mode": mode,
            "price_label": label,
            "current": (
                {
                    "amount": current[1],
                    "valid_from": current[0],
                    "note": by_day[current[0]].note,
                }
                if current
                else None
            ),
            "history": [
                {
                    "id": str(r.id),
                    "amount": int(r.amount),
                    "valid_from": r.valid_from,
                    "note": r.note,
                    "created_by": str(r.created_by) if r.created_by else None,
                    "created_by_name": names.get(r.created_by),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "is_current": bool(current and r.valid_from == current[0]),
                    "is_future": r.valid_from > today,
                    "changes": [
                        {
                            "at": a.created_at.isoformat() if a.created_at else None,
                            "actor_name": trail_names.get(a.actor_id),
                            "old_amount": a.snapshot.get("old_amount"),
                            "new_amount": a.snapshot.get("new_amount"),
                            "note": a.snapshot.get("note"),
                        }
                        for a in trail.get(r.valid_from.isoformat(), [])
                    ],
                }
                for r in rows
            ],
        }

    def _membership(self, org_id: str, membership_id: UUID, *, lock: bool = False) -> StudentGroup:
        query = (
            self.db.query(StudentGroup)
            .join(Student, Student.id == StudentGroup.student_id)
            .join(Group, Group.id == StudentGroup.group_id)
            .filter(StudentGroup.id == membership_id, Student.org_id == org_id, Group.org_id == org_id)
        )
        if lock:
            # FOR NO KEY UPDATE, like every other writer of the money of a membership (see PaymentService._membership)
            query = query.with_for_update(of=StudentGroup, key_share=True).populate_existing()
        membership = query.first()
        if membership is None:
            raise PriceNotFound("Членство не найдено")
        return membership

    def override_history(self, org_id: str, membership_id: UUID) -> Dict[str, Any]:
        """The exception history of a membership (newest first) and the price it has today.

        Every ``history`` item carries ``changes`` - the audit trail of THAT ``valid_from`` (``group_audit``, action
        ``membership_price_set``), newest first: ``at``, ``actor_name``, ``old_amount`` and ``old_source`` (the
        EFFECTIVE price of the membership that day before the write and where it came from: ``group`` / ``override``
        / ``none``), ``new_amount`` (null - "the group price again") and ``reason``.  Money roles only."""
        membership = self._membership(org_id, membership_id)
        today = ps.business_today()
        rows = (
            self.db.query(MembershipPriceOverride)
            .filter(MembershipPriceOverride.membership_id == membership.id)
            .order_by(MembershipPriceOverride.valid_from.desc())
            .all()
        )
        books = load_books(self.db, [membership.group_id], [membership.id])
        current = books.override_book(membership.id).at(today)
        names = self._names(r.created_by for r in rows)
        mode, label = self._label(org_id)
        trail, trail_names = self._audit_by_day(
            membership.group_id, ACTION_MEMBERSHIP_PRICE, membership_id=membership.id
        )
        return {
            "membership_id": str(membership.id),
            "group_id": str(membership.group_id),
            "as_of": today.isoformat(),
            "period_mode": mode,
            "price_label": label,
            "price": price_dict(books.price(membership.id, membership.group_id, today)),
            "history": [
                {
                    "id": str(r.id),
                    "amount": None if r.amount is None else int(r.amount),
                    "valid_from": r.valid_from,
                    "reason": r.reason,
                    "created_by": str(r.created_by) if r.created_by else None,
                    "created_by_name": names.get(r.created_by),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "is_current": bool(current and r.valid_from == current[0]),
                    "is_future": r.valid_from > today,
                    "changes": [
                        {
                            "at": a.created_at.isoformat() if a.created_at else None,
                            "actor_name": trail_names.get(a.actor_id),
                            "old_amount": a.snapshot.get("old_amount"),
                            "old_source": a.snapshot.get("old_source"),
                            "new_amount": a.snapshot.get("new_amount"),
                            "reason": a.snapshot.get("reason"),
                        }
                        for a in trail.get(r.valid_from.isoformat(), [])
                    ],
                }
                for r in rows
            ],
        }

    # ----------------------------------------------------------------- impact

    def impact(
        self,
        org_id: str,
        group: Group,
        amount: int,
        valid_from: date,
        today: date,
        *,
        owner_view: bool,
    ) -> Dict[str, Any]:
        """What a price of ``amount`` from ``valid_from`` would touch (writes nothing).

        Students are counted on the day the change reaches them, ``max(valid_from, today)``: active students of
        the group (not archived) who follow the group price, and who carry an individual price.  ``owner_view``
        adds the list of the sheets (payroll is the owner's alone); the count is always given.
        """
        from .payroll_service import PayrollService  # lazy: payroll pulls in the payments service

        rows = (
            self.db.query(StudentGroup.id)
            .join(Student, Student.id == StudentGroup.student_id)
            .filter(
                StudentGroup.group_id == group.id,
                StudentGroup.is_active.is_(True),
                Student.org_id == org_id,
                Student.status != "archived",
            )
            .all()
        )
        ids = [r[0] for r in rows]
        books = load_books(self.db, [group.id], ids)
        reach = max(valid_from, today)
        with_override = sum(
            1 for mid in ids if (o := books.override_book(mid).at(reach)) is not None and o[1] is not None
        )
        book = books.group_book(group.id).with_step(valid_from, amount)
        sheets = PayrollService(self.db).sheets_changed_by_group_price(org_id, group.id, book, valid_from)
        return {
            "students_following": len(ids) - with_override,
            "students_with_override": with_override,
            "stale_payroll_sheets": len(sheets),
            "stale_sheets": [
                {
                    "sheet_id": s["sheet_id"],
                    "month": s["month"],
                    "teacher_id": s["teacher_id"],
                    "teacher_name": s["teacher_name"],
                }
                for s in sheets
            ]
            if owner_view
            else [],
        }

    # ----------------------------------------------------------------- writing

    @_guard
    def set_group_price(
        self,
        org_id: str,
        group_id: UUID,
        actor_id: str,
        *,
        amount: int,
        valid_from: Optional[date] = None,
        note: Optional[str] = None,
        dry_run: bool = False,
        owner_view: bool = False,
    ) -> Dict[str, Any]:
        """Set the price of a group from a day (default: today).  A past day is allowed - the impact says what it
        touches.  The same day again corrects the amount of that day; a row of that day written by somebody else
        while this request waited for the lock is a 409 ``PRICE_CONFLICT`` (see the module docstring).  The same
        amount and note as the row of that day (a double click) change nothing and leave no audit row; every other
        write leaves one ``group_price_set`` row in ``group_audit``, committed with the price.  ``dry_run``: only the
        impact, nothing is written and no lock is taken."""
        requested_at = _utcnow()
        today = ps.business_today()
        amount = check_amount(amount)
        valid_from = check_valid_from(valid_from or today, today)
        note = clean_text(note)
        if note is not None and len(note) > MAX_NOTE:
            raise PriceInvalid(f"Примечание — не более {MAX_NOTE} символов")
        try:
            group = self._group(org_id, group_id, lock=not dry_run)
            impact = self.impact(org_id, group, amount, valid_from, today, owner_view=owner_view)
            if dry_run:
                self.db.rollback()
                return {"dry_run": True, "impact": impact, "current": None, "history": None}
            now, actor = _utcnow(), _uuid(actor_id)
            row = (
                self.db.query(GroupPrice)
                .filter(GroupPrice.group_id == group.id, GroupPrice.valid_from == valid_from)
                .first()
            )
            if row is not None and (int(row.amount), row.note or None) == (amount, note):
                pass  # the same request twice (a double click, or one that raced and lost): nothing to write or audit
            elif row is not None and row.created_at is not None and row.created_at >= requested_at:
                # written while this request waited for the lock: the caller has not seen it
                raise PriceConflict(PRICE_CONFLICT_MESSAGE, code="PRICE_CONFLICT")
            else:
                # what was in force on that day before this write: the row being replaced, else the last step before it
                before = row or (
                    self.db.query(GroupPrice)
                    .filter(GroupPrice.group_id == group.id, GroupPrice.valid_from < valid_from)
                    .order_by(GroupPrice.valid_from.desc())
                    .first()
                )
                old_amount = None if before is None else int(before.amount)
                corrected, old_note = row is not None, (row.note if row is not None else None)
                if row is not None:
                    row.amount, row.note, row.created_by, row.created_at = amount, note, actor, now
                else:
                    self.db.add(
                        GroupPrice(
                            group_id=group.id, amount=amount, valid_from=valid_from, note=note, created_by=actor,
                            created_at=now,
                        )
                    )
                self.db.flush()
                _add_audit(
                    self.db,
                    org_id=group.org_id,
                    group_id=group.id,
                    teacher_id=group.teacher_id,
                    actor=actor,
                    action=ACTION_GROUP_PRICE,
                    snapshot={
                        "actor_id": str(actor),
                        "group_id": str(group.id),
                        "valid_from": valid_from.isoformat(),
                        "old_amount": old_amount,
                        "old_note": old_note,
                        "new_amount": amount,
                        "note": note,
                        "corrected": corrected,
                        "impact": {
                            key: impact[key]
                            for key in ("students_following", "students_with_override", "stale_payroll_sheets")
                        },
                    },
                    at=now,
                )
            self.db.commit()
        except IntegrityError as exc:  # a concurrent write of the same day slipped through
            self.db.rollback()
            logger.info("group price write lost a race group=%s", group_id)
            raise PriceConflict(PRICE_CONFLICT_MESSAGE, code="PRICE_CONFLICT") from exc
        except PriceError:
            self.db.rollback()
            raise
        except Exception:  # whatever fails after the first insert takes the price AND its audit row down together
            self.db.rollback()
            raise
        logger.info("group price set group=%s valid_from=%s actor=%s", group_id, valid_from, actor_id)
        out = self.group_prices(org_id, group_id)
        out.update({"dry_run": False, "impact": impact})
        return out

    @_guard
    def set_override(
        self,
        org_id: str,
        membership_id: UUID,
        actor_id: str,
        *,
        amount: Optional[int],
        valid_from: Optional[date] = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The individual price of a student from a day (reason mandatory), or ``amount=None``: the group price again.

        One ``membership_price_set`` row goes to ``group_audit`` in the same transaction (module docstring); the same
        amount and reason as the row of that day (a double click) change nothing and leave no audit row."""
        requested_at = _utcnow()
        today = ps.business_today()
        amount, reason = check_override_input(amount, reason)
        valid_from = check_valid_from(valid_from or today, today)
        try:
            membership = self._membership(org_id, membership_id, lock=True)
            status = self.db.query(Student.status).filter(Student.id == membership.student_id).scalar()
            if status == "archived":
                raise PriceConflict(ARCHIVED_STUDENT_MESSAGE)
            write_override(
                self.db, membership.id, actor_id, amount=amount, valid_from=valid_from, reason=reason,
                seen_before=requested_at,
            )
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            logger.info("price override write lost a race membership=%s", membership_id)
            raise PriceConflict(PRICE_CONFLICT_MESSAGE, code="PRICE_CONFLICT") from exc
        except PriceError:
            self.db.rollback()
            raise
        except Exception:  # the exception and its audit row go together
            self.db.rollback()
            raise
        logger.info(
            "price override set membership=%s valid_from=%s cleared=%s actor=%s",
            membership_id, valid_from, amount is None, actor_id,
        )
        return self.override_history(org_id, membership_id)


# ============================================================================
# Groups for the money roles
# ============================================================================


def current_group_prices(db: Session, group_ids: Sequence[Any], day: Optional[date] = None) -> Dict[UUID, Optional[Dict[str, Any]]]:
    """``{group_id: {"amount", "valid_from"} | None}`` on ``day`` (default: the business today) - the
    ``price_current`` of the group list and card, for the money roles only."""
    day = day or ps.business_today()
    books = load_books(db, group_ids, [])
    out: Dict[UUID, Optional[Dict[str, Any]]] = {}
    for gid in {_uuid(g) for g in group_ids}:
        row = books.group_book(gid).at(day)
        out[gid] = {"amount": row[1], "valid_from": row[0]} if row else None
    return out
