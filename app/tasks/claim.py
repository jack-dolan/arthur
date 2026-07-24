"""Atomic task-state claim shared across concurrent workflow paths.

Some booking tasks can be driven by more than one trigger at the same time —
e.g. the HOA email is sent either by the DocuSign completed-webhook (immediate,
when the booking is already in the HOA window) OR by the hourly
``check_hoa_window`` scan. Without a serialisation point, both paths can observe
the task as WAITING and both perform the external side effect (two HOA emails).

``claim_task`` is that serialisation point: a single ``UPDATE ... WHERE
state=<expect> RETURNING`` guarded on the current state, committed immediately.
Under Postgres READ COMMITTED the row lock serialises concurrent claimants — the
loser's UPDATE matches zero rows and returns False. This is the same claim
family the dispatcher uses for R2 (PENDING -> IN_PROGRESS); here it is reused so
both HOA send paths are mutually exclusive (Step 19 / R1).
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BookingTask, TaskState

log = logging.getLogger(__name__)


async def claim_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    expect: TaskState,
    to: TaskState,
) -> bool:
    """Atomically transition a task from ``expect`` to ``to`` and commit.

    Returns True if this caller won the claim (the row was in ``expect`` state
    and is now ``to``), False if the task was no longer in ``expect`` state
    (another path already claimed/advanced it). The claim is committed so a
    concurrent session sees it immediately.
    """
    claimed = (
        await session.execute(
            update(BookingTask)
            .where(BookingTask.id == task_id, BookingTask.state == expect)
            .values(state=to)
            .returning(BookingTask.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    return claimed is not None
