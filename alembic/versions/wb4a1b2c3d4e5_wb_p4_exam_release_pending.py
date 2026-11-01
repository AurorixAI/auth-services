"""Wave B P4 (B8 / SV-15, LD-23): ``group_attendance.exam_release_pending``.

Revision ID: wb4a1b2c3d4e5
Revises: k4a1b2c3d4e5
Create Date: 2026-11-02

The release of the exams of a CANCELLED lesson is asked of exam-service after the commit; when it does not answer, the cancelled row keeps this marker and the next write
to the group (or ``scripts/sweep_exam_releases.py``) retries it.  ``BOOLEAN NOT NULL DEFAULT false`` is a catalogue-only change on PostgreSQL 11+ (no table rewrite);
the partial index holds only the rows that wait (normally none).

``group_audit.action`` (``ck_group_audit_action``) also accepts ``exam_release_pending`` / ``exam_release_done``: the owner's trail «экзамен по уроку не освобождён - повторим» / «освобождён» (R3 5).

Only ADDS.  Idempotent (``IF NOT EXISTS``); the downgrade drops the index and the column and the two trail actions (rows of them go first).
"""
from __future__ import annotations

from alembic import op

revision: str = "wb4a1b2c3d4e5"
down_revision: str = "k4a1b2c3d4e5"
branch_labels = None
depends_on = None


AUDIT_OLD = (
    "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored', "
    "'group_price_set', 'membership_price_set', 'group_price_cancelled', 'membership_price_cancelled', "
    "'payment_writeoff', 'payment_writeoff_void', 'payment_refund', 'payment_refund_void', "
    "'membership_pause_set', 'membership_pause_void', 'opening_balance_set', 'opening_balance_void', "
    "'group_lesson_type_set', 'student_left_group', "
    "'group_archived', 'group_restored', 'lesson_teacher_set', 'lesson_teacher_cleared', 'day_off_voided', 'lesson_restored', 'lesson_cancelled', "
    "'one_off_created', 'one_off_money_decided')"
)
AUDIT_NEW = AUDIT_OLD[:-1] + ", 'exam_release_pending', 'exam_release_done')"


def _audit_check(definition: str) -> None:
    op.execute("ALTER TABLE group_audit DROP CONSTRAINT IF EXISTS ck_group_audit_action")
    op.execute(f"ALTER TABLE group_audit ADD CONSTRAINT ck_group_audit_action CHECK ({definition}) NOT VALID")
    op.execute("ALTER TABLE group_audit VALIDATE CONSTRAINT ck_group_audit_action")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE group_attendance ADD COLUMN IF NOT EXISTS exam_release_pending BOOLEAN NOT NULL DEFAULT false")
    op.execute("CREATE INDEX IF NOT EXISTS ix_attendance_exam_release_pending ON group_attendance (org_id, group_id) WHERE exam_release_pending")
    _audit_check(AUDIT_NEW)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM group_audit WHERE action IN ('exam_release_pending', 'exam_release_done')")
    _audit_check(AUDIT_OLD)
    op.execute("DROP INDEX IF EXISTS ix_attendance_exam_release_pending")
    op.execute("ALTER TABLE group_attendance DROP COLUMN IF EXISTS exam_release_pending")
