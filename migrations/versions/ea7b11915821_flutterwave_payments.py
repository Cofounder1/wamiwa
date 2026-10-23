"""flutterwave payments

Revision ID: ea7b11915821
Revises: c2d26f2b1566
Create Date: 2026-10-18 18:03:30.494893

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import app.models.base


# revision identifiers, used by Alembic.
revision: str = 'ea7b11915821'
down_revision: Union[str, Sequence[str], None] = 'c2d26f2b1566'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """PY-05 Flutterwave: saved cards for renewals, and foreign bank details for dollar payouts."""
    op.add_column('payments', sa.Column('reusable_method', sa.String(length=200), nullable=True))
    op.add_column('bank_accounts', sa.Column('details', sa.JSON(), nullable=True))
    if op.get_bind().dialect.name != 'sqlite':  # SQLite cannot alter a column and does not enforce the length
        op.alter_column('bank_accounts', 'account_number', existing_type=sa.VARCHAR(length=20),
                        type_=sa.String(length=64), existing_nullable=False)


def downgrade() -> None:
    if op.get_bind().dialect.name != 'sqlite':
        op.alter_column('bank_accounts', 'account_number', existing_type=sa.String(length=64),
                        type_=sa.VARCHAR(length=20), existing_nullable=False)
    op.drop_column('bank_accounts', 'details')
    op.drop_column('payments', 'reusable_method')
