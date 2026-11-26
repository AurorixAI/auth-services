"""LESSON-RULES С2 (26.11): the journal of the requests learns the event ``confirmed`` (the owner confirmed a cancellation that was waiting: ``unconfirmed`` -> ``centre`` | ``holiday``).

Revision ID: lr2a2b3c4d5e
Revises: lr1a2b3c4d5e
Create Date: 2026-11-26

Schema only, no data: the CHECK ``ck_lse_action`` of ``lesson_substitution_events`` also accepts the action (drop + add ``NOT VALID`` + ``VALIDATE``).  Rollback (downgrade): the rows with the
action are deleted (the cancellation itself and its audit stay), then the old CHECK returns.
"""
from __future__ import annotations

from alembic import op

revision: str = "lr2a2b3c4d5e"
down_revision: str = "lr1a2b3c4d5e"
branch_labels = None
depends_on = None

TABLE = "lesson_substitution_events"
CHECK = "ck_lse_action"
OLD = ("action IN ('requested', 'withdrawn', 'substituted', 'rescheduled', 'cancelled', 'rejected', 'closed', 'reopened', "
       "'assignment_cleared', 'suggestion_unavailable', 'lesson_restored', 'day_off_voided')")
NEW = OLD[:-1] + ", 'confirmed')"


def _replace(definition: str) -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CHECK}")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} CHECK ({definition}) NOT VALID")
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CHECK}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _replace(NEW)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DELETE FROM {TABLE} WHERE action = 'confirmed'")
    _replace(OLD)
