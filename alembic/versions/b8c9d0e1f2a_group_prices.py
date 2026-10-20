"""Group price + individual price of a student: ``group_prices``, ``membership_price_overrides`` (ROADMAP #105).

Revision ID: b8c9d0e1f2a
Revises: a7b8c9d0e1f
Create Date: 2026-10-17

Until this revision the price lived in ``student_groups.monthly_fee`` only (one number per membership). Now:

* ``group_prices`` - the price of a GROUP: ``id``, ``group_id`` -> ``groups.id`` ON DELETE CASCADE, ``amount`` BIGINT
  NOT NULL (>= 0), ``valid_from`` DATE NOT NULL (a business day, Tashkent), ``note`` (200), ``created_by`` ->
  ``users.id`` (NULL on a row made here), ``created_at`` (naive UTC). UNIQUE(``group_id``, ``valid_from``). The
  price on day *d* is the row with the greatest ``valid_from <= d``; no row - "цена не задана".
* ``membership_price_overrides`` - the explicit exception of one student: ``membership_id`` -> ``student_groups.id``
  ON DELETE CASCADE, ``amount`` BIGINT NULL (>= 0; NULL = "from this day the price of the group again"),
  ``valid_from`` DATE NOT NULL, ``reason`` (200), ``created_by``, ``created_at``. UNIQUE(``membership_id``,
  ``valid_from``). CHECK: a sum needs a non-blank reason.

Backfill (the rules of the plan, ``plan_backfill`` below - the dry-run script ``scripts/group_price_dry_run.py``
imports the very same function, so what it prints is what this migration does):

1. Per group: the price is the MOST FREQUENT non-NULL ``monthly_fee`` among its ACTIVE memberships of students that
   are not archived; a tie goes to the amount set LATER (``monthly_fee_updated_at``; NULL is the oldest), then to the
   SMALLER amount. ``valid_from = 2000-01-01`` ("it has always been so": past months are calculated as they are
   now). The price needs AT LEAST 2 VOTES (``MIN_VOTES``): one student's fee - possibly a discount - must not become
   the price of the whole group. A group whose most frequent fee has fewer votes (a single fee, or every fee
   different), or that has no fee at all, gets NO price; a tie still needs 2+ votes.
2. EVERY membership (inactive and archived included) whose ``monthly_fee`` is not NULL and differs from the price of
   its group (or the group has none - also a group left without a price by the rule above) keeps its fee as an exception ``valid_from = 2000-01-01``, reason «Цена до введения цены
   группы» - so no student's price changes.
3. A membership with ``monthly_fee`` NULL in a group that got a price starts to follow the group price - an intended
   change (in a group without a price nothing changes: nobody silently starts paying); run ``scripts/group_price_dry_run.py`` on production before the release to see which groups and how many.

``student_groups.monthly_fee`` (+ ``_updated_by`` / ``_updated_at``) STAYS (rollback safety): the code no longer
reads or writes it; a LATER migration drops it. Do not drop it before the image that stopped using it has been live
and the rollback window is over.

Audit trail: ``group_audit.action`` (CHECK ``ck_group_audit_action``) also accepts ``group_price_set`` and
``membership_price_set`` (a write of a group price / of an individual price) and ``group_price_cancelled`` /
``membership_price_cancelled`` (a SCHEDULED price taken back: its row is deleted) - every one leaves a row there, in the
same transaction (see ``src/auth/pricing.py``).  The rows the backfill below makes are NOT audited (no author, no change).

Idempotent: the tables are created only when missing (a database built from the models by ``create_all`` and stamped
back is fine); the backfill inserts ``ON CONFLICT DO NOTHING`` on the unique keys, so a re-run changes nothing that
exists.

Rollback (downgrade): ``monthly_fee`` of EVERY membership is refilled with its effective price TODAY (the business
day of Tashkent: the last exception with an amount, else the price of the group; NULL when neither exists), then
both tables are dropped, the four price actions' rows of ``group_audit`` are deleted and the
action CHECK goes back to the previous list. The prices set after the release are thus kept in the old column; the
history of price changes (and its audit trail) is lost. An older auth image cannot start on a database at this head (unknown revision): downgrade first,
roll the image back afterwards.
"""
# (no ``from __future__ import annotations``: Alembic and scripts/group_price_dry_run.py load this file without registering
# it in ``sys.modules``, which ``dataclasses`` needs to resolve string annotations)
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID, uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b8c9d0e1f2a"
down_revision: str = "a7b8c9d0e1f"
branch_labels = None
depends_on = None

GROUP_PRICES = "group_prices"
OVERRIDES = "membership_price_overrides"
EPOCH = date(2000, 1, 1)
OVERRIDE_REASON = "Цена до введения цены группы"
GROUP_NOTE = "Перенос из «оплаты в месяц» учеников"
MIN_VOTES = 2  # the most frequent fee needs this many active memberships to become the price of a group

