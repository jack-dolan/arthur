"""processed messages dead-letter

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-05

Step 19 / R3b: records booking-feed messages that produced no booking
(EmailType.OTHER or a booking that failed to parse) so the poller does not
re-fetch them every 5 minutes forever.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "processed_messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("disposition", sa.String(length=32), nullable=False),
        sa.Column("classified_as", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_processed_messages_message_id"),
    )


def downgrade() -> None:
    op.drop_table("processed_messages")
