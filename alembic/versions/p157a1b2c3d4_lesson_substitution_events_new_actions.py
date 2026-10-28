"""#157 (28.10): the journal of the requests learns two events - ``lesson_restored`` (the cancellation of a lesson was undone, A10) and ``day_off_voided`` (a day off that had cancelled the
lesson / taken the substitute off was cancelled, A4).

Revision ID: p157a1b2c3d4
Revises: w1a2b3c4d5e
Create Date: 2026-10-28

Schema only, no data: the CHECK ``ck_lse_action`` of ``lesson_substitution_events`` also accepts the two actions (drop + add ``NOT VALID`` + ``VALIDATE``: the first takes a short lock; a short
table).  Rollback (downgrade): the rows with the two actions are deleted (they are information only - no status hangs on them) and the old CHECK comes back.
"""
from __future__ import annotations

from alembic import op

revision: str = "p157a1b2c3d4"
down_revision: str = "w1a2b3c4d5e"
branch_labels = None
depends_on = None

TABLE = "lesson_substitution_events"
CHECK = "ck_lse_action"
OLD = ("action IN ('requested', 'withdrawn', 'substituted', 'rescheduled', 'cancelled', 'rejected', 'closed', 'reopened', "
       "'assignment_cleared', 'suggestion_unavailable')")
NEW = OLD[:-1] + ", 'lesson_restored', 'day_off_voided')"


def _replace(definition: str) -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CHECK}")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} CHECK ({definition}) NOT VALID")
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CHECK}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _replace(NEW)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DELETE FROM {TABLE} WHERE action IN ('lesson_restored', 'day_off_voided')")
    _replace(OLD)
