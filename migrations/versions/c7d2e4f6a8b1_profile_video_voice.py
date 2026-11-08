"""profile video voice, and deleting AI videos

The voice (female or male) a person's AI videos speak in; and when a person deleted one of their AI videos.

Revision ID: c7d2e4f6a8b1
Revises: a3e9d7c1b5f2
Create Date: 2026-11-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d2e4f6a8b1'
down_revision: Union[str, Sequence[str], None] = 'a3e9d7c1b5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Chosen when someone makes a video; their likeness videos use the same voice. Unset until then."""
    op.add_column('user_profiles', sa.Column('video_voice', sa.String(length=10), nullable=True))
    # A deleted video leaves the person's list and its file goes, but the record stays so its watermark can still
    # trace a leaked copy.
    op.add_column('generated_videos', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('generated_videos', 'deleted_at')
    op.drop_column('user_profiles', 'video_voice')
