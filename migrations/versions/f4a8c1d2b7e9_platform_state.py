"""platform state: when Wamiwa went live

Revision ID: f4a8c1d2b7e9
Revises: ea7b11915821
Create Date: 2026-10-27 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models.base


# revision identifiers, used by Alembic.
revision: str = 'f4a8c1d2b7e9'
down_revision: Union[str, Sequence[str], None] = 'ea7b11915821'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Test mode until a Super Admin presses Go live; launched_at records when, and is never cleared."""
    op.create_table(
        'platform_state',
        sa.Column('launched_at', app.models.base.UTCDateTime(timezone=True), nullable=True),
        sa.Column('launched_by', sa.String(length=32), nullable=True),
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('created_at', app.models.base.UTCDateTime(timezone=True), nullable=False),
        sa.Column('updated_at', app.models.base.UTCDateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_platform_state_created_at'), 'platform_state', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_platform_state_created_at'), table_name='platform_state')
    op.drop_table('platform_state')
