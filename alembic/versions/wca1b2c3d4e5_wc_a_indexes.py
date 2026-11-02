"""Wave C-A (MN-48 / LD-29): three indexes for the reads that scale with the centre.

Revision ID: wca1b2c3d4e5
Revises: wb4a1b2c3d4e5
Create Date: 2026-11-02

* ``student_groups (group_id)`` - «who is in this group» (the ledger select by group, the price impact, payroll); the unique constraint leads with ``student_id``.
* ``group_attendance (org_id, lesson_date) WHERE status = 'completed'`` - the held lessons of a centre in a month: the calculation of the payroll sheets reads exactly
  ``status = 'completed'`` (``payroll_service._collect``).  The ledger of a centre does NOT use it: it reads ``status IN ('cancelled', 'completed')`` / ``= 'cancelled'``, which a
  partial index on ``completed`` cannot serve.  The existing ``idx_attendance_org`` has no date, so it returns every lesson of the centre of every year (to be confirmed with
  EXPLAIN of the month's calculation on stage).
* ``organization_members (user_id)`` - «where is this user a member» (sign-in, the live-access check); ``uq_org_member`` leads with ``org_id``.

Only ADDS (plain ``CREATE INDEX IF NOT EXISTS``: before the launch the tables are small, so no CONCURRENTLY; the models carry the same names).  The downgrade drops them.
"""
from __future__ import annotations

from alembic import op

revision: str = "wca1b2c3d4e5"
down_revision: str = "wb4a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_student_groups_group ON student_groups (group_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_attendance_org_completed_date ON group_attendance (org_id, lesson_date) WHERE status = 'completed'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_org_members_user ON organization_members (user_id)")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_org_members_user")
    op.execute("DROP INDEX IF EXISTS ix_attendance_org_completed_date")
    op.execute("DROP INDEX IF EXISTS ix_student_groups_group")