# ``group_audit.action``: the list of the previous revision (student archive) and the list with the price actions.
GROUP_AUDIT = "group_audit"
PRICE_ACTIONS = ("group_price_set", "membership_price_set", "group_price_cancelled", "membership_price_cancelled")
GROUP_AUDIT_OLD = "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored')"
GROUP_AUDIT_NEW = (
    "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored', "
    "'group_price_set', 'membership_price_set', 'group_price_cancelled', 'membership_price_cancelled')"
)

# The memberships with a fee, their group and organisation (the names are for the dry-run report only).
# "Active" = the membership is active (NULL is not) and the student is not archived.
FETCH_SQL = """
SELECT sg.id            AS membership_id,
       sg.group_id      AS group_id,
       g.org_id         AS org_id,
       o.name           AS org_name,
       g.name           AS group_name,
       sg.monthly_fee   AS monthly_fee,
       sg.monthly_fee_updated_at AS fee_updated_at,
       (sg.is_active IS TRUE AND s.status IS DISTINCT FROM 'archived') AS active
  FROM student_groups sg
  JOIN groups g ON g.id = sg.group_id
  JOIN students s ON s.id = sg.student_id
  JOIN organizations o ON o.id = g.org_id
 ORDER BY o.name, g.name, sg.id
"""


# ============================================================================
# The backfill rules (pure: no database)
# ============================================================================


@dataclass
class GroupPlan:
    """What the migration does with one group (``plan_backfill``)."""

    group_id: UUID
    org_id: UUID
    org_name: str
    group_name: str
    price: Optional[int] = None  # the price the group gets; None - no price
    votes: int = 0  # active memberships that have that fee
    tie: bool = False  # another amount had as many votes (decided by the date, then the smaller sum)
    memberships: int = 0
    active: int = 0
    follow_active: int = 0  # active memberships that follow the price: the fee equals it, or is NULL
    follow_inactive: int = 0
    null_active: int = 0  # of those: the fee is NULL - they START to follow the price (a change)
    null_inactive: int = 0
    overrides: List[Tuple[UUID, int, bool]] = field(default_factory=list)  # (membership, fee, active)

    @property
    def override_active(self) -> int:
        return sum(1 for _, _, active in self.overrides if active)

    @property
    def single_vote(self) -> bool:
        """Some fee exists but none has ``MIN_VOTES`` votes: the group gets no price, every fee stays an exception."""
        return self.price is None and self.votes > 0


def choose_group_price(fees: Iterable[Tuple[int, Optional[datetime]]]) -> Tuple[Optional[int], int, bool]:
    """``(price, votes, tie)`` from the ``(fee, fee_updated_at)`` of the ACTIVE memberships of one group.

    The most frequent fee wins; among equals the one set later (the latest ``fee_updated_at`` of its holders, NULL is
    the oldest), then the smaller sum.  It is a price only with ``MIN_VOTES`` votes or more: below that ``price`` is
    None (``votes`` is still the top count, ``tie`` still says whether several fees shared it), so the dry run can
    tell "a single fee" from "no fee at all" (``votes == 0``).
    """
    votes: Counter = Counter()
    latest: Dict[int, datetime] = {}
    for fee, updated_at in fees:
        votes[fee] += 1
        stamp = updated_at or datetime.min
        if fee not in latest or stamp > latest[fee]:
            latest[fee] = stamp
    if not votes:
        return None, 0, False
    top = max(votes.values())
    contenders = [fee for fee, n in votes.items() if n == top]
    best = max(contenders, key=lambda fee: (latest[fee], -fee))
    return (int(best) if top >= MIN_VOTES else None), top, len(contenders) > 1


def plan_backfill(rows: Iterable[Any]) -> List[GroupPlan]:
    """The plan for every group that has memberships, from the rows of ``FETCH_SQL``."""
    by_group: Dict[UUID, List[Any]] = {}
    for row in rows:
        by_group.setdefault(row["group_id"], []).append(row)
    plans: List[GroupPlan] = []
    for group_id, members in by_group.items():
        first = members[0]
        plan = GroupPlan(group_id, first["org_id"], first["org_name"] or "", first["group_name"] or "")
        plan.memberships = len(members)
        plan.active = sum(1 for m in members if m["active"])
        plan.price, plan.votes, plan.tie = choose_group_price(
            (int(m["monthly_fee"]), m["fee_updated_at"])
            for m in members
            if m["active"] and m["monthly_fee"] is not None
        )
        for m in members:
            fee, active = m["monthly_fee"], bool(m["active"])
            if fee is None:
                if plan.price is not None:  # follows the price of the group from now on
                    if active:
                        plan.follow_active += 1
                        plan.null_active += 1
                    else:
                        plan.follow_inactive += 1
                        plan.null_inactive += 1
            elif plan.price is not None and int(fee) == plan.price:
                if active:
                    plan.follow_active += 1
                else:
                    plan.follow_inactive += 1
            else:
                plan.overrides.append((m["membership_id"], int(fee), active))
        plans.append(plan)
    plans.sort(key=lambda p: (p.org_name.casefold(), p.group_name.casefold(), str(p.group_id)))
    return plans


