"""Background task dispatch for booking workflow.

Phase 4: real dispatcher — iterates PENDING Phase 4 tasks in deterministic order,
calls each handler, commits after each task, and isolates failures so one failing
task cannot prevent the others from running.

Called via asyncio.create_task() from app/routers/dashboard.py after a
successful contact-info save. Must accept only serializable arguments
(UUID, not ORM objects) because the DB session is closed before it runs.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401 — used in type hints

from app.db.models import (  # noqa: F401
    Booking,
    BookingStatus,
    BookingTask,
    TaskState,
    TaskType,
)
from app.db.session import AsyncSessionLocal
from app.tasks.handlers.access_code import handle_access_code_create
from app.tasks.handlers.cleaner_sheet import handle_cleaner_sheet
from app.tasks.handlers.docusign import handle_docusign_send
from app.tasks.handlers.hoa import handle_hoa_email

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Handler registry (D-01)
# ---------------------------------------------------------------------------

HANDLERS = {
    TaskType.CLEANER_SHEET_ADD: handle_cleaner_sheet,
    TaskType.DOCUSIGN_SEND: handle_docusign_send,
    TaskType.ACCESS_CODE_CREATE: handle_access_code_create,
    TaskType.HOA_EMAIL: handle_hoa_email,
}

# Deterministic dispatch order — Phase 4 tasks run in this sequence.
# Phase 5 will add reminder task types that the dispatcher ignores (no handler entry).
_DISPATCH_ORDER = [
    TaskType.CLEANER_SHEET_ADD,
    TaskType.DOCUSIGN_SEND,
    TaskType.ACCESS_CODE_CREATE,
    TaskType.HOA_EMAIL,
]


async def _dispatch_pending_tasks(booking_id: uuid.UUID) -> None:
    """Dispatch all PENDING tasks for a booking immediately.

    Iterates Phase 4 task types in _DISPATCH_ORDER. For each task that is
    PENDING (and has a registered handler), calls the handler then commits.
    On exception: marks task FAILED, records error, increments attempt_count,
    commits, and continues to the next task (D-02 failure isolation).

    SEAM-03 note: handle_access_code_create sets task.state = WAITING when
    guest_phone is None; the dispatcher's commit then persists WAITING.
    The dispatcher MUST NOT overwrite WAITING with FAILED — the handler
    returns cleanly (no exception), so the except block is never reached.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Booking).where(Booking.id == booking_id))
        booking = result.scalar_one_or_none()

        if booking is None:
            log.warning("dispatch: booking %s not found", booking_id)
            return

        # F5 (bug hunt 2026-07-22): never dispatch for a non-ACTIVE booking —
        # stale PENDING tasks on a cancelled stay would create a real envelope,
        # door code and cleaner row. Defense in depth alongside the
        # cancellation-time task skip and the save_contact guard.
        if booking.status != BookingStatus.ACTIVE:
            log.warning(
                "dispatch: booking %s is %s; refusing to dispatch",
                booking_id,
                booking.status.value,
            )
            return

        # Eagerly load tasks so we can iterate them without a DB round-trip per task
        await session.refresh(booking, attribute_names=["tasks"])

        tasks_by_type = {t.task_type: t for t in booking.tasks}

        # Build the handler lookup at dispatch-time from module globals so that
        # unit tests can patch individual handlers via
        # patch("app.tasks.dispatch.handle_cleaner_sheet", ...) and have those
        # patches reflected here (module-level HANDLERS dict is populated at
        # import time and would not see patches applied later).
        _mod = globals()
        _handler_names = {
            TaskType.CLEANER_SHEET_ADD: "handle_cleaner_sheet",
            TaskType.DOCUSIGN_SEND: "handle_docusign_send",
            TaskType.ACCESS_CODE_CREATE: "handle_access_code_create",
            TaskType.HOA_EMAIL: "handle_hoa_email",
        }

        for task_type in _DISPATCH_ORDER:
            task: BookingTask | None = tasks_by_type.get(task_type)
            if task is None:
                continue

            # Fast in-memory gate: skip WAITING/COMPLETE/FAILED/IN_PROGRESS/SKIPPED
            # without touching the DB. The atomic claim below is the actual
            # concurrency guard; this is a cheap short-circuit for the common case.
            if task.state != TaskState.PENDING:
                continue

            handler_name = _handler_names.get(task_type)
            handler = _mod.get(handler_name) if handler_name else None
            if handler is None:
                # No Phase 4 handler registered — skip silently
                continue

            # Atomic claim (Step 17 / R2): flip PENDING -> IN_PROGRESS in a single
            # UPDATE guarded on the current state, committed immediately. Under
            # Postgres READ COMMITTED the row lock serialises concurrent dispatches
            # (e.g. a double contact-info POST): the loser's UPDATE matches zero
            # rows (state is no longer PENDING) and skips, so a task's handler — and
            # its external side effect (DocuSign envelope, Seam code) — runs at most
            # once. Also skips WAITING/COMPLETE/FAILED/SKIPPED without a handler run.
            claimed = (
                await session.execute(
                    update(BookingTask)
                    .where(
                        BookingTask.id == task.id,
                        BookingTask.state == TaskState.PENDING,
                    )
                    .values(state=TaskState.IN_PROGRESS)
                    .returning(BookingTask.id)
                )
            ).scalar_one_or_none()
            if claimed is None:
                # Not PENDING (already claimed by a concurrent dispatch, or in a
                # non-dispatchable state) — do not run the handler.
                await session.commit()
                continue
            await session.commit()  # publish the claim so a racing dispatch sees it

            # Captured while the instance is known-fresh: after a rollback the
            # whole identity map is expired, and reading an expired attribute
            # on an AsyncSession raises MissingGreenlet.
            task_id = task.id
            prev_attempts = task.attempt_count or 0
            try:
                await handler(booking, task, session)
                await session.commit()  # D-02: commit progress after each successful task
            except Exception as exc:
                # Log FIRST — guaranteed even if the DB is unreachable below.
                log.exception(
                    "dispatch: task %s failed for booking %s", task_type, booking_id
                )
                failure_error = str(exc)
                # F12 (bug hunt 2026-07-22): the handler may have left the
                # transaction ABORTED (mid-flush IntegrityError, dead
                # connection). Committing on top of that raises
                # PendingRollbackError, which used to escape the dispatcher:
                # the failure was never recorded (task stranded IN_PROGRESS
                # forever) and every later task was skipped. Roll back first,
                # then record FAILED via a core UPDATE that never reads
                # (possibly expired) ORM state.
                try:
                    await session.rollback()
                    await session.execute(
                        update(BookingTask)
                        .where(BookingTask.id == task_id)
                        .values(
                            state=TaskState.FAILED,
                            last_error=failure_error,
                            attempt_count=prev_attempts + 1,
                        )
                    )
                    await session.commit()
                    # The rollback expired the whole identity map — the next
                    # iteration reads task.state, so re-load the collection
                    # fresh (no-op under mocked sessions in unit tests).
                    await session.refresh(booking, attribute_names=["tasks"])
                    tasks_by_type = {t.task_type: t for t in booking.tasks}
                except Exception:
                    log.exception(
                        "dispatch: could not record FAILED for task %s (booking "
                        "%s) — the DB row may remain IN_PROGRESS (claimed); "
                        "inspect manually",
                        task_type,
                        booking_id,
                    )
                    try:
                        await session.rollback()
                    except Exception:
                        log.exception("dispatch: recovery rollback also failed")
                    return  # session state is unreliable; stop this dispatch
                # Mirror onto the original in-memory instance (set-only — no
                # reads) so unit-level callers observe the failure too.
                task.state = TaskState.FAILED
                task.last_error = failure_error
                task.attempt_count = prev_attempts + 1
