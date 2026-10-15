"""The payment moves with the student on a transfer (N13 P2.1).

Revision ID: t1u2v3w4x5y
Revises: t0u1v2w3x4y
Create Date: 2026-10-15

* ``student_payments.carried_from_payment_id`` - set on the carry-over payment
  (amount 0) that a transfer creates in the new group; it points at the payment
  it was made from.  NULL = an ordinary payment.  Voiding or replacing the
  original reaches its carry-overs through this column.
* ``student_payments.carried_out_at`` - set on the ORIGINAL when its carry-over is
  made: the INSTANT of the transfer (TIMESTAMP, naive UTC like ``student_groups.left_at``,
  the very same moment).  From then on the original covers, in its own membership, only
  what happened while the student was still in the group (a lesson that started before
  the instant, a calendar day up to and including the transfer day), so the coverage is
  never counted in two groups at once (also not when the student returns).  NULL = it
  did not move.
* a partial index on ``carried_from_payment_id`` (cascades look carry-overs up by it).
* the duplicate guard ``uq_student_payments_active_dup`` now also tells carry-overs
  made from different payments apart (A -> B -> A -> B within one day makes two
  carry-overs of identical figures on B, one from each source).  Two identical rows
  from the SAME source stay refused.

Idempotent: ``001_baseline`` builds tables from the current ORM model, so the
columns may already exist.  The downgrade drops both columns and restores the old
guard; the carry-over rows stay as ordinary zero payments of the new group (a pair of
live rows that would break the old guard is cancelled first, the later one).
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
FROM_COLUMN = "carried_from_payment_id"
OUT_COLUMN = "carried_out_at"
FROM_INDEX = "idx_student_payments_carried_from"
DUP_INDEX = "uq_student_payments_active_dup"

_DUP_COLUMNS = (
    "org_id, membership_id, paid_on, valid_from, COALESCE(valid_to, DATE '1900-01-01'), "
    "COALESCE(lessons_count, 0), amount"
)


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns(PAYMENTS)}
    if FROM_COLUMN not in existing:
        op.add_column(
            PAYMENTS,
            sa.Column(
                FROM_COLUMN,
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("student_payments.id"),
                nullable=True,
            ),
        )
    if OUT_COLUMN not in existing:
        op.add_column(PAYMENTS, sa.Column(OUT_COLUMN, sa.DateTime(), nullable=True))
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {FROM_INDEX} ON {PAYMENTS} ({FROM_COLUMN}) "
        f"WHERE {FROM_COLUMN} IS NOT NULL"
    )
    # Rebuilt every time (the table is small): either the old guard or the new one may be there.
    op.execute(f"DROP INDEX IF EXISTS {DUP_INDEX}")
    op.execute(
        f"CREATE UNIQUE INDEX {DUP_INDEX} ON {PAYMENTS} ({_DUP_COLUMNS}, "
        f"COALESCE({FROM_COLUMN}, '00000000-0000-0000-0000-000000000000'::uuid)) "
        "WHERE voided_at IS NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    inspector = sa.inspect(op.get_bind())
    if PAYMENTS not in set(inspector.get_table_names()):
        return
    op.execute(f"DROP INDEX IF EXISTS {DUP_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {FROM_INDEX}")
    # Without the source column two live carry-overs of identical figures would collide.
    op.execute(
        "UPDATE student_payments SET voided_at = now(), voided_by = created_by, "
        "void_reason = 'Откат миграции: дубликат переноса' WHERE id IN ("
        "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY "
        f"{_DUP_COLUMNS} ORDER BY created_at, id) AS n "
        "FROM student_payments WHERE voided_at IS NULL) AS d WHERE d.n > 1)"
    )
    existing = {c["name"] for c in inspector.get_columns(PAYMENTS)}
    for column in (OUT_COLUMN, FROM_COLUMN):
        if column in existing:
            op.drop_column(PAYMENTS, column)
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {DUP_INDEX} ON {PAYMENTS} ({_DUP_COLUMNS}) "
        "WHERE voided_at IS NULL"
    )
