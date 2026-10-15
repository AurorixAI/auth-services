"""Carry the payment over when a student is transferred (N13 P2.1).

Revision ID: t1u2v3w4x5y
Revises: t0u1v2w3x4y
Create Date: 2026-10-15

* ``student_payments.carried_from_payment_id`` - set on the carry-over payment
  (amount 0) that a transfer creates in the new group; it points at the payment
  it was made from.  NULL = an ordinary payment.  Voiding or replacing the
  original reaches its carry-overs through this column.
* a partial index on the column (cascades look the carry-overs up by it).

The partial unique index ``uq_student_payments_active_dup`` is untouched: it
includes the membership and the amount, so a carry-over (amount 0, another
membership) never collides with the original; an identical pair of carry-overs
is prevented by the application.

Idempotent: ``001_baseline`` builds tables from the current ORM model, so the
column may already exist.  The downgrade drops the column (and the link, not
the carry-over rows - they stay as ordinary zero payments of the new group).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "t1u2v3w4x5y"
down_revision: str = "t0u1v2w3x4y"
branch_labels = None
depends_on = None

PAYMENTS = "student_payments"
COLUMN = "carried_from_payment_id"
INDEX = "idx_student_payments_carried_from"


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    if COLUMN not in {c["name"] for c in inspector.get_columns(PAYMENTS)}:
        op.add_column(
            PAYMENTS,
            sa.Column(
                COLUMN,
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("student_payments.id"),
                nullable=True,
            ),
        )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX} ON {PAYMENTS} ({COLUMN}) "
        f"WHERE {COLUMN} IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    inspector = sa.inspect(op.get_bind())
    if PAYMENTS in set(inspector.get_table_names()) and COLUMN in {
        c["name"] for c in inspector.get_columns(PAYMENTS)
    }:
        op.drop_column(PAYMENTS, COLUMN)
