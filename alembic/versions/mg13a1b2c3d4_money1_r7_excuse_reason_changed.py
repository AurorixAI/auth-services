"""Money-1 (pack G, R-7): the journal of the marks accepts ``excuse_reason_changed`` - a change of the text of a DECIDED excuse leaves a record (the old and the new text).

Revision ID: mg13a1b2c3d4
Revises: mg12a1b2c3d4
Create Date: 2026-11-13

Only WEAKENS: the CHECK ``ck_group_attendance_audit_action`` gets one more value; no row is read, moved or changed.
Rollback (downgrade): the CHECK stays as wide as it is - a record with the new action may already be there, and a rollback never costs a record of the journal (an old reader ignores the action).
"""
from __future__ import annotations

import re

from alembic import op
import sqlalchemy as sa

revision: str = "mg13a1b2c3d4"
down_revision: str = "mg12a1b2c3d4"
branch_labels = None
depends_on = None

TABLE, NAME, NEW_ACTION = "group_attendance_audit", "ck_group_attendance_audit_action", "excuse_reason_changed"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    definition = op.get_bind().execute(
        sa.text("SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = :t AND c.conname = :n"), {"t": TABLE, "n": NAME},
    ).scalar()
    live = re.findall(r"'([a-z_]+)'", definition or "")
    if not live or NEW_ACTION in live:
        return
    listed = ", ".join(f"'{v}'" for v in [*live, NEW_ACTION])
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {NAME}")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {NAME} CHECK (action IN ({listed}))")


def downgrade() -> None:
    pass          # the wider CHECK stays: a record of the new action may exist, and a rollback deletes nothing
