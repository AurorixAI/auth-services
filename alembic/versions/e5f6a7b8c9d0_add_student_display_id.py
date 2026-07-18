"""add student display_id: public per-org sequential IDs

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-18

Adds the public "Display-ID" for students (zero-friction test lobby):

  * organizations.student_id_prefix — optional prefix (e.g. "AL15"). If NULL,
    it's derived from the org name at generation time (see student_id_service).
  * students.sequence_number        — per-org running number (max+1).
  * students.display_id             — formatted public ID, e.g. "AL15-001".

Both student columns are nullable: a lead sitting in the diagnostic buffer has
no ID until actual enrollment (import insert / assign-to-group).

Uniqueness is per-organization, enforced with partial-free UNIQUE indexes
(Postgres treats NULLs as distinct, so buffered students don't clash).

Backfill: historical students (created before this feature) are numbered once,
per org, ordered by created_at — so existing group lists aren't full of blanks.
The SQL prefix derivation mirrors student_id_service._derive_prefix_from_name.
Everything is idempotent / re-runnable.
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

    # ── 2. Indexes / per-org uniqueness (NULLs are distinct in PG) ──────────
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

    # ── 3. One-time backfill of historical rows ─────────────────────────────
    # prefix = COALESCE(explicit, <letters[:4]><digits[:4]>, 'ST')
    # seq    = ROW_NUMBER() over each org, ordered by created_at (stable).
    op.execute(sa.text(
        """
        WITH numbered AS (
            SELECT
                s.id AS student_id,
                ROW_NUMBER() OVER (
                    PARTITION BY s.org_id
                    ORDER BY s.created_at ASC, s.id ASC
                ) AS rn,
                COALESCE(
                    NULLIF(UPPER(TRIM(o.student_id_prefix)), ''),
                    NULLIF(
                        COALESCE(
                            NULLIF(UPPER(SUBSTRING(
                                REGEXP_REPLACE(o.name, '[^A-Za-z]', '', 'g')
                                FROM 1 FOR 4)), ''),
                            ''
                        )
                        || SUBSTRING(
                                REGEXP_REPLACE(o.name, '[^0-9]', '', 'g')
                                FROM 1 FOR 4),
                        ''
                    ),
                    'ST'
                ) AS prefix
            FROM students s
            JOIN organizations o ON o.id = s.org_id
            WHERE s.display_id IS NULL
        )
        UPDATE students t
        SET sequence_number = n.rn,
            display_id = n.prefix || '-' || LPAD(n.rn::text, 3, '0')
        FROM numbered n
        WHERE t.id = n.student_id
        """
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS uq_student_org_display_id"))
    op.execute(sa.text("DROP INDEX IF EXISTS uq_student_org_sequence"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_students_display_id"))
    op.execute(sa.text("ALTER TABLE students DROP COLUMN IF EXISTS display_id"))
    op.execute(sa.text("ALTER TABLE students DROP COLUMN IF EXISTS sequence_number"))
    op.execute(sa.text(
        "ALTER TABLE organizations DROP COLUMN IF EXISTS student_id_prefix"
    ))
