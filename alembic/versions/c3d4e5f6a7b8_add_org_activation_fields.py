"""add org activation fields

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The baseline migration creates organizations from the current model state,
    # which already includes these columns. Use IF NOT EXISTS so this is a no-op
    # when running on a fresh database where baseline already created them.
    op.execute(sa.text(
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS activation_token VARCHAR(500)"
    ))
    op.execute(sa.text(
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS activation_token_expires_at TIMESTAMP"
    ))

    # The baseline uses SQLAlchemy's auto-naming for the check constraint (no
    # explicit name), so the constraint may have a different name than expected.
    # Drop whatever exists and recreate with the canonical name.
    op.execute(sa.text(
        "ALTER TABLE organizations DROP CONSTRAINT IF EXISTS organizations_status_check"
    ))
    op.execute(sa.text(
        "ALTER TABLE organizations ADD CONSTRAINT organizations_status_check "
        "CHECK (status IN ('pending', 'active', 'suspended', 'rejected'))"
    ))


def downgrade() -> None:
    op.drop_constraint('organizations_status_check', 'organizations', type_='check')
    op.create_check_constraint(
        'organizations_status_check',
        'organizations',
        sa.text("status IN ('pending', 'active', 'suspended')")
    )
    op.drop_column('organizations', 'activation_token_expires_at')
    op.drop_column('organizations', 'activation_token')
