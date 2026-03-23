"""rename parent_phone to student_phone and make NOT NULL

Revision ID: 003
Revises: 002
Create Date: 2026-03-01
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "003_rename_parent_phone"
down_revision = "002_baseline"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Rename column
    op.alter_column("students", "parent_phone", new_column_name="student_phone")
    # 2. Fill NULLs so NOT NULL can be applied
    op.execute("UPDATE students SET student_phone = '' WHERE student_phone IS NULL")
    # 3. Make NOT NULL
    op.alter_column(
        "students", "student_phone", nullable=False, existing_type=sa.String(30)
    )


def downgrade():
    op.alter_column(
        "students", "student_phone", nullable=True, existing_type=sa.String(30)
    )
    op.alter_column("students", "student_phone", new_column_name="parent_phone")
