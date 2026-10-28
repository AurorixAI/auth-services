"""#154 (28.10): one-off lessons get a PURPOSE - ``makeup`` / ``extra`` / ``moved`` - and, for an additional lesson, what the teacher gets (``money``); the row of a cancelled lesson that
has a live make-up points at it (``made_up_by``); the group audit learns ``one_off_created`` and ``one_off_money_decided``.

Revision ID: p154a1b2c3d4
Revises: p157a1b2c3d4
Create Date: 2026-10-28

Schema + the old rows in ONE revision (LESSONS 49: no compatibility branches in the code, the old rows are brought to the new rule here):
* a one-off row that a move made (a request of the journal «Замены» points at it by ``moved_to_lesson_id``) -> ``moved``; every other one -> ``extra`` with ``money = 'paid'`` (the
  teacher as before; the students no longer pay for it - the packages of the test centres are recalculated, they are test data);
* CHECKs are added ``NOT VALID`` and then ``VALIDATE``d (no long lock); ``purpose`` becomes NOT NULL after the rows are filled;
* ``group_attendance.made_up_by`` (nullable) + a partial unique index (one make-up lesson makes up for one cancelled lesson).

Rollback (downgrade): the new audit rows are deleted (information only), the CHECK of the audit goes back, the index and the columns are dropped."""
from __future__ import annotations

from alembic import op

revision: str = "p154a1b2c3d4"
down_revision: str = "p157a1b2c3d4"
branch_labels = None
depends_on = None

ONE_OFF = "group_schedule_one_off_lessons"
AUDIT = "group_audit"
AUDIT_CHECK = "ck_group_audit_action"
AUDIT_OLD = (
    "action IN ('created', 'updated', 'teacher_changed', 'student_archived', 'student_restored', "
    "'group_price_set', 'membership_price_set', 'group_price_cancelled', 'membership_price_cancelled', "
    "'payment_writeoff', 'payment_writeoff_void', 'payment_refund', 'payment_refund_void', "
    "'membership_pause_set', 'membership_pause_void', 'opening_balance_set', 'opening_balance_void', "
    "'group_lesson_type_set', 'student_left_group', "
    "'group_archived', 'group_restored', 'lesson_teacher_set', 'lesson_teacher_cleared', 'day_off_voided', 'lesson_restored', 'lesson_cancelled')"
)
AUDIT_NEW = AUDIT_OLD[:-1] + ", 'one_off_created', 'one_off_money_decided')"

CHECKS = {
    "ck_one_off_purpose": "purpose IN ('makeup', 'extra', 'moved')",
    "ck_one_off_money": (
        "(purpose = 'extra' AND money IS NOT NULL AND money IN ('none', 'wait', 'paid')) OR (purpose = 'makeup' AND (money IS NULL OR money IN ('none', 'paid'))) "
        "OR (purpose = 'moved' AND money IS NULL)"
    ),
    "ck_one_off_made_up": "((purpose = 'makeup') = (made_up_lesson_id IS NOT NULL)) AND ((made_up_lesson_id IS NULL) = (made_up_date IS NULL))",
}


def _replace_audit_check(definition: str) -> None:
    op.execute(f"ALTER TABLE {AUDIT} DROP CONSTRAINT IF EXISTS {AUDIT_CHECK}")
    op.execute(f"ALTER TABLE {AUDIT} ADD CONSTRAINT {AUDIT_CHECK} CHECK ({definition}) NOT VALID")
    op.execute(f"ALTER TABLE {AUDIT} VALIDATE CONSTRAINT {AUDIT_CHECK}")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"ALTER TABLE {ONE_OFF} ADD COLUMN IF NOT EXISTS purpose VARCHAR(10), ADD COLUMN IF NOT EXISTS money VARCHAR(10), "
        "ADD COLUMN IF NOT EXISTS made_up_lesson_id VARCHAR(100), ADD COLUMN IF NOT EXISTS made_up_date VARCHAR(10)"
    )
    op.execute(
        f"UPDATE {ONE_OFF} SET purpose = 'moved' WHERE purpose IS NULL AND id IN "
        "(SELECT moved_to_lesson_id FROM lesson_substitution_requests WHERE moved_to_lesson_id IS NOT NULL)"
    )
    op.execute(f"UPDATE {ONE_OFF} SET purpose = 'extra', money = 'paid' WHERE purpose IS NULL")
    for name, definition in CHECKS.items():
        op.execute(f"ALTER TABLE {ONE_OFF} DROP CONSTRAINT IF EXISTS {name}")          # a database built from the models already has them (the chain is re-runnable)
        op.execute(f"ALTER TABLE {ONE_OFF} ADD CONSTRAINT {name} CHECK ({definition}) NOT VALID")
        op.execute(f"ALTER TABLE {ONE_OFF} VALIDATE CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {ONE_OFF} ALTER COLUMN purpose SET NOT NULL")
    op.execute("ALTER TABLE group_attendance ADD COLUMN IF NOT EXISTS made_up_by VARCHAR(100)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_group_attendance_made_up_by ON group_attendance (group_id, made_up_by) WHERE made_up_by IS NOT NULL"
    )
    _replace_audit_check(AUDIT_NEW)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DELETE FROM {AUDIT} WHERE action IN ('one_off_created', 'one_off_money_decided')")
    _replace_audit_check(AUDIT_OLD)
    op.execute("DROP INDEX IF EXISTS uq_group_attendance_made_up_by")
    op.execute("ALTER TABLE group_attendance DROP COLUMN IF EXISTS made_up_by")
    for name in CHECKS:
        op.execute(f"ALTER TABLE {ONE_OFF} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(
        f"ALTER TABLE {ONE_OFF} DROP COLUMN IF EXISTS made_up_date, DROP COLUMN IF EXISTS made_up_lesson_id, DROP COLUMN IF EXISTS money, DROP COLUMN IF EXISTS purpose"
    )
