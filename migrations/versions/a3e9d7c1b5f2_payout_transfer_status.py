"""payout transfer status: sent, then confirmed or failed

Revision ID: a3e9d7c1b5f2
Revises: f4a8c1d2b7e9
Create Date: 2026-11-02 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3e9d7c1b5f2'
down_revision: Union[str, Sequence[str], None] = 'f4a8c1d2b7e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """A payout the provider accepted is "sent" until the bank's answer arrives: "confirmed", or "failed", which
    reverses it. Payouts made before this change were final when made, so they count as confirmed."""
    op.add_column('payouts', sa.Column('transfer_status', sa.String(length=12), nullable=True))
    op.create_index(op.f('ix_payouts_transfer_status'), 'payouts', ['transfer_status'], unique=False)
    op.execute("UPDATE payouts SET transfer_status = 'confirmed' WHERE status = 'paid'")


def downgrade() -> None:
    op.drop_index(op.f('ix_payouts_transfer_status'), table_name='payouts')
    op.drop_column('payouts', 'transfer_status')
