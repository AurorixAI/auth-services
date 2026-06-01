"""sync nullable constraints

Revision ID: a55f9fecfdec
Revises: 001_baseline
Create Date: 2026-04-15 23:13:27.993377

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a55f9fecfdec'
down_revision: Union[str, Sequence[str], None] = '001_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
