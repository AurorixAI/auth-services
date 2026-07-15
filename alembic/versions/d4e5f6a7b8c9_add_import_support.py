"""add import support: nullable student phone + must_change_password

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-16

Supports the bulk-import feature:
  * students.student_phone becomes optional (centers may not have every phone).
  * users.must_change_password flags teacher accounts provisioned with a
    temporary password by an import — they are forced to set their own on
    first login.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Student phone: required → optional
    op.execute(sa.text(
        "ALTER TABLE students ALTER COLUMN student_phone DROP NOT NULL"
    ))

    # Force-reset flag for imported teacher accounts (idempotent)
    op.execute(sa.text(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "must_change_password BOOLEAN NOT NULL DEFAULT FALSE"
    ))


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
    # Note: restoring NOT NULL would fail if any NULL phones exist; left as-is.
    op.execute(sa.text(
        "ALTER TABLE students ALTER COLUMN student_phone SET NOT NULL"
    ))