def fetch_rows(connection) -> List[Dict[str, Any]]:
    """The rows ``plan_backfill`` needs (read-only)."""
    return [dict(row) for row in connection.execute(sa.text(FETCH_SQL)).mappings().all()]


# ============================================================================
# Upgrade / downgrade
# ============================================================================


def _replace_check(table: str, name: str, expression: str, *, not_valid: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    # Older local databases may carry the PostgreSQL default name instead.
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_action_check")
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})"
        + (" NOT VALID" if not_valid else "")
    )


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if GROUP_PRICES not in tables:
        op.create_table(
            GROUP_PRICES,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "group_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("amount", sa.BigInteger(), nullable=False),
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("note", sa.String(200), nullable=True),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("group_id", "valid_from", name="uq_group_prices_group_from"),
            sa.CheckConstraint("amount >= 0", name="ck_group_prices_amount"),
        )
    if OVERRIDES not in tables:
        op.create_table(
            OVERRIDES,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "membership_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("student_groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("amount", sa.BigInteger(), nullable=True),
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("reason", sa.String(200), nullable=True),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("membership_id", "valid_from", name="uq_membership_price_overrides_from"),
            sa.CheckConstraint("amount IS NULL OR amount >= 0", name="ck_membership_price_overrides_amount"),
            sa.CheckConstraint(
                "amount IS NULL OR (reason IS NOT NULL AND length(btrim(reason)) > 0)",
                name="ck_membership_price_overrides_reason",
            ),
        )

    # ---- audit trail: the two price actions are allowed in ``group_audit``
    _replace_check(GROUP_AUDIT, "ck_group_audit_action", GROUP_AUDIT_NEW)

    # ---- backfill: the rules in the module docstring
    plans = plan_backfill(fetch_rows(bind))
    price_rows = [
        {"id": uuid4(), "group_id": p.group_id, "amount": p.price, "valid_from": EPOCH, "note": GROUP_NOTE}
        for p in plans
        if p.price is not None
    ]
    override_rows = [
        {"id": uuid4(), "membership_id": membership_id, "amount": fee, "valid_from": EPOCH, "reason": OVERRIDE_REASON}
        for p in plans
        for membership_id, fee, _ in p.overrides
    ]
    if price_rows:
        bind.execute(
            sa.text(
                f"INSERT INTO {GROUP_PRICES} (id, group_id, amount, valid_from, note, created_by, created_at) "
                "VALUES (:id, :group_id, :amount, :valid_from, :note, NULL, (now() AT TIME ZONE 'UTC')) "
                "ON CONFLICT (group_id, valid_from) DO NOTHING"
            ),
            price_rows,
        )
    if override_rows:
        bind.execute(
            sa.text(
                f"INSERT INTO {OVERRIDES} (id, membership_id, amount, valid_from, reason, created_by, created_at) "
                "VALUES (:id, :membership_id, :amount, :valid_from, :reason, NULL, (now() AT TIME ZONE 'UTC')) "
                "ON CONFLICT (membership_id, valid_from) DO NOTHING"
            ),
            override_rows,
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if GROUP_PRICES in tables and OVERRIDES in tables:
        # The effective price TODAY (business day, Tashkent): the last exception with an amount, else the price of
        # the group; NULL when neither exists.  An exception with a NULL amount ("the group price again") falls to
        # the group price through COALESCE.
        op.execute(
            f"""
            WITH today AS (SELECT (now() AT TIME ZONE 'Asia/Tashkent')::date AS d),
            last_override AS (
                SELECT DISTINCT ON (o.membership_id) o.membership_id, o.amount
                  FROM {OVERRIDES} o, today
                 WHERE o.valid_from <= today.d
                 ORDER BY o.membership_id, o.valid_from DESC
            ),
            last_group AS (
                SELECT DISTINCT ON (p.group_id) p.group_id, p.amount
                  FROM {GROUP_PRICES} p, today
                 WHERE p.valid_from <= today.d
                 ORDER BY p.group_id, p.valid_from DESC
            )
            UPDATE student_groups sg
               SET monthly_fee = COALESCE(
                       (SELECT lo.amount FROM last_override lo WHERE lo.membership_id = sg.id),
                       (SELECT lg.amount FROM last_group lg WHERE lg.group_id = sg.group_id)
                   )
            """
        )
    op.execute(f"DROP TABLE IF EXISTS {OVERRIDES}")
    op.execute(f"DROP TABLE IF EXISTS {GROUP_PRICES}")
    # The audit trail of the prices goes with the prices; only then can the old list of actions come back.
    actions = ", ".join(f"'{action}'" for action in PRICE_ACTIONS)
    op.execute(f"DELETE FROM {GROUP_AUDIT} WHERE action IN ({actions})")
    _replace_check(GROUP_AUDIT, "ck_group_audit_action", GROUP_AUDIT_OLD, not_valid=True)
