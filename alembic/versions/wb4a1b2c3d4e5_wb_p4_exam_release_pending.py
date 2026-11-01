"""Wave B P4 (B8 / SV-15, LD-23): ``group_attendance.exam_release_pending``.

Revision ID: wb4a1b2c3d4e5
Revises: k4a1b2c3d4e5
Create Date: 2026-11-02

The release of the exams of a CANCELLED lesson is asked of exam-service after the commit; when it does not answer, the cancelled row keeps this marker and the next write
to the group (or ``scripts/sweep_exam_releases.py``) retries it.  ``BOOLEAN NOT NULL DEFAULT false`` is a catalogue-only change on PostgreSQL 11+ (no table rewrite);
the partial index holds only the rows that wait (normally none).

Only ADDS.  Idempotent (``IF NOT EXISTS``); the downgrade drops the index and the column (nothing else depends on them).
"""
from __future__ import annotations

from alembic import op

revision: str = "wb4a1b2c3d4e5"
down_revision: str = "k4a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE group_attendance ADD COLUMN IF NOT EXISTS exam_release_pending BOOLEAN NOT NULL DEFAULT false")
    op.execute("CREATE INDEX IF NOT EXISTS ix_attendance_exam_release_pending ON group_attendance (org_id, group_id) WHERE exam_release_pending")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_attendance_exam_release_pending")
    op.execute("ALTER TABLE group_attendance DROP COLUMN IF EXISTS exam_release_pending")
