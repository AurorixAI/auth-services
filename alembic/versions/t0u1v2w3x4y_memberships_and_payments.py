"""Several groups per student: membership fee; student payments (N13 P2).

Revision ID: t0u1v2w3x4y
Revises: s9t0u1v2w3x
Create Date: 2026-10-15

* ``student_groups.monthly_fee`` (+ who/when) - "оплата в месяц" of a
  membership; NULL = not set, 0 = free.
* ``student_groups.transferred_at`` - when a transfer made an already active
  membership the primary one (the journal's "transferred" label).
* ``student_payments`` - one row per payment for one membership.  Rows are
  never deleted: ``voided_*`` cancels, ``replaces_payment_id`` links a
  correction.  ``valid_to`` (calendar period) XOR ``lessons_count`` (period in
  lessons) is filled - the period mode that was active when it was entered.

The foreign key to ``student_groups`` is ``ON DELETE RESTRICT``: a membership
(and therefore its group or student, which cascade to it) that carries payments
cannot be deleted physically.  The application never deletes groups or
students, so this is a safety net, not a code path.

Everything is idempotent: ``001_baseline`` builds ``student_groups`` from the
current ORM model, so the new columns/check may already exist.

Rollback runbook (an older auth image cannot start on a database at this head -
unknown revision):
  1. export ``student_payments`` and ``student_groups.monthly_fee*`` (the
     downgrade DROPS the table and the three columns - payments are lost);
  2. ``alembic downgrade s9t0u1v2w3x``;
  3. only then roll the image back.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "t0u1v2w3x4y"
down_revision: str = "s9t0u1v2w3x"
branch_labels = None
depends_on = None

MEMBERSHIPS = "student_groups"
PAYMENTS = "student_payments"
FEE_CHECK = "ck_student_groups_monthly_fee"


def _index_names(inspector, table: str) -> set[str]:
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Membership fee.
    existing = {c["name"] for c in inspector.get_columns(MEMBERSHIPS)}
    if "monthly_fee" not in existing:
        op.add_column(MEMBERSHIPS, sa.Column("monthly_fee", sa.BigInteger(), nullable=True))
    if "monthly_fee_updated_by" not in existing:
        op.add_column(
            MEMBERSHIPS,
            sa.Column(
                "monthly_fee_updated_by",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id"),
                nullable=True,
            ),
        )
    if "monthly_fee_updated_at" not in existing:
        op.add_column(MEMBERSHIPS, sa.Column("monthly_fee_updated_at", sa.DateTime(), nullable=True))
    if "transferred_at" not in existing:
        op.add_column(MEMBERSHIPS, sa.Column("transferred_at", sa.DateTime(), nullable=True))
    checks = {c["name"] for c in inspector.get_check_constraints(MEMBERSHIPS)}
    if FEE_CHECK not in checks:
        op.create_check_constraint(FEE_CHECK, MEMBERSHIPS, "monthly_fee >= 0")

    # 2. Payments.
    if PAYMENTS not in set(inspector.get_table_names()):
        op.create_table(
            PAYMENTS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "membership_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("student_groups.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "student_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("students.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "group_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("groups.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("amount", sa.BigInteger(), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False, server_default="UZS"),
            sa.Column("paid_on", sa.Date(), nullable=False),
            sa.Column("valid_from", sa.Date(), nullable=False),
            sa.Column("valid_to", sa.Date(), nullable=True),
            sa.Column("lessons_count", sa.SmallInteger(), nullable=True),
            sa.Column("note", sa.String(length=500), nullable=True),
            sa.Column(
                "replaces_payment_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("student_payments.id"),
                nullable=True,
            ),
            sa.Column(
                "created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("voided_at", sa.DateTime(), nullable=True),
            sa.Column(
                "voided_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
            ),
            sa.Column("void_reason", sa.String(length=500), nullable=True),
            sa.CheckConstraint("amount >= 0", name="ck_student_payments_amount"),
            sa.CheckConstraint("lessons_count > 0", name="ck_student_payments_lessons_positive"),
            sa.CheckConstraint(
                "(valid_to IS NULL) <> (lessons_count IS NULL)", name="ck_student_payments_period_kind"
            ),
            sa.CheckConstraint(
                "valid_to IS NULL OR valid_to >= valid_from", name="ck_student_payments_period_order"
            ),
            sa.CheckConstraint(
                "(voided_at IS NULL AND voided_by IS NULL AND void_reason IS NULL) OR "
                "(voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL)",
                name="ck_student_payments_void_all_or_none",
            ),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_student_payments_membership "
        "ON student_payments (org_id, membership_id, valid_to DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_student_payments_org_valid_to "
        "ON student_payments (org_id, valid_to) WHERE voided_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_student_payments_active_dup "
        "ON student_payments (org_id, membership_id, paid_on, valid_from, "
        "COALESCE(valid_to, DATE '1900-01-01'), COALESCE(lessons_count, 0), amount) "
        "WHERE voided_at IS NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if PAYMENTS in set(inspector.get_table_names()):
        op.drop_table(PAYMENTS)

    checks = {c["name"] for c in inspector.get_check_constraints(MEMBERSHIPS)}
    if FEE_CHECK in checks:
        op.drop_constraint(FEE_CHECK, MEMBERSHIPS, type_="check")
    existing = {c["name"] for c in inspector.get_columns(MEMBERSHIPS)}
    for column in ("transferred_at", "monthly_fee_updated_at", "monthly_fee_updated_by", "monthly_fee"):
        if column in existing:
            op.drop_column(MEMBERSHIPS, column)
