"""PAY-2: payment = money.  Write-offs, refunds, transfer links, receipt snapshots, accounting start.

Revision ID: e1f2a3b4c5d
Revises: d0e1f2a3b4c
Create Date: 2026-10-21

Design: ``artifacts/partner_doc_check/pay2_tech_design.md`` §2.  A payment is a sum (``billing_ledger.run_ledger`` derives the
charges, the distribution of the money, "paid until", debt and advance).

``student_payments``
* ``method`` (cash / card / bank_transfer / click / payme / other / unknown), old rows ``unknown``;
* ``kind`` (``payment`` / ``legacy_carry``); every old carry-over copy (``carried_from_payment_id`` set, amount 0) becomes
  ``legacy_carry`` - the ledger ignores it, the UI hides it;
* ``idempotency_key`` + UNIQUE ``(org_id, idempotency_key) WHERE NOT NULL`` replace the old double-click index
  ``uq_student_payments_active_dup`` (two equal payments on one day are legal now);
* ``valid_from`` loses NOT NULL and ``ck_student_payments_period_kind`` weakens to «not both»: a new payment has no period.  The columns
  ``valid_from / valid_to / lessons_count / partial_period / carried_*`` stay as LEGACY (read only, the old period of an old row);
* ``idx_student_payments_org_valid_to`` goes; ``ix_sp_org_paid_on`` and ``ix_sp_membership_live`` come.

New tables: ``student_writeoffs``, ``student_refunds``, ``membership_transfers`` (a LINK from the old membership to the new one -
the money that travels is derived, never stored), ``payment_receipt_snapshots``.  ``group_audit`` accepts ``payment_writeoff``,
``payment_writeoff_void``, ``payment_refund``, ``payment_refund_void``.

Data (stage: converted "as money", no hybrid).  The amount of every live payment stays; its explicit period is ignored from now
on (the sum never matched the period - that is the very defect PAY-2 removes), so debts appear where the old periods hid them.
* ``membership_transfers`` are made from the live carry-over copies: ``(original.membership, carry.membership, original.carried_out_at)``;
  the money moves along by itself.
* ``organizations.settings.payments.accounting_start_date`` (only where it is not set yet): the earliest ``valid_from`` / ``paid_on``
  of the organisation's live payments, else the day of the migration (business date, Asia/Tashkent; on production: the release day).
  Days before it are never charged.
* ``scripts/pay2_convert_report.py`` prints, before and after, per organisation and membership, the old and the new "paid until" and
  the money totals (they must be equal).

Idempotent: every step is guarded by the inspector.

Rollback (downgrade).  New payments get a period back: ``valid_from = paid_on`` and ``valid_to`` = the day before the same day of the next
month (``lessons_count = lessons_per_payment`` in an organisation in lessons mode), the old CHECKs and indexes are restored (live
payments that are now identical duplicates are VOIDED with the reason «downgrade PAY-2: дубль»).  Write-offs, refunds, transfer links,
snapshots, methods and the audit rows of write-offs / refunds are LOST - export them first.  An older auth image cannot start on a database
at this head (unknown revision): downgrade first, roll the image back afterwards.
"""
from __future__ import annotations

import json
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e1f2a3b4c5d"
down_revision: str = "d0e1f2a3b4c"
branch_labels = None
depends_on = None

PAYMENTS = "student_payments"
WRITEOFFS = "student_writeoffs"
REFUNDS = "student_refunds"
TRANSFERS = "membership_transfers"
SNAPSHOTS = "payment_receipt_snapshots"
GROUP_AUDIT = "group_audit"

METHODS = "'cash', 'card', 'bank_transfer', 'click', 'payme', 'other', 'unknown'"
AUDIT_OLD = (
    "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored', "
    "'group_price_set', 'membership_price_set', 'group_price_cancelled', 'membership_price_cancelled')"
)
PAY2_ACTIONS = ("payment_writeoff", "payment_writeoff_void", "payment_refund", "payment_refund_void")
AUDIT_NEW = AUDIT_OLD[:-1] + ", " + ", ".join(f"'{a}'" for a in PAY2_ACTIONS) + ")"
DUP_INDEX = "uq_student_payments_active_dup"
VOID_REASON = "downgrade PAY-2: дубль"


def _checks(inspector, table: str) -> set:
    return {c["name"] for c in inspector.get_check_constraints(table)}


def _indexes(inspector, table: str) -> set:
    return {ix["name"] for ix in inspector.get_indexes(table)}


def _replace_check(table: str, name: str, expression: str, *, not_valid: bool = False) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_action_check")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression})" + (" NOT VALID" if not_valid else ""))


