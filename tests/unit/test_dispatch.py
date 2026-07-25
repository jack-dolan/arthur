"""Wave 0 failing tests for the central task dispatcher (covers D-01, D-02, D-03).

All imports are inside test functions (project convention). These tests are
intentionally RED — the stub _dispatch_pending_tasks only logs; it does not
iterate tasks, call handlers, or write to DB. Wave 2 will replace the stub
with full implementation.

Requirements covered:
  D-01: Handlers run sequentially; each owns its own try/except
  D-02: Commit after each task to preserve partial progress
  D-03: SEAM-03 — missing phone sets task WAITING, not FAILED
  SEAM-03: Dispatcher-level behavior for missing guest_phone
"""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models import (
    Booking,
    BookingStatus,
    BookingTask,
    Platform,
    TaskState,
    TaskType,
)


def _async_ctx(obj):
    """Return an async context manager that yields *obj*."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=obj)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_booking_with_tasks(
    task_overrides: dict[TaskType, tuple[TaskState, str | None]] | None = None,
    *,
    guest_phone: str | None = "+15551234999",
):
    """Create an in-memory Booking with Phase 4 task set."""
    booking = Booking(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id="HMDISP01",
        property_id="test_property",
        guest_first_name="Dispatch",
        guest_last_name="Test",
        guest_phone=guest_phone,
        guest_email="dispatch@example.com",
        check_in_date=date(2026, 8, 1),
        check_out_date=date(2026, 8, 5),
        status=BookingStatus.ACTIVE,
        source_email_message_id="msg-disp-1",
    )
    default_tasks = {
        TaskType.OWNER_ALERT_NEW_BOOKING: (TaskState.COMPLETE, None),
        TaskType.CLEANER_SHEET_ADD: (TaskState.PENDING, None),
        TaskType.DOCUSIGN_SEND: (TaskState.PENDING, None),
        TaskType.ACCESS_CODE_CREATE: (TaskState.PENDING, None),
        TaskType.HOA_EMAIL: (TaskState.WAITING, None),  # waiting for DocuSign
    }
    if task_overrides:
        default_tasks.update(task_overrides)
    for task_type, (state, ext_ref) in default_tasks.items():
        booking.tasks.append(BookingTask(task_type=task_type, state=state, external_ref=ext_ref))
    return booking


# ---------------------------------------------------------------------------
# Sequential dispatch (D-01)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_iterates_pending_tasks_sequentially():
    """D-01: PENDING tasks are dispatched in order; WAITING tasks are skipped.

    Expected order: CLEANER_SHEET_ADD, DOCUSIGN_SEND, ACCESS_CODE_CREATE.
    HOA_EMAIL is WAITING → skipped.
    OWNER_ALERT_NEW_BOOKING is COMPLETE → skipped.
    """
    from app.tasks.dispatch import _dispatch_pending_tasks

    booking = _make_booking_with_tasks()
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=booking))
    )
    session.refresh = AsyncMock()
    session.commit = AsyncMock()

    call_order = []

    async def mock_handler(b, task, s):
        call_order.append(task.task_type)
        task.state = TaskState.COMPLETE

    with (
        patch("app.tasks.dispatch.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch("app.tasks.dispatch.handle_cleaner_sheet", side_effect=mock_handler),
        patch("app.tasks.dispatch.handle_docusign_send", side_effect=mock_handler),
        patch("app.tasks.dispatch.handle_access_code_create", side_effect=mock_handler),
        patch("app.tasks.dispatch.handle_hoa_email", side_effect=mock_handler),
    ):
        await _dispatch_pending_tasks(booking.id)

    assert TaskType.CLEANER_SHEET_ADD in call_order
    assert TaskType.DOCUSIGN_SEND in call_order
    assert TaskType.ACCESS_CODE_CREATE in call_order
    # HOA_EMAIL is WAITING — must not have been dispatched
    assert TaskType.HOA_EMAIL not in call_order


# ---------------------------------------------------------------------------
# Failure isolation (D-01, D-02)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_handler_failure_isolates_other_tasks():
    """D-01: One handler raising does not abort remaining tasks.
    D-02: Failed task gets state=FAILED, last_error set, attempt_count incremented.
    """
    from app.tasks.dispatch import _dispatch_pending_tasks

    booking = _make_booking_with_tasks()
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=booking))
    )
    session.refresh = AsyncMock()
    session.commit = AsyncMock()

    call_count = {"n": 0}

    async def failing_cleaner(b, task, s):
        raise RuntimeError("sheets API unavailable")

    async def succeeding_handler(b, task, s):
        call_count["n"] += 1
        task.state = TaskState.COMPLETE

    with (
        patch("app.tasks.dispatch.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch("app.tasks.dispatch.handle_cleaner_sheet", side_effect=failing_cleaner),
        patch("app.tasks.dispatch.handle_docusign_send", side_effect=succeeding_handler),
        patch("app.tasks.dispatch.handle_access_code_create", side_effect=succeeding_handler),
        patch("app.tasks.dispatch.handle_hoa_email", side_effect=succeeding_handler),
    ):
        await _dispatch_pending_tasks(booking.id)

    # CLEANER_SHEET_ADD should be FAILED
    cleaner_task = next(t for t in booking.tasks if t.task_type == TaskType.CLEANER_SHEET_ADD)
    assert cleaner_task.state == TaskState.FAILED
    assert cleaner_task.last_error is not None
    assert cleaner_task.attempt_count >= 1

    # Remaining handlers still ran
    assert call_count["n"] >= 2


# ---------------------------------------------------------------------------
# Commit-per-task (D-02)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_commits_after_each_task():
    """D-02: session.commit() is called once per executed task."""
    from app.tasks.dispatch import _dispatch_pending_tasks

    booking = _make_booking_with_tasks()
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=booking))
    )
    session.refresh = AsyncMock()
    session.commit = AsyncMock()

    async def ok_handler(b, task, s):
        task.state = TaskState.COMPLETE

    with (
        patch("app.tasks.dispatch.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch("app.tasks.dispatch.handle_cleaner_sheet", side_effect=ok_handler),
        patch("app.tasks.dispatch.handle_docusign_send", side_effect=ok_handler),
        patch("app.tasks.dispatch.handle_access_code_create", side_effect=ok_handler),
        patch("app.tasks.dispatch.handle_hoa_email", side_effect=ok_handler),
    ):
        await _dispatch_pending_tasks(booking.id)

    # 3 PENDING tasks (CLEANER_SHEET_ADD, DOCUSIGN_SEND, ACCESS_CODE_CREATE)
    # HOA_EMAIL is WAITING — not dispatched, no commit for it
    assert session.commit.await_count >= 3


# ---------------------------------------------------------------------------
# SEAM-03: missing phone → WAITING (D-03)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_seam_missing_phone_sets_waiting_not_failed():
    """D-03/SEAM-03: When guest_phone is None, ACCESS_CODE_CREATE task ends WAITING."""
    from app.tasks.dispatch import _dispatch_pending_tasks

    booking = _make_booking_with_tasks(guest_phone=None)
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=booking))
    )
    session.refresh = AsyncMock()
    session.commit = AsyncMock()

    async def ok_handler(b, task, s):
        task.state = TaskState.COMPLETE

    async def seam_missing_phone(b, task, s):
        # Simulate the SEAM-03 handler behavior: set WAITING when no phone
        task.state = TaskState.WAITING

    with (
        patch("app.tasks.dispatch.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch("app.tasks.dispatch.handle_cleaner_sheet", side_effect=ok_handler),
        patch("app.tasks.dispatch.handle_docusign_send", side_effect=ok_handler),
        patch("app.tasks.dispatch.handle_access_code_create", side_effect=seam_missing_phone),
        patch("app.tasks.dispatch.handle_hoa_email", side_effect=ok_handler),
    ):
        await _dispatch_pending_tasks(booking.id)

    seam_task = next(t for t in booking.tasks if t.task_type == TaskType.ACCESS_CODE_CREATE)
    assert seam_task.state == TaskState.WAITING


# ---------------------------------------------------------------------------
# Unknown task type — graceful skip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_skips_tasks_not_in_phase_4_handler_map():
    """Tasks with no Phase 4 handler (e.g., OWNER_ALERT_NEW_BOOKING PENDING) are
    skipped silently."""
    from app.tasks.dispatch import _dispatch_pending_tasks

    booking = _make_booking_with_tasks(task_overrides={
        TaskType.OWNER_ALERT_NEW_BOOKING: (TaskState.PENDING, None),  # override default COMPLETE
        TaskType.CLEANER_SHEET_ADD: (TaskState.WAITING, None),
        TaskType.DOCUSIGN_SEND: (TaskState.WAITING, None),
        TaskType.ACCESS_CODE_CREATE: (TaskState.WAITING, None),
    })
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=booking))
    )
    session.refresh = AsyncMock()
    session.commit = AsyncMock()

    alert_called = {"n": 0}

    async def unexpected_call(b, task, s):
        alert_called["n"] += 1

    with (
        patch("app.tasks.dispatch.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch("app.tasks.dispatch.handle_cleaner_sheet", side_effect=unexpected_call),
        patch("app.tasks.dispatch.handle_docusign_send", side_effect=unexpected_call),
        patch("app.tasks.dispatch.handle_access_code_create", side_effect=unexpected_call),
        patch("app.tasks.dispatch.handle_hoa_email", side_effect=unexpected_call),
    ):
        # Should not raise; OWNER_ALERT_NEW_BOOKING has no handler, no exception
        await _dispatch_pending_tasks(booking.id)

    # No Phase 4 handlers called (all tasks were WAITING)
    assert alert_called["n"] == 0

    # OWNER_ALERT_NEW_BOOKING task state unchanged (still PENDING)
    alert_task = next(t for t in booking.tasks if t.task_type == TaskType.OWNER_ALERT_NEW_BOOKING)
    assert alert_task.state == TaskState.PENDING


# ---------------------------------------------------------------------------
# Booking not found — graceful return
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_booking_not_found_logs_warning_and_returns():
    """Passing a UUID with no matching booking returns cleanly (no exception)."""
    from app.tasks.dispatch import _dispatch_pending_tasks

    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )
    session.refresh = AsyncMock()
    session.commit = AsyncMock()

    missing_id = uuid.uuid4()

    with patch("app.tasks.dispatch.AsyncSessionLocal", return_value=_async_ctx(session)):
        # Must return None cleanly, no exception
        result = await _dispatch_pending_tasks(missing_id)

    assert result is None
    session.commit.assert_not_called()


# ---------------------------------------------------------------------------
# F5 (bug hunt 2026-07-22): the dispatcher must never act on a cancelled booking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_refuses_cancelled_booking():
    """A CANCELLED booking with (stale) PENDING tasks must dispatch nothing —
    real external side effects (envelope, door code, cleaner row) for a
    cancelled stay. Defense in depth alongside the cancellation task-skip and
    the save_contact guard."""
    from app.tasks.dispatch import _dispatch_pending_tasks

    booking = _make_booking_with_tasks()
    booking.status = BookingStatus.CANCELLED
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=booking))
    )
    session.refresh = AsyncMock()
    session.commit = AsyncMock()

    handlers_called = []

    async def mock_handler(b, task, s):
        handlers_called.append(task.task_type)

    with (
        patch("app.tasks.dispatch.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch("app.tasks.dispatch.handle_cleaner_sheet", side_effect=mock_handler),
        patch("app.tasks.dispatch.handle_docusign_send", side_effect=mock_handler),
        patch("app.tasks.dispatch.handle_access_code_create", side_effect=mock_handler),
        patch("app.tasks.dispatch.handle_hoa_email", side_effect=mock_handler),
    ):
        await _dispatch_pending_tasks(booking.id)

    assert handlers_called == []
