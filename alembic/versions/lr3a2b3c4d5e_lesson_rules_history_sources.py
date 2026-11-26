"""LESSON-RULES С7 (26.11): the sources of the list «История» - the group journal learns ``lesson_added`` and the journal of attendance gets the index the list reads it by.

Revision ID: lr3a2b3c4d5e
Revises: lr2a2b3c4d5e
Create Date: 2026-11-26

* ``ck_group_audit_action`` also accepts ``lesson_added`` (a weekly lesson created by a teacher; one line at the creation).  NOT VALID + VALIDATE.
* ``ix_gaa_org_done_corrected``: ``group_attendance_audit (org_id, created_at)`` for the lines the list reads (``updated`` / ``corrected``), a partial index.
Rollback (downgrade): the index is dropped, the ``lesson_added`` lines are deleted, the old CHECK returns.
"""
from __future__ import annotations

from alembic import op

revision: str = "lr3a2b3c4d5e"
down_revision: str = "lr2a2b3c4d5e"
branch_labels = None
depends_on = None

TABLE, CHECK, INDEX = "group_audit", "ck_group_audit_action", "ix_gaa_org_done_corrected"
OLD = ("action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored', 'group_price_set', 'membership_price_set', 'group_price_cancelled', "
       "'membership_price_cancelled', 'payment_writeoff', 'payment_writeoff_void', 'payment_refund', 'payment_refund_void', 'membership_pause_set', 'membership_pause_void', "
       "'opening_balance_set', 'opening_balance_void', 'group_lesson_type_set', 'student_left_group', 'group_archived', 'group_restored', 'lesson_teacher_set', "
       "'lesson_teacher_cleared', 'day_off_voided', 'lesson_restored', 'lesson_cancelled', 'one_off_created', 'one_off_money_decided', 'exam_release_pending', "
       "'exam_release_done', 'payment_void', 'payment_replace', 'payment_file_removed', 'student_transferred')")
NEW = OLD[:-1] + ", 'lesson_added')"


def _replace(definition: str) -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {CHECK}")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK} CHECK ({definition}) NOT VALID")
    op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {CHECK}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _replace(NEW)
    op.execute(f"CREATE INDEX IF NOT EXISTS {INDEX} ON group_attendance_audit (org_id, created_at) WHERE action IN ('updated', 'corrected')")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"DELETE FROM {TABLE} WHERE action = 'lesson_added'")
    _replace(OLD)
