"""seam_api data point source

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-22

The access-code handler now records the door code the Seam API actually set
(from the create response) as a DataPoint, so the dashboard can display the
real value rather than re-deriving it from the phone number. That provenance
needs its own source: 'seam_api'.

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE data_point_source ADD VALUE IF NOT EXISTS 'seam_api'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value; leaving it in place is harmless.
    pass
