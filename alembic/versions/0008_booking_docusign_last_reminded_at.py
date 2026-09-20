"""bookings.docusign_last_reminded_at (dashboard reminder button)

The detail page gets a "Send reminder via DocuSign" button that resends the
existing envelope's signing email. This column records when the owner last
pressed it, so the page can show the date later. Nullable, no default: rows
written before this migration and bookings never reminded read as NULL, and
the previous release neither reads nor writes the column.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-19

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '1min'")
    op.add_column(
        "bookings",
        sa.Column("docusign_last_reminded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("SET statement_timeout = '1min'")
    op.drop_column("bookings", "docusign_last_reminded_at")
