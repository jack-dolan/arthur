"""Wave 0 failing tests for Seam access code task handlers (SEAM-01, SEAM-02, SEAM-03).

All imports are inside test functions (project convention). These tests are
intentionally RED — the target modules do not exist yet. Wave 2 will create
app/tasks/handlers/access_code.py to make them green.

Requirements covered:
  SEAM-01: Creates time-bound code with last 4 digits of phone, 4PM check-in → 11AM checkout ET/UTC
  SEAM-02: Deletes code by external_ref (access_code_id)
  SEAM-03: If phone is None, flips task to WAITING instead of raising
"""
from __future__ import annotations

import uuid
from datetime import date, timezone
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


def _make_booking(
    *,
    guest_phone: str | None = "+15551234999",
    guest_email: str | None = "guest@example.com",
):
    """Create a minimal in-memory Booking for Seam handler tests."""
    return Booking(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id="HMSEAM01",
        property_id="test_property",
        guest_first_name="Seam",
        guest_last_name="Guest",
        guest_phone=guest_phone,
        guest_email=guest_email,
        check_in_date=date(2026, 8, 1),
        check_out_date=date(2026, 8, 5),
        status=BookingStatus.ACTIVE,
        source_email_message_id="msg-seam-1",
    )


def _make_task(
    task_type: TaskType = TaskType.ACCESS_CODE_CREATE, state: TaskState = TaskState.PENDING
):
    return BookingTask(
        id=uuid.uuid4(),
        task_type=task_type,
        state=state,
    )


