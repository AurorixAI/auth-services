"""W1 (27.10): one live day off per range, the «not finished» mark of a day off, the audit row of a restored lesson.

Revision ID: w1a2b3c4d5e
Revises: pf1a2b3c4d5
Create Date: 2026-10-27

``group_attendance.converted_from_kind`` (VARCHAR(20) NULL): the kind a hand-made cancellation HAD before a day off turned it into a holiday - «Отменить выходной» gives it back.
``org_days_off.incomplete`` (BOOLEAN NOT NULL DEFAULT false): the exam-service stopped the apply, some lessons wait for «Продолжить» (it lived only in the open tab).
``uq_org_days_off_live_range`` (partial UNIQUE ``(org_id, date_from, date_to) WHERE voided_at IS NULL``): two windows / a retry cannot give the same day off twice.
Live DUPLICATES that already exist are merged first: the oldest stays, the others are voided (``voided_at`` now, nobody as the author) and the lessons they cancelled are
pointed to the oldest, so that «Отменить выходной» still gives them back (nothing is deleted).
``group_audit.action`` also accepts ``lesson_restored`` (one row when an owner / admin gives a cancelled lesson back: what it was is recorded before its row is deleted) and ``lesson_cancelled``
(who cancelled a lesson of a group that has no teacher - there is nobody to attribute a record of «Замены» to).
The CHECK is replaced (drop + add ``NOT VALID`` + ``VALIDATE``); the index is built ``IF NOT EXISTS``; the column default is a constant (no table rewrite).

Rollback (downgrade): drops the index and the column and puts the old CHECK back (``lesson_restored`` rows are deleted first).  The merged duplicates stay voided.
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa

revision: str = "w1a2b3c4d5e"
down_revision: str = "pf1a2b3c4d5"
branch_labels = None
depends_on = None

AUDIT_TABLE = "group_audit"
AUDIT_CHECK = "ck_group_audit_action"
AUDIT_OLD = (
    "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored', "
    "'group_price_set', 'membership_price_set', 'group_price_cancelled', 'membership_price_cancelled', "
    "'payment_writeoff', 'payment_writeoff_void', 'payment_refund', 'payment_refund_void', "
    "'membership_pause_set', 'membership_pause_void', 'opening_balance_set', 'opening_balance_void', "
    "'group_lesson_type_set', 'student_left_group', "
    "'group_archived', 'group_restored', 'lesson_teacher_set', 'lesson_teacher_cleared', 'day_off_voided')"
)
AUDIT_NEW = AUDIT_OLD[:-1] + ", 'lesson_restored', 'lesson_cancelled')"
INDEX = "uq_org_days_off_live_range"
log = logging.getLogger("alembic.runtime.migration")


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _add_validated(definition: str) -> None:
    """``ADD CONSTRAINT ... NOT VALID`` then ``VALIDATE CONSTRAINT`` (the first takes only a short lock)."""
    op.execute(f"ALTER TABLE {AUDIT_TABLE} ADD CONSTRAINT {AUDIT_CHECK} CHECK ({definition}) NOT VALID")
    op.execute(f"ALTER TABLE {AUDIT_TABLE} VALIDATE CONSTRAINT {AUDIT_CHECK}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    if "incomplete" not in _columns(bind, "org_days_off"):
        op.add_column("org_days_off", sa.Column("incomplete", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    if "converted_from_kind" not in _columns(bind, "group_attendance"):          # nullable, no default: no table rewrite
        op.add_column("group_attendance", sa.Column("converted_from_kind", sa.String(20), nullable=True))
    # merge the live duplicates of one range (the oldest stays)
    repointed = bind.execute(sa.text(
        """
        WITH ranked AS (
            SELECT id, first_value(id) OVER w AS keeper, row_number() OVER w AS n
            FROM org_days_off WHERE voided_at IS NULL
            WINDOW w AS (PARTITION BY org_id, date_from, date_to ORDER BY created_at, id)
        )
        UPDATE group_attendance a SET day_off_id = r.keeper
        FROM ranked r WHERE a.day_off_id = r.id AND r.n > 1
        """
    )).rowcount
    merged = bind.execute(sa.text(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY org_id, date_from, date_to ORDER BY created_at, id) AS n
            FROM org_days_off WHERE voided_at IS NULL
        )
        UPDATE org_days_off d SET voided_at = timezone('utc', now()), voided_by = NULL
        FROM ranked r WHERE d.id = r.id AND r.n > 1
        """
    )).rowcount
    log.info("w1a2b3c4d5e: merged %d live duplicate day off(s) of the same range (the oldest of each range kept, the others voided); %d cancelled lesson(s) re-pointed to the kept one", merged, repointed)
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX} ON org_days_off (org_id, date_from, date_to) WHERE voided_at IS NULL")
    op.execute(f"ALTER TABLE {AUDIT_TABLE} DROP CONSTRAINT IF EXISTS {AUDIT_CHECK}")
    _add_validated(AUDIT_NEW)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    op.execute(f"DELETE FROM {AUDIT_TABLE} WHERE action IN ('lesson_restored', 'lesson_cancelled')")
    op.execute(f"ALTER TABLE {AUDIT_TABLE} DROP CONSTRAINT IF EXISTS {AUDIT_CHECK}")
    _add_validated(AUDIT_OLD)
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    if "incomplete" in _columns(bind, "org_days_off"):
        op.drop_column("org_days_off", "incomplete")
    if "converted_from_kind" in _columns(bind, "group_attendance"):
        op.drop_column("group_attendance", "converted_from_kind")
