"""Receipts, payment cheques and the "incomplete month" flag (N13 P2.2).

Revision ID: u1v2w3x4y5z
Revises: t1u2v3w4x5y
Create Date: 2026-10-15

* ``student_payments.receipt_no`` - the receipt number, 1, 2, 3... inside the organisation, a
  partial UNIQUE index ``uq_student_payments_receipt_no (org_id, receipt_no) WHERE receipt_no IS
  NOT NULL``.  The application takes ``max + 1`` in a SAVEPOINT and retries on a conflict.  Every
  payment but a carry-over (``carried_from_payment_id`` set - not money) gets one; a replacement gets
  a new number.  Backfill: ``row_number() over (partition by org_id order by created_at, id)`` for the
  payments that have none (voided ones included - the history keeps its numbers).
* ``student_payments.partial_period`` - boolean NOT NULL default false: the payment did not cover
  whole periods of the centre ("единый день оплаты").  Old rows are false (no such model existed).
* ``student_payment_files`` - photo / scan of the cheque (JPEG/PNG/PDF, <= 2 MB, bytea), like
  ``attendance_excuse_files``; deleted together with its payment (ON DELETE CASCADE).

Idempotent: columns, the table, the index and the backfill (it numbers only rows without a number,
after the highest existing one) tolerate a partial earlier run.

Rollback: the downgrade drops the index, the table and both columns - receipt numbers and ALL
attached cheques are lost.  Export ``student_payment_files`` (and ``receipt_no``) first if they matter;
nothing else in the payment history changes.  An older auth image cannot start on a database at this
head (unknown revision): downgrade first, roll the image back afterwards.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "u1v2w3x4y5z"
down_revision: str = "t1u2v3w4x5y"
branch_labels = None
depends_on = None

PAYMENTS = "student_payments"
FILES = "student_payment_files"
RECEIPT_INDEX = "uq_student_payments_receipt_no"
FILES_INDEX = "idx_student_payment_files_payment"


def _index_names(inspector, table: str) -> set[str]:
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Columns of the payment.
    existing = {c["name"] for c in inspector.get_columns(PAYMENTS)}
    if "receipt_no" not in existing:
        op.add_column(PAYMENTS, sa.Column("receipt_no", sa.Integer(), nullable=True))
    if "partial_period" not in existing:
        op.add_column(
            PAYMENTS,
            sa.Column("partial_period", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )

    # 2. Numbers for the existing payments (a carry-over is not money: no receipt).  Before the index,
    #    after the highest number that may already exist.
    op.execute(
        """
        UPDATE student_payments AS p
        SET receipt_no = n.rn + COALESCE(m.mx, 0)
        FROM (
            SELECT id, org_id,
                   row_number() OVER (PARTITION BY org_id ORDER BY created_at, id) AS rn
            FROM student_payments
            WHERE receipt_no IS NULL AND carried_from_payment_id IS NULL
        ) AS n
        LEFT JOIN (
            SELECT org_id, max(receipt_no) AS mx
            FROM student_payments
            WHERE receipt_no IS NOT NULL
            GROUP BY org_id
        ) AS m ON m.org_id = n.org_id
        WHERE p.id = n.id
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {RECEIPT_INDEX} ON {PAYMENTS} (org_id, receipt_no) "
        "WHERE receipt_no IS NOT NULL"
    )

    # 3. Cheques.
    if FILES not in set(inspector.get_table_names()):
        op.create_table(
            FILES,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "payment_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("student_payments.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("filename", sa.String(length=200), nullable=True),
            sa.Column("content_type", sa.String(length=50), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("data", sa.LargeBinary(), nullable=False),
            sa.Column(
                "uploaded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("uploaded_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "size_bytes > 0 AND size_bytes <= 2097152",
                name="ck_student_payment_file_size",
            ),
        )
    if FILES_INDEX not in _index_names(sa.inspect(bind), FILES):
        op.create_index(FILES_INDEX, FILES, ["payment_id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if FILES in set(inspector.get_table_names()):
        op.drop_table(FILES)
    if PAYMENTS not in set(inspector.get_table_names()):
        return
    op.execute(f"DROP INDEX IF EXISTS {RECEIPT_INDEX}")
    existing = {c["name"] for c in inspector.get_columns(PAYMENTS)}
    for column in ("partial_period", "receipt_no"):
        if column in existing:
            op.drop_column(PAYMENTS, column)
