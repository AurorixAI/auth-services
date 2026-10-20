"""Excuse decision mark: ``decided_after_deadline`` + ``deadline_at_decision`` (N13 P4, ROADMAP #39).

Revision ID: c9d0e1f2a3b
Revises: b8c9d0e1f2a
Create Date: 2026-10-20

Owner rule (#39, reconfirmed 20.10): the administrator may decide an attendance excuse AFTER its confirmation deadline,
but such a decision must be MARKED.  Until this revision ``overdue`` was only a hint on the pending items - once the
excuse was decided nothing said that it had been late.

``attendance_excuses`` gets two nullable columns, set by the code in the very transaction of the decision (confirm, reject,
a changed decision, the bulk review) and cleared by every way back to ``pending`` (reset; a reason edited after a decision):

* ``decided_after_deadline`` BOOLEAN - the decision was taken after the deadline the list showed at that moment;
* ``deadline_at_decision`` DATE - that deadline as a business day (Tashkent, like every date of the attendance tables).
  A lessons-package deadline has no day, so it is NULL there while the flag is still set.

NULL means "unknown", NOT "on time": the decisions made before this revision have no mark and the backfill leaves them
NULL - their deadline of that day can no longer be told (payments, packages and settings have changed since).  A
pending excuse has both NULL as well.

Both ADD COLUMNs are nullable without a default (a catalogue change only, no table rewrite); no data is touched.

Idempotent: a column that is already there (a database built by ``create_all`` and stamped back) is left as it is - and
so are its values.  A second ``upgrade`` changes nothing.

Rollback (downgrade): drops the two columns - the marks are lost, the decisions themselves are not touched, the
previous code runs unchanged.  An older auth image cannot start on a database at this head (unknown revision):
downgrade first, roll the image back afterwards.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "c9d0e1f2a3b"
down_revision: str = "b8c9d0e1f2a"
branch_labels = None
depends_on = None

TABLE = "attendance_excuses"


def _columns(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    columns = _columns(op.get_bind())
    if "decided_after_deadline" not in columns:
        op.add_column(TABLE, sa.Column("decided_after_deadline", sa.Boolean(), nullable=True))
    if "deadline_at_decision" not in columns:
        op.add_column(TABLE, sa.Column("deadline_at_decision", sa.Date(), nullable=True))
    # No backfill on purpose: the already decided rows stay NULL = "unknown", never "on time".


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    columns = _columns(op.get_bind())
    if "deadline_at_decision" in columns:
        op.drop_column(TABLE, "deadline_at_decision")
    if "decided_after_deadline" in columns:
        op.drop_column(TABLE, "decided_after_deadline")
