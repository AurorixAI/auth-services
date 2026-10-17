"""A diagnostic invite can be cancelled: archiving a student switches his unfinished links off (N13, ROADMAP #89).

Revision ID: y5z6a7b8c9d
Revises: x4y5z6a7b8c
Create Date: 2026-10-17

* ``diagnostic_invites.status`` also accepts ``cancelled`` (the CHECK is replaced by ``ck_diagnostic_invites_status``).
  A cancelled invite is unusable on every public path (lookup by code, confirm, start, complete) and is never
  flipped to ``expired`` by the expiry helpers.
* ``diagnostic_invites.cancelled_at`` (TIMESTAMP, naive UTC) and ``cancel_reason`` (VARCHAR(50), a machine code:
  ``student_archived``) - both NULL for every invite that was not cancelled.  Nullable and additive.

No data is changed: nothing is cancelled retroactively (the archived students that exist today keep whatever
invites they have; the next archiving cancels its own).

Idempotent: the columns and the CHECK tolerate a partial earlier run (a database built from the models, then
stamped).  The old CHECK of a database made by the SQL scripts may carry the PostgreSQL default name
(``diagnostic_invites_status_check``) or another one: every CHECK on ``diagnostic_invites`` that constrains
``status`` is dropped by what it says, not by its name.

Rollback (downgrade): ``cancelled`` is mapped to ``expired`` (the closest old state: the link is dead), the old
CHECK comes back and the two columns are dropped - the cancellation date and reason are lost.  An older auth
image cannot start on a database at this head (unknown revision): downgrade first, roll the image back afterwards.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "y5z6a7b8c9d"
down_revision: str = "x4y5z6a7b8c"
branch_labels = None
depends_on = None

TABLE = "diagnostic_invites"
CHECK_NAME = "ck_diagnostic_invites_status"
STATUS_OLD = "status IN ('pending', 'confirmed', 'in_progress', 'completed', 'expired')"
STATUS_NEW = "status IN ('pending', 'confirmed', 'in_progress', 'completed', 'expired', 'cancelled')"


def _drop_status_checks() -> None:
    """Drop every CHECK of the table that constrains ``status`` (whatever it is called)."""
    inspector = sa.inspect(op.get_bind())
    for check in inspector.get_check_constraints(TABLE):
        text = (check.get("sqltext") or "").lower()
        if "status" in text and "pending" in text:
            op.execute(f'ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS "{check["name"]}"')


def upgrade() -> None:
    # Fail fast instead of queueing behind a long transaction on a hot table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}

    if "cancelled_at" not in columns:
        op.add_column(TABLE, sa.Column("cancelled_at", sa.DateTime(), nullable=True))
    if "cancel_reason" not in columns:
        op.add_column(TABLE, sa.Column("cancel_reason", sa.String(length=50), nullable=True))

    _drop_status_checks()
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK_NAME} CHECK ({STATUS_NEW})")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}

    # Before the old CHECK comes back: no row may still carry the new state.
    op.execute(f"UPDATE {TABLE} SET status = 'expired' WHERE status = 'cancelled'")
    _drop_status_checks()
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK_NAME} CHECK ({STATUS_OLD})")

    if "cancel_reason" in columns:
        op.drop_column(TABLE, "cancel_reason")
    if "cancelled_at" in columns:
        op.drop_column(TABLE, "cancelled_at")
