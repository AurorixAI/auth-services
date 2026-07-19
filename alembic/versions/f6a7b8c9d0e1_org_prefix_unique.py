"""org student_id_prefix uniqueness (initials-based prefixes: NC → NC1)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-19

Follow-up to e5f6a7b8c9d0 (which is already merged/deployed and MUST NOT be
edited). The org prefix is now the initials of the organisation name and must be
UNIQUE across organisations so the collision suffixing (NC → NC1 → NC2), done in
student_id_service.ensure_org_prefix, is safe.

Adds a UNIQUE index on organizations.student_id_prefix. NULLs are distinct in
Postgres, so organisations that haven't generated any student ID yet don't clash.
Idempotent / re-runnable.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_org_student_id_prefix "
        "ON organizations (student_id_prefix)"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS uq_org_student_id_prefix"))