def _uuid(name: str, *, nullable: bool = False, fk: str | None = None, ondelete: str | None = None, pk: bool = False):
    args = [sa.ForeignKey(fk, ondelete=ondelete)] if fk else []
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, nullable=nullable, primary_key=pk)


def _money_record_columns(with_method: bool):
    cols = [
        _uuid("id", pk=True),
        _uuid("org_id", fk="organizations.id", ondelete="CASCADE"),
        _uuid("membership_id", fk="student_groups.id", ondelete="RESTRICT"),
        _uuid("student_id", fk="students.id", ondelete="RESTRICT"),
        _uuid("group_id", fk="groups.id", ondelete="RESTRICT"),
        sa.Column("amount", sa.BigInteger(), nullable=False),
    ]
    if with_method:
        cols.append(sa.Column("method", sa.String(16), nullable=False))
        cols.append(sa.Column("reason", sa.String(500), nullable=True))
        cols.append(sa.Column("note", sa.String(500), nullable=True))
    else:
        cols.append(sa.Column("reason", sa.String(500), nullable=False))
    cols += [
        sa.Column("effective_on", sa.Date(), nullable=False),
        _uuid("created_by", fk="users.id"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("timezone('utc', now())")),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        _uuid("voided_by", nullable=True, fk="users.id"),
        sa.Column("void_reason", sa.String(500), nullable=True),
    ]
    return cols


