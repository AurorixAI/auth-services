"""LESSON-RULES С1 (26.11): the third kind of a cancellation, ``unconfirmed`` («ждёт подтверждения»).

Revision ID: lr1a2b3c4d5e
Revises: mm22a1b2c3d4
Create Date: 2026-11-26

Only WIDENS one CHECK (``ck_group_attendance_cancellation_kind``: ``centre`` | ``holiday`` | ``unconfirmed`` | NULL); no data, no column.  The new CHECK is added ``NOT VALID`` and
validated in a separate statement (a short lock on a big table).  Rollback (downgrade): the old CHECK returns only when no row carries ``unconfirmed`` - the owner decides those rows first
(``unconfirmed`` is neutral for the money, so a downgrade that turns them into NULL changes no account: they become cancellations without a kind, never recalculated).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision: str = "lr1a2b3c4d5e"
down_revision: str = "mm22a1b2c3d4"
branch_labels = None
depends_on = None

TABLE, NAME = "group_attendance", "ck_group_attendance_cancellation_kind"
OLD = "cancellation_kind IS NULL OR cancellation_kind IN ('centre', 'holiday')"
NEW = "cancellation_kind IS NULL OR cancellation_kind IN ('centre', 'holiday', 'unconfirmed')"


def _exists(bind) -> bool:
    return bool(bind.execute(sa.text("SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = :t AND c.conname = :n"), {"t": TABLE, "n": NAME}).scalar())


def _replace(bind, expression: str) -> None:
    if _exists(bind):
        op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {NAME}")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {NAME} CHECK ({expression}) NOT VALID")
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {NAME}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")          # as lr2 and lr3: a lock that is not given in 5 seconds fails the migration instead of queueing the whole table behind it
    _replace(op.get_bind(), NEW)


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"UPDATE {TABLE} SET cancellation_kind = NULL WHERE cancellation_kind = 'unconfirmed'")
    _replace(bind, OLD)
