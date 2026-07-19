"""add student display_id: public per-org sequential IDs

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-18

Adds the public "Display-ID" for students (zero-friction test lobby):

  * organizations.student_id_prefix — org prefix (initials, e.g. "NC"), UNIQUE
    across organizations. Assigned lazily by student_id_service.ensure_org_prefix
    (collision → NC1, NC2 …); an explicitly set value is respected.
  * students.sequence_number        — per-org running number (max+1).
  * students.display_id             — formatted public ID, e.g. "NC-001".

Both student columns are nullable: a lead sitting in the diagnostic buffer has
no ID until actual enrollment (import insert / assign-to-group).

Uniqueness:
  * students — per organization, via UNIQUE(org_id, sequence_number) and
    UNIQUE(org_id, display_id). Postgres treats NULLs as distinct, so buffered
    students without IDs don't clash.
  * organizations.student_id_prefix — UNIQUE across all orgs (NULLs distinct),
    which is what makes the NC → NC1 collision-suffixing safe.

No data backfill: prefix assignment is collision-aware and lives in Python
(student_id_service); historical students receive an ID the next time they are
imported / enrolled. Everything here is idempotent / re-runnable.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Columns (idempotent) ─────────────────────────────────────────────
    op.execute(sa.text(
        "ALTER TABLE organizations "
        "ADD COLUMN IF NOT EXISTS student_id_prefix VARCHAR(16)"
    ))
    op.execute(sa.text(
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS sequence_number INTEGER"
    ))
    op.execute(sa.text(
        "ALTER TABLE students ADD COLUMN IF NOT EXISTS display_id VARCHAR(32)"
    ))

    # ── 2. Student indexes / per-org uniqueness (NULLs distinct in PG) ──────
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_students_display_id "
        "ON students (display_id)"
    ))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_student_org_sequence "
        "ON students (org_id, sequence_number)"
    ))
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_student_org_display_id "
        "ON students (org_id, display_id)"
    ))

    # ── 3. Org prefix uniqueness across organizations ───────────────────────
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_student_id_prefix "
        "ON organizations (student_id_prefix)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS uq_org_student_id_prefix"))
    op.execute(sa.text("DROP INDEX IF EXISTS uq_student_org_display_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS uq_student_org_sequence"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_students_display_id"))
    op.execute(sa.text("ALTER TABLE students DROP COLUMN IF EXISTS display_id"))
    op.execute(sa.text("ALTER TABLE students DROP COLUMN IF EXISTS sequence_number"))
    op.execute(sa.text(
        "ALTER TABLE organizations DROP COLUMN IF EXISTS student_id_prefix"
    ))