VOID_ALL_OR_NONE = (
    "(voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL) OR "
    "(voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL)"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns(PAYMENTS)}

    # ---- 1. student_payments
    if "method" not in columns:
        op.add_column(PAYMENTS, sa.Column("method", sa.String(16), nullable=False, server_default="unknown"))
    if "kind" not in columns:
        op.add_column(PAYMENTS, sa.Column("kind", sa.String(16), nullable=False, server_default="payment"))
    if "idempotency_key" not in columns:
        op.add_column(PAYMENTS, sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=True))
    inspector = sa.inspect(bind)
    checks = _checks(inspector, PAYMENTS)
    if "ck_student_payments_method" not in checks:
        op.create_check_constraint("ck_student_payments_method", PAYMENTS, f"method IN ({METHODS})")
    if "ck_student_payments_kind" not in checks:
        op.create_check_constraint("ck_student_payments_kind", PAYMENTS, "kind IN ('payment', 'legacy_carry')")
    # "exactly one of valid_to / lessons_count" is no longer true (a new payment has neither): the weaker form stays
    op.execute(f"ALTER TABLE {PAYMENTS} DROP CONSTRAINT IF EXISTS ck_student_payments_period_kind")
    op.create_check_constraint("ck_student_payments_period_kind", PAYMENTS, "valid_to IS NULL OR lessons_count IS NULL")
    op.alter_column(PAYMENTS, "valid_from", existing_type=sa.Date(), nullable=True)
    op.execute(f"UPDATE {PAYMENTS} SET kind = 'legacy_carry' WHERE carried_from_payment_id IS NOT NULL AND kind <> 'legacy_carry'")
    indexes = _indexes(sa.inspect(bind), PAYMENTS)
    op.execute(f"DROP INDEX IF EXISTS {DUP_INDEX}")
    op.execute("DROP INDEX IF EXISTS idx_student_payments_org_valid_to")
    if "ix_sp_org_paid_on" not in indexes:
        op.create_index("ix_sp_org_paid_on", PAYMENTS, ["org_id", "paid_on"], postgresql_where=sa.text("voided_at IS NULL"))
    if "ix_sp_membership_live" not in indexes:
        op.create_index(
            "ix_sp_membership_live", PAYMENTS, ["org_id", "membership_id"], postgresql_where=sa.text("voided_at IS NULL")
        )
    if "uq_student_payments_idempotency" not in indexes:
        op.create_index(
            "uq_student_payments_idempotency", PAYMENTS, ["org_id", "idempotency_key"], unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
        )

    # ---- 2. new tables
    tables = set(sa.inspect(bind).get_table_names())
    if WRITEOFFS not in tables:
        op.create_table(
            WRITEOFFS, *_money_record_columns(with_method=False),
            sa.CheckConstraint("amount > 0", name="ck_student_writeoffs_amount"),
            sa.CheckConstraint("char_length(btrim(reason)) >= 3", name="ck_student_writeoffs_reason"),
            sa.CheckConstraint(VOID_ALL_OR_NONE, name="ck_student_writeoffs_void_all_or_none"),
        )
        op.create_index("ix_sw_membership_live", WRITEOFFS, ["org_id", "membership_id"], postgresql_where=sa.text("voided_at IS NULL"))
        op.create_index("ix_sw_org_effective_on", WRITEOFFS, ["org_id", "effective_on"], postgresql_where=sa.text("voided_at IS NULL"))
    if REFUNDS not in tables:
        op.create_table(
            REFUNDS, *_money_record_columns(with_method=True),
            sa.CheckConstraint("amount > 0", name="ck_student_refunds_amount"),
            sa.CheckConstraint("method IN ('cash', 'card', 'bank_transfer', 'click', 'payme', 'other')", name="ck_student_refunds_method"),
            sa.CheckConstraint(VOID_ALL_OR_NONE, name="ck_student_refunds_void_all_or_none"),
        )
        op.create_index("ix_sr_membership_live", REFUNDS, ["org_id", "membership_id"], postgresql_where=sa.text("voided_at IS NULL"))
        op.create_index("ix_sr_org_effective_on", REFUNDS, ["org_id", "effective_on"], postgresql_where=sa.text("voided_at IS NULL"))
    if TRANSFERS not in tables:
        op.create_table(
            TRANSFERS,
            _uuid("id", pk=True),
            _uuid("org_id", fk="organizations.id", ondelete="CASCADE"),
            _uuid("student_id", fk="students.id", ondelete="RESTRICT"),
            _uuid("from_membership_id", fk="student_groups.id", ondelete="RESTRICT"),
            _uuid("to_membership_id", fk="student_groups.id", ondelete="RESTRICT"),
            sa.Column("at", sa.DateTime(), nullable=False),
            _uuid("created_by", nullable=True, fk="users.id"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("timezone('utc', now())")),
            sa.Column("voided_at", sa.DateTime(), nullable=True),
            _uuid("voided_by", nullable=True, fk="users.id"),
            sa.Column("void_reason", sa.String(500), nullable=True),
            sa.UniqueConstraint("from_membership_id", "to_membership_id", "at", name="uq_membership_transfers_link"),
            sa.CheckConstraint("from_membership_id <> to_membership_id", name="ck_membership_transfers_distinct"),
        )
        op.create_index("ix_mt_from", TRANSFERS, ["from_membership_id"], postgresql_where=sa.text("voided_at IS NULL"))
        op.create_index("ix_mt_to", TRANSFERS, ["to_membership_id"], postgresql_where=sa.text("voided_at IS NULL"))
        op.create_index("ix_mt_org_student", TRANSFERS, ["org_id", "student_id"])
    if SNAPSHOTS not in tables:
        op.create_table(
            SNAPSHOTS,
            _uuid("payment_id", fk=f"{PAYMENTS}.id", ondelete="CASCADE", pk=True),
            _uuid("org_id"),
            sa.Column("snapshot", postgresql.JSONB(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("timezone('utc', now())")),
        )

    # ---- 3. audit trail
    _replace_check(GROUP_AUDIT, "ck_group_audit_action", AUDIT_NEW)

    # ---- 4. data: transfers from the live carry-over copies
    pairs = bind.execute(
        sa.text(
            f"""
            SELECT DISTINCT orig.org_id, orig.student_id, orig.membership_id, carry.membership_id, orig.carried_out_at
              FROM {PAYMENTS} carry
              JOIN {PAYMENTS} orig ON orig.id = carry.carried_from_payment_id
             WHERE carry.voided_at IS NULL AND orig.carried_out_at IS NOT NULL AND orig.membership_id <> carry.membership_id
            """
        )
    ).fetchall()
    for org_id, student_id, from_id, to_id, at in pairs:
        bind.execute(
            sa.text(
                f"INSERT INTO {TRANSFERS} (id, org_id, student_id, from_membership_id, to_membership_id, at, created_by, created_at) "
                "VALUES (:id, :org, :student, :src, :dst, :at, NULL, timezone('utc', now())) "
                "ON CONFLICT ON CONSTRAINT uq_membership_transfers_link DO NOTHING"
            ),
            {"id": uuid4(), "org": org_id, "student": student_id, "src": from_id, "dst": to_id, "at": at},
        )

    # ---- 5. data: the accounting start of every organisation
    migration_day = bind.execute(sa.text("SELECT (now() AT TIME ZONE 'Asia/Tashkent')::date")).scalar_one()
    rows = bind.execute(
        sa.text(
            "SELECT o.id, o.settings, "
            "  (SELECT min(LEAST(p.paid_on, COALESCE(p.valid_from, p.paid_on))) FROM student_payments p "
            "    WHERE p.org_id = o.id AND p.voided_at IS NULL AND p.kind = 'payment') AS first_day "
            "FROM organizations o"
        )
    ).fetchall()
    for org_id, settings, first_day in rows:
        settings = dict(settings or {})
        block = dict(settings.get("payments") or {})
        if block.get("accounting_start_date"):
            continue
        block["accounting_start_date"] = (first_day or migration_day).isoformat()
        settings["payments"] = block
        bind.execute(
            sa.text("UPDATE organizations SET settings = CAST(:settings AS jsonb) WHERE id = :id"),
            {"settings": json.dumps(settings), "id": org_id},
        )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns(PAYMENTS)}

    # a period for every payment that has none
    op.execute(
        f"""
        UPDATE {PAYMENTS} p
           SET valid_from = p.paid_on
         WHERE p.valid_from IS NULL
        """
    )
    op.execute(
        f"""
        UPDATE {PAYMENTS} p
           SET lessons_count = COALESCE(NULLIF((o.settings->'payments'->>'lessons_per_payment'), '')::int, 12)
          FROM organizations o
         WHERE o.id = p.org_id AND p.valid_to IS NULL AND p.lessons_count IS NULL
           AND (o.settings->'payments'->>'period_mode') = 'lessons'
        """
    )
    op.execute(
        f"""
        UPDATE {PAYMENTS} p
           SET valid_to = CASE
                 WHEN EXTRACT(DAY FROM p.paid_on) = 1
                   THEN (date_trunc('month', p.paid_on) + interval '1 month' - interval '1 day')::date
                 ELSE LEAST(
                        (date_trunc('month', p.paid_on) + interval '1 month')::date + (EXTRACT(DAY FROM p.paid_on)::int - 2),
                        (date_trunc('month', p.paid_on) + interval '2 month' - interval '1 day')::date)
               END
         WHERE p.valid_to IS NULL AND p.lessons_count IS NULL
        """
    )
    # identical live payments would break the old double-click index: void the later ones
    op.execute(
        f"""
        WITH ranked AS (
            SELECT id, row_number() OVER (
                       PARTITION BY org_id, membership_id, paid_on, valid_from, COALESCE(valid_to, DATE '1900-01-01'),
                                    COALESCE(lessons_count, 0), amount,
                                    COALESCE(carried_from_payment_id, '00000000-0000-0000-0000-000000000000'::uuid)
                       ORDER BY created_at, id) AS rn
              FROM {PAYMENTS} WHERE voided_at IS NULL
        )
        UPDATE {PAYMENTS} p
           SET voided_at = timezone('utc', now()), voided_by = p.created_by, void_reason = '{VOID_REASON}'
          FROM ranked r WHERE r.id = p.id AND r.rn > 1
        """
    )
    op.execute(f"DROP INDEX IF EXISTS uq_student_payments_idempotency")
    op.execute(f"DROP INDEX IF EXISTS ix_sp_membership_live")
    op.execute(f"DROP INDEX IF EXISTS ix_sp_org_paid_on")
    op.alter_column(PAYMENTS, "valid_from", existing_type=sa.Date(), nullable=False)
    op.execute(f"ALTER TABLE {PAYMENTS} DROP CONSTRAINT IF EXISTS ck_student_payments_period_kind")
    op.create_check_constraint("ck_student_payments_period_kind", PAYMENTS, "(valid_to IS NULL) <> (lessons_count IS NULL)")
    op.create_index(
        "idx_student_payments_org_valid_to", PAYMENTS, ["org_id", "valid_to"], postgresql_where=sa.text("voided_at IS NULL")
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX {DUP_INDEX} ON {PAYMENTS} (org_id, membership_id, paid_on, valid_from,
            COALESCE(valid_to, DATE '1900-01-01'), COALESCE(lessons_count, 0), amount,
            COALESCE(carried_from_payment_id, '00000000-0000-0000-0000-000000000000'::uuid))
         WHERE voided_at IS NULL
        """
    )
    op.execute(f"ALTER TABLE {PAYMENTS} DROP CONSTRAINT IF EXISTS ck_student_payments_method")
    op.execute(f"ALTER TABLE {PAYMENTS} DROP CONSTRAINT IF EXISTS ck_student_payments_kind")
    for column in ("idempotency_key", "kind", "method"):
        if column in columns:
            op.drop_column(PAYMENTS, column)
    for table in (SNAPSHOTS, TRANSFERS, REFUNDS, WRITEOFFS):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    actions = ", ".join(f"'{a}'" for a in PAY2_ACTIONS)
    op.execute(f"DELETE FROM {GROUP_AUDIT} WHERE action IN ({actions})")
    _replace_check(GROUP_AUDIT, "ck_group_audit_action", AUDIT_OLD, not_valid=True)
