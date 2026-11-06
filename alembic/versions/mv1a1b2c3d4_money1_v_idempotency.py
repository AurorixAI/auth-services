"""Money-1 (pack V1): a repeated request of a forgiveness / a refund writes one record.

Revision ID: mv1a1b2c3d4
Revises: mn1b2b3c4d5e
Create Date: 2026-11-07

* ``student_writeoffs.idempotency_key`` and ``student_refunds.idempotency_key`` (nullable) + a partial unique index per table ``(org_id, idempotency_key) WHERE idempotency_key IS NOT NULL``:
  the header ``Idempotency-Key`` of a double click.  Old rows keep NULL.

Only ADDS (no data is moved).  Rollback (downgrade): drops the indexes and the columns.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "mv1a1b2c3d4"
down_revision: str = "mn1b2b3c4d5e"
branch_labels = None
depends_on = None

TABLES = (("student_writeoffs", "uq_student_writeoffs_idempotency"), ("student_refunds", "uq_student_refunds_idempotency"))


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    for table, index in TABLES:
        if "idempotency_key" not in {c["name"] for c in inspector.get_columns(table)}:
            op.add_column(table, sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=True))
        if index not in {i["name"] for i in inspector.get_indexes(table)}:
            op.create_index(index, table, ["org_id", "idempotency_key"], unique=True, postgresql_where=sa.text("idempotency_key IS NOT NULL"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    for table, index in TABLES:
        if index in {i["name"] for i in inspector.get_indexes(table)}:
            op.drop_index(index, table_name=table)
        if "idempotency_key" in {c["name"] for c in inspector.get_columns(table)}:
            op.drop_column(table, "idempotency_key")
