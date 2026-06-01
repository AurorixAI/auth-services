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
    # Add activation_token and activation_token_expires_at columns to organizations table
    op.add_column('organizations', sa.Column('activation_token', sa.String(length=500), nullable=True))
    op.add_column('organizations', sa.Column('activation_token_expires_at', sa.DateTime(), nullable=True))
    
    # Drop old status check constraint and create updated check constraint
    op.drop_constraint('organizations_status_check', 'organizations', type_='check')
    op.create_check_constraint(
        'organizations_status_check',
        'organizations',
        sa.text("status IN ('pending', 'active', 'suspended', 'rejected')")
    )


def downgrade() -> None:
    op.drop_constraint('organizations_status_check', 'organizations', type_='check')
    op.create_check_constraint(
        'organizations_status_check',
        'organizations',
        sa.text("status IN ('pending', 'active', 'suspended')")
    )
    op.drop_column('organizations', 'activation_token_expires_at')
    op.drop_column('organizations', 'activation_token')
