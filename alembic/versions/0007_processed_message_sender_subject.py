"""processed_messages sender + subject (classifier-drift digest)

Sustainability audit 2026-07-23, item 4: the weekly drift digest surfaces
OTHER dead-letters from platform domains (airbnb/vrbo/homeaway) for human
review, which requires the original sender and subject to be recorded at
dead-letter time. Both columns are nullable — rows recorded before this
migration simply have no values.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-24

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "processed_messages",
        sa.Column("sender", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "processed_messages",
        sa.Column("subject", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("processed_messages", "subject")
    op.drop_column("processed_messages", "sender")