# ---------------------------------------------------------------------------
# handle_access_code_create — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_access_code_create_uses_last_four_digits_of_phone():
    """Phone +15551234999 → code '4999'."""
    from app.tasks.handlers.access_code import handle_access_code_create

    booking = _make_booking(guest_phone="+15551234999")
    task = _make_task()
    session = AsyncMock()
    session.add = MagicMock()

    with patch("app.tasks.handlers.access_code.get_seam_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.access_codes.create.return_value = MagicMock(access_code_id="ac-123")
        mock_get_client.return_value = mock_client

        await handle_access_code_create(booking, task, session)

    call_kwargs = mock_client.access_codes.create.call_args.kwargs
    assert call_kwargs.get("code") == "4999"


@pytest.mark.asyncio
async def test_handle_access_code_create_sets_complete_and_stores_external_ref():
    """Happy path: task reaches COMPLETE with external_ref = returned access_code_id."""
    from app.tasks.handlers.access_code import handle_access_code_create

    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()
    session.add = MagicMock()

    with patch("app.tasks.handlers.access_code.get_seam_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.access_codes.create.return_value = MagicMock(access_code_id="ac-123")
        mock_get_client.return_value = mock_client

        await handle_access_code_create(booking, task, session)

    assert task.state == TaskState.COMPLETE
    assert task.external_ref == "ac-123"


@pytest.mark.asyncio
async def test_handle_access_code_create_missing_phone_sets_waiting_not_failed():
    """SEAM-03: guest_phone is None — task flips to WAITING; Seam API NOT called."""
    from app.tasks.handlers.access_code import handle_access_code_create

    booking = _make_booking(guest_phone=None)
    task = _make_task()
    session = AsyncMock()

    with patch("app.tasks.handlers.access_code.get_seam_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        await handle_access_code_create(booking, task, session)

    assert task.state == TaskState.WAITING
    mock_client.access_codes.create.assert_not_called()


@pytest.mark.asyncio
async def test_handle_access_code_create_time_window_uses_4pm_eastern_to_11am_eastern():
    """starts_at = 4PM ET check-in day, ends_at = 11AM ET checkout day, both as UTC ISO."""
    from app.tasks.handlers.access_code import handle_access_code_create

    booking = _make_booking()
    # check_in_date = 2026-08-01 (summer, EDT = UTC-4)
    # 4PM EDT = 20:00 UTC; 11AM EDT = 15:00 UTC
    task = _make_task()
    session = AsyncMock()
    session.add = MagicMock()

    with patch("app.tasks.handlers.access_code.get_seam_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.access_codes.create.return_value = MagicMock(access_code_id="ac-xyz")
        mock_get_client.return_value = mock_client

        await handle_access_code_create(booking, task, session)

    call_kwargs = mock_client.access_codes.create.call_args.kwargs
    starts_at = call_kwargs.get("starts_at")
    ends_at = call_kwargs.get("ends_at")
    # 4PM EDT on 2026-08-01 = 20:00 UTC
    assert "20:00" in starts_at or "2026-08-01T20" in starts_at
    # 11AM EDT on 2026-08-05 = 15:00 UTC
    assert "15:00" in ends_at or "2026-08-05T15" in ends_at


# ---------------------------------------------------------------------------
# _checkin_window — DST boundary tests (pure date math)
# ---------------------------------------------------------------------------

def test_access_code_window_dst_spring_forward_2026_03_08():
    """Spring forward: 4PM EDT on 2026-03-08 → 20:00 UTC."""
    from app.tasks.handlers.access_code import _checkin_window

    starts_at, ends_at = _checkin_window(date(2026, 3, 8), date(2026, 3, 12))
    # 2026-03-08 is after spring-forward (clocks jumped at 2AM → EDT = UTC-4)
    assert starts_at.hour == 20
    assert starts_at.tzinfo == timezone.utc


def test_access_code_window_dst_fall_back_2026_11_01():
    """Fall back day 2026-11-01: clocks fall back at 2AM, so 4PM is EST (UTC-5) = 21:00 UTC.

    2026-11-01 is the first Sunday of November — DST ends at 2AM, transitioning from
    EDT (UTC-4) to EST (UTC-5). Since check-in is at 4PM, which is AFTER the 2AM rollback,
    4PM EST = 21:00 UTC. The zoneinfo library handles this automatically.

    Note: The research doc specifies '20:00 UTC' which corresponds to EDT (UTC-4), but that
    offset is only correct BEFORE the 2AM fall-back. At 4PM the offset is EST=-5, so the
    correct UTC value is 21:00. This test encodes the correct domain rule.
    """
    from app.tasks.handlers.access_code import _checkin_window

    starts_at, ends_at = _checkin_window(date(2026, 11, 1), date(2026, 11, 5))
    # After fall-back at 2AM on 2026-11-01: 4PM EST = 21:00 UTC
    assert starts_at.hour == 21
    assert starts_at.tzinfo == timezone.utc


@pytest.mark.asyncio
async def test_handle_access_code_create_records_code_from_api_response():
    """The door code shown on the dashboard must be the value Seam actually set
    (from the create response), NOT a re-derivation of the phone's last 4 —
    proven here by a response code that differs from the phone digits."""
    from app.db.models import DataPoint, DataPointSource
    from app.tasks.handlers.access_code import handle_access_code_create

    booking = _make_booking(guest_phone="+15551234999")  # last 4 = 4999
    task = _make_task()
    session = AsyncMock()
    session.add = MagicMock()

    with patch("app.tasks.handlers.access_code.get_seam_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.access_codes.create.return_value = MagicMock(
            access_code_id="ac-123", code="7777"
        )
        mock_get_client.return_value = mock_client

        await handle_access_code_create(booking, task, session)

    added = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], DataPoint)]
    assert len(added) == 1
    dp = added[0]
    assert dp.field_name == "access_code"
    assert dp.value == "7777"  # the API response value, not "4999"
    assert dp.source == DataPointSource.SEAM_API
    assert "ac-123" in (dp.notes or "")


@pytest.mark.asyncio
async def test_handle_access_code_create_no_data_point_without_response_code():
    """If the Seam response carries no code, nothing is recorded (never guess)."""
    from app.db.models import DataPoint
    from app.tasks.handlers.access_code import handle_access_code_create

    booking = _make_booking(guest_phone="+15551234999")
    task = _make_task()
    session = AsyncMock()
    session.add = MagicMock()

    with patch("app.tasks.handlers.access_code.get_seam_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.access_codes.create.return_value = MagicMock(
            access_code_id="ac-123", code=None
        )
        mock_get_client.return_value = mock_client

        await handle_access_code_create(booking, task, session)

    added = [c.args[0] for c in session.add.call_args_list if isinstance(c.args[0], DataPoint)]
    assert added == []
    assert task.state == TaskState.COMPLETE  # code recording never blocks the task


@pytest.mark.asyncio
async def test_handle_access_code_create_sets_completed_at():
    """F14 (bug hunt 2026-07-22): COMPLETE without completed_at renders a
    blank timestamp on the dashboard."""
    from app.tasks.handlers.access_code import handle_access_code_create

    booking = _make_booking(guest_phone="+1 (555) 010-4242")
    task = _make_task()
    session = AsyncMock()
    session.add = MagicMock()

    mock_client = MagicMock()
    mock_client.access_codes.create.return_value = MagicMock(
        access_code_id="ac-ts-1", code="4242"
    )
    with (
        patch(
            "app.tasks.handlers.access_code.get_seam_client",
            return_value=mock_client,
        ),
        patch("app.config.load_config") as mock_cfg,
    ):
        mock_cfg.return_value.properties = [
            MagicMock(id="test_property", seam_device_id="dev-1")
        ]
        await handle_access_code_create(booking, task, session)

    assert task.state == TaskState.COMPLETE
    assert task.completed_at is not None
