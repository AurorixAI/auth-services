"""#159 (29.10): a make-up is an official lesson - its ``money`` is always NULL (no decision of an empty make-up).

Revision ID: p159a1b2c3d4
Revises: p154a1b2c3d4
Create Date: 2026-10-29

* the empty make-ups that #154 left with ``paid`` / ``none`` (or waiting) are brought to NULL by ONE update (LESSONS 49: the test centres only - stage and «ALGO Лид-тест»; no compatibility
  branch in the code); the CHECK ``ck_one_off_money`` is replaced (``NOT VALID`` + ``VALIDATE``, LESSONS 26): a make-up and a copy of a move have no money at all;
* the audit rows ``one_off_money_decided`` of those decisions stay (history).

Rollback (downgrade): the old CHECK comes back; the NULLs are not restored (the decisions are in the audit)."""
from __future__ import annotations

from alembic import op

revision: str = "p159a1b2c3d4"
down_revision: str = "p154a1b2c3d4"
branch_labels = None
depends_on = None

ONE_OFF = "group_schedule_one_off_lessons"
CHECK = "ck_one_off_money"
NEW = "(purpose = 'extra' AND money IS NOT NULL AND money IN ('none', 'wait', 'paid')) OR (purpose IN ('makeup', 'moved') AND money IS NULL)"
OLD = (
    "(purpose = 'extra' AND money IS NOT NULL AND money IN ('none', 'wait', 'paid')) OR (purpose = 'makeup' AND (money IS NULL OR money IN ('none', 'paid'))) "
    "OR (purpose = 'moved' AND money IS NULL)"
)


def _replace_check(definition: str) -> None:
    op.execute(f"ALTER TABLE {ONE_OFF} DROP CONSTRAINT IF EXISTS {CHECK}")
    op.execute(f"ALTER TABLE {ONE_OFF} ADD CONSTRAINT {CHECK} CHECK ({definition}) NOT VALID")
    op.execute(f"ALTER TABLE {ONE_OFF} VALIDATE CONSTRAINT {CHECK}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"UPDATE {ONE_OFF} SET money = NULL WHERE purpose = 'makeup' AND money IS NOT NULL")
    _replace_check(NEW)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _replace_check(OLD)
