"""booking_status completed value

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-23

F15 (bug hunt 2026-07-22): bookings had no terminal state, so the dashboard's
active list and every daily scan (HOA window, reminders, requeue) accumulated
every booking forever. Adds BookingStatus.COMPLETED; a new daily job
(complete_past_bookings) flips ACTIVE bookings to COMPLETED once
check_out_date has passed.

"""
from typing import Sequence, Union

from alembic import op


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE booking_status ADD VALUE IF NOT EXISTS 'completed'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value; leaving it in place is harmless.
    pass
