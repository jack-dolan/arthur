"""Tests for Google Sheets insertion logic and cleaner-sheet handler (SHEET-01, SHEET-02).

Wave 0 tests (pure-logic + auth construction) were made GREEN in Task 1 of plan 04-03.
Handler tests below were added in Task 2 of plan 04-03.

Requirements covered:
  SHEET-01: Sheets service construction with refresh token
  SHEET-02: Row insertion above sentinel in chronological order
"""
from __future__ import annotations

from datetime import date


# ---------------------------------------------------------------------------
# _find_insertion_index — pure logic, no I/O
# ---------------------------------------------------------------------------

def test_find_insertion_index_empty_sheet_returns_sentinel_position():
    """Only sentinel row exists; new check-in should return sentinel_index (0)."""
    from app.integrations.sheets.client import _find_insertion_index

    rows = [["", "--- end ---"]]  # only the sentinel (col A empty, text in col B)
    sentinel_index = 0
    result = _find_insertion_index(rows, date(2026, 8, 1), sentinel_index)
    assert result == 0


def test_find_insertion_index_new_earlier_than_all():
    """New check-in is earlier than all existing rows — returns index of first data row."""
    from app.integrations.sheets.client import _find_insertion_index

    # Real layout: col A = checkbox, col B = guest name, col C = check-in date
    rows = [
        ["FALSE", "Guest A", "9/1/2026", "9/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["FALSE", "Guest B", "10/1/2026", "10/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["", "--- end ---"],
    ]
    sentinel_index = 2
    result = _find_insertion_index(rows, date(2026, 8, 1), sentinel_index)
    assert result == 0


def test_find_insertion_index_new_later_than_all():
    """New check-in is later than all existing rows — returns index after last guest row."""
    from app.integrations.sheets.client import _find_insertion_index

    rows = [
        ["FALSE", "Guest A", "8/1/2026", "8/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["FALSE", "Guest B", "9/1/2026", "9/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["", "--- end ---"],
    ]
    sentinel_index = 2
    result = _find_insertion_index(rows, date(2026, 10, 1), sentinel_index)
    assert result == sentinel_index


def test_find_insertion_index_new_later_than_all_with_trailing_empty_rows():
    """Far-future date skips empty structural rows and lands after the last guest row."""
    from app.integrations.sheets.client import _find_insertion_index

    rows = [
        ["FALSE", "Guest A", "8/1/2026", "8/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["FALSE"],   # empty structural row (checkbox only, no guest data)
        ["FALSE"],   # another empty row
        ["", "--- end ---"],
    ]
    sentinel_index = 3
    # Guest A is at index 0; the backward scan finds 8/1/2026 ≤ 2099-12-28 → insert at 1
    result = _find_insertion_index(rows, date(2099, 12, 28), sentinel_index)
    assert result == 1


def test_find_insertion_index_chronological_middle():
    """Three existing rows; new booking fits between row 1 and row 2."""
    from app.integrations.sheets.client import _find_insertion_index

    rows = [
        ["FALSE", "Guest A", "8/1/2026", "8/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["FALSE", "Guest B", "9/1/2026", "9/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["FALSE", "Guest C", "10/1/2026", "10/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["", "--- end ---"],
    ]
    sentinel_index = 3
    result = _find_insertion_index(rows, date(2026, 9, 15), sentinel_index)
    # Should insert after Guest B (index 1), so result is 2
    assert result == 2


def test_find_insertion_index_no_parseable_dates_returns_sentinel():
    """When no rows have parseable dates in column C, fall back to sentinel_index."""
    from app.integrations.sheets.client import _find_insertion_index

    rows = [
        ["Property Title"],                                     # no column C
        ["FALSE", "Some Guest", "not a date", "", "", ""],      # unparseable col C
        ["", "Do Not Use This Row - Leave This Row At Bottom"],  # sentinel
    ]
    sentinel_index = 2
    result = _find_insertion_index(rows, date(2026, 8, 1), sentinel_index)
    assert result == sentinel_index


def test_find_insertion_index_duplicate_check_in_date():
    """New check-in equals an existing row's date — inserts after the duplicate."""
    from app.integrations.sheets.client import _find_insertion_index

    rows = [
        ["FALSE", "Guest A", "8/1/2026", "8/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["FALSE", "Guest B", "9/1/2026", "9/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["", "--- end ---"],
    ]
    sentinel_index = 2
    # Duplicate of Guest B's check-in date — insert after index 1
    result = _find_insertion_index(rows, date(2026, 9, 1), sentinel_index)
    assert result == 2


def test_find_insertion_index_handles_iso_dates_in_col_c():
    """Rows written by the app (ISO format) sort correctly alongside M/D/YYYY rows."""
    from app.integrations.sheets.client import _find_insertion_index

    rows = [
        ["FALSE", "Guest A", "8/1/2026", "8/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["FALSE", "Guest B", "2026-10-01", "2026-10-05", "4:00:00 PM", "11:00:00 AM"],
        ["", "--- end ---"],
    ]
    sentinel_index = 2
    result = _find_insertion_index(rows, date(2026, 9, 15), sentinel_index)
    # Should insert after Guest A (8/1) but before Guest B (10/1) → index 1
    assert result == 1


# ---------------------------------------------------------------------------
# get_sheets_service — auth construction
# ---------------------------------------------------------------------------

def test_get_sheets_service_uses_refresh_token_from_settings():
    """Service is built with settings.google_sheets_refresh_token as refresh_token."""
    from unittest.mock import MagicMock, patch

    with (
        patch("app.integrations.sheets.client.build") as mock_build,
        patch("app.integrations.sheets.client.Credentials") as mock_creds_cls,
        patch("app.integrations.sheets.client.settings") as mock_settings,
    ):
        mock_settings.google_sheets_refresh_token = "test-refresh-token"
        mock_settings.google_client_id = "test-client-id"
        mock_settings.google_client_secret = "test-client-secret"

        from app.integrations.sheets.client import get_sheets_service
        get_sheets_service()

        # Credentials must be constructed with the refresh token from settings
        call_kwargs = mock_creds_cls.call_args.kwargs
        assert call_kwargs.get("refresh_token") == "test-refresh-token"
        mock_build.assert_called_once()


# ---------------------------------------------------------------------------
# handle_cleaner_sheet — async handler tests (Task 2, Wave 2)
# ---------------------------------------------------------------------------

import uuid
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch, call


def _make_booking(check_in=date(2026, 9, 1), check_out=date(2026, 9, 5), property_id="prop-1"):
    """Create a minimal in-memory Booking for testing."""
    from app.db.models import Booking, BookingStatus, Platform

    return Booking(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id="HMTEST01",
        property_id=property_id,
        guest_first_name="Jane",
        guest_last_name="Smith",
        check_in_date=check_in,
        check_out_date=check_out,
        status=BookingStatus.ACTIVE,
        source_email_message_id="msg-001",
    )


def _make_task():
    """Create a minimal in-memory BookingTask for testing."""
    from app.db.models import BookingTask, TaskState, TaskType

    return BookingTask(
        id=uuid.uuid4(),
        task_type=TaskType.CLEANER_SHEET_ADD,
        state=TaskState.PENDING,
    )


def _make_mock_service(rows=None):
    """Build a deeply-nested mock Google Sheets service."""
    if rows is None:
        rows = [
            ["FALSE", "Guest A", "8/1/2026", "8/5/2026", "4:00:00 PM", "11:00:00 AM"],
            ["", "--- end ---"],
        ]

    mock_service = MagicMock()
    get_result = {"values": rows}
    mock_service.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "Cleaner Schedule", "sheetId": 42}}]
    }
    mock_service.spreadsheets().values().get().execute.return_value = get_result
    mock_service.spreadsheets().batchUpdate().execute.return_value = {}
    mock_service.spreadsheets().values().update().execute.return_value = {}
    return mock_service


def _make_config(spreadsheet_id="test-spreadsheet-id", sheet_name="Cleaner Schedule", property_id="prop-1", sentinel_pattern=None):
    """Build a minimal mock AppConfig with one property."""
    from app.config import AppConfig, PropertyConfig, OwnersConfig, EmailConfig, HOAConfig, CleanerScheduleConfig

    schedule_kwargs = dict(type="google_sheets", spreadsheet_id=spreadsheet_id, sheet_name=sheet_name)
    if sentinel_pattern is not None:
        schedule_kwargs["sentinel_pattern"] = sentinel_pattern

    return AppConfig(
        owners=OwnersConfig(primary_name="Owner", cohost_name="Cohost"),
        email=EmailConfig(booking_feed="feed@example.com", alerts="alerts@example.com"),
        properties=[
            PropertyConfig(
                id=property_id,
                hoa=HOAConfig(enabled=False),
                cleaner_schedule=CleanerScheduleConfig(**schedule_kwargs),
                seam_device_id="seam-dev-1",
                docusign_template_id="ds-tmpl-1",
            )
        ],
    )


import pytest


@pytest.mark.asyncio
async def test_handle_cleaner_sheet_inserts_row_at_correct_index():
    """Handler calls batchUpdate with insertDimension at the index returned by _find_insertion_index."""
    from app.db.models import BookingTask, TaskState

    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()

    rows = [
        ["FALSE", "Guest A", "8/1/2026", "8/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["FALSE", "Guest B", "9/1/2026", "9/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["", "--- end ---"],
    ]
    mock_service = _make_mock_service(rows)
    config = _make_config()

    with (
        patch("app.tasks.handlers.cleaner_sheet.get_sheets_service", return_value=mock_service),
        patch("app.tasks.handlers.cleaner_sheet.load_config", return_value=config),
        patch("app.tasks.handlers.cleaner_sheet._find_insertion_index", return_value=2) as mock_find_idx,
    ):
        from app.tasks.handlers.cleaner_sheet import handle_cleaner_sheet
        await handle_cleaner_sheet(booking, task, session)

    # batchUpdate must have been called
    batch_calls = mock_service.spreadsheets().batchUpdate.call_args_list
    assert len(batch_calls) >= 1, "batchUpdate should have been called at least once"
    # Find the call with the body containing insertDimension
    found_insert = False
    for c in batch_calls:
        body = c.kwargs.get("body") or (c.args[0] if c.args else None)
        if body and "requests" in body:
            for req in body["requests"]:
                if "insertDimension" in req:
                    found_insert = True
                    props = req["insertDimension"]["range"]
                    assert props["startIndex"] == 2
    assert found_insert, "No insertDimension request found in batchUpdate calls"


@pytest.mark.asyncio
async def test_handle_cleaner_sheet_row_contents_are_guest_dates_and_times():
    """values().update is called with the correct 5-column row content."""
    booking = _make_booking(check_in=date(2026, 9, 1), check_out=date(2026, 9, 5))
    task = _make_task()
    session = AsyncMock()

    rows = [["", "--- end ---"]]
    mock_service = _make_mock_service(rows)
    config = _make_config()

    with (
        patch("app.tasks.handlers.cleaner_sheet.get_sheets_service", return_value=mock_service),
        patch("app.tasks.handlers.cleaner_sheet.load_config", return_value=config),
        patch("app.tasks.handlers.cleaner_sheet._find_insertion_index", return_value=0),
    ):
        from app.tasks.handlers.cleaner_sheet import handle_cleaner_sheet
        await handle_cleaner_sheet(booking, task, session)

    # Find the update call — range must be B:F (not A:E), values in M/D/YYYY format.
    update_calls = mock_service.spreadsheets().values().update.call_args_list
    assert len(update_calls) >= 1, "values().update should have been called"
    update_body = None
    update_range = None
    for c in update_calls:
        body_kwarg = c.kwargs.get("body")
        if body_kwarg and "values" in body_kwarg:
            update_body = body_kwarg
            update_range = c.kwargs.get("range", "")
            break
    assert update_body is not None, "No update body with 'values' found"
    row = update_body["values"][0]
    assert row[0] is False          # col A: checkbox explicitly unchecked
    assert row[1] == "Jane Smith"
    assert row[2] == "9/1/2026"    # M/D/YYYY, no zero-padding
    assert row[3] == "9/5/2026"    # M/D/YYYY, no zero-padding
    assert row[4] == "4:00:00 PM"
    assert row[5] == "11:00:00 AM"
    # Write range must start at column A
    assert "A" in update_range, f"Expected range starting at column A, got: {update_range}"


@pytest.mark.asyncio
async def test_handle_cleaner_sheet_rolls_back_blank_row_when_fill_fails():
    """Step 17 partial failure: if the fill (values().update) fails after the row
    was inserted, the handler deletes that blank row (deleteDimension) and re-raises,
    so a retry cannot accumulate orphan blank rows in the real cleaner schedule."""
    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()

    rows = [["", "--- end ---"]]
    mock_service = _make_mock_service(rows)
    # Insert (batchUpdate) succeeds; the fill (values().update) fails.
    mock_service.spreadsheets().values().update().execute.side_effect = RuntimeError("sheets 503")
    config = _make_config()

    with (
        patch("app.tasks.handlers.cleaner_sheet.get_sheets_service", return_value=mock_service),
        patch("app.tasks.handlers.cleaner_sheet.load_config", return_value=config),
        patch("app.tasks.handlers.cleaner_sheet._find_insertion_index", return_value=0),
    ):
        from app.tasks.handlers.cleaner_sheet import handle_cleaner_sheet
        with pytest.raises(RuntimeError):
            await handle_cleaner_sheet(booking, task, session)

    # A deleteDimension rollback at the inserted index (0) must have been issued.
    found_delete = False
    for c in mock_service.spreadsheets().batchUpdate.call_args_list:
        body = c.kwargs.get("body") or (c.args[0] if c.args else None)
        if body and "requests" in body:
            for req in body["requests"]:
                if "deleteDimension" in req:
                    rng = req["deleteDimension"]["range"]
                    if rng["startIndex"] == 0 and rng["endIndex"] == 1:
                        found_delete = True
    assert found_delete, "handler did not roll back the inserted blank row on fill failure"


@pytest.mark.asyncio
async def test_handle_cleaner_sheet_sets_task_complete_on_success():
    """task.state is COMPLETE after a successful handler call."""
    from app.db.models import TaskState

    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()

    rows = [["", "--- end ---"]]
    mock_service = _make_mock_service(rows)
    config = _make_config()

    with (
        patch("app.tasks.handlers.cleaner_sheet.get_sheets_service", return_value=mock_service),
        patch("app.tasks.handlers.cleaner_sheet.load_config", return_value=config),
        patch("app.tasks.handlers.cleaner_sheet._find_insertion_index", return_value=0),
    ):
        from app.tasks.handlers.cleaner_sheet import handle_cleaner_sheet
        await handle_cleaner_sheet(booking, task, session)

    assert task.state == TaskState.COMPLETE


@pytest.mark.asyncio
async def test_handle_cleaner_sheet_uses_spreadsheet_id_from_config():
    """The spreadsheetId passed to all SDK calls matches config.cleaner_schedule.spreadsheet_id."""
    booking = _make_booking(property_id="prop-abc")
    task = _make_task()
    session = AsyncMock()

    rows = [["", "--- end ---"]]
    mock_service = _make_mock_service(rows)
    config = _make_config(spreadsheet_id="spreadsheet-abc", property_id="prop-abc")

    with (
        patch("app.tasks.handlers.cleaner_sheet.get_sheets_service", return_value=mock_service),
        patch("app.tasks.handlers.cleaner_sheet.load_config", return_value=config),
        patch("app.tasks.handlers.cleaner_sheet._find_insertion_index", return_value=0),
    ):
        from app.tasks.handlers.cleaner_sheet import handle_cleaner_sheet
        await handle_cleaner_sheet(booking, task, session)

    # Check that spreadsheets().get() was called with the correct spreadsheetId
    get_calls = mock_service.spreadsheets().get.call_args_list
    assert any(
        c.kwargs.get("spreadsheetId") == "spreadsheet-abc"
        for c in get_calls
    ), f"spreadsheetId='spreadsheet-abc' not found in spreadsheets().get() calls: {get_calls}"


@pytest.mark.asyncio
async def test_handle_cleaner_sheet_wraps_blocking_calls_in_to_thread():
    """asyncio.to_thread is called at least twice (get, batchUpdate/update)."""
    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()

    rows = [["", "--- end ---"]]
    mock_service = _make_mock_service(rows)
    config = _make_config()

    call_count = 0
    original_to_thread = asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await original_to_thread(func, *args, **kwargs)

    with (
        patch("app.tasks.handlers.cleaner_sheet.get_sheets_service", return_value=mock_service),
        patch("app.tasks.handlers.cleaner_sheet.load_config", return_value=config),
        patch("app.tasks.handlers.cleaner_sheet._find_insertion_index", return_value=0),
        patch("app.tasks.handlers.cleaner_sheet.asyncio.to_thread", side_effect=spy_to_thread),
    ):
        from app.tasks.handlers.cleaner_sheet import handle_cleaner_sheet
        await handle_cleaner_sheet(booking, task, session)

    assert call_count >= 2, f"Expected >=2 asyncio.to_thread calls, got {call_count}"


@pytest.mark.asyncio
async def test_handle_cleaner_sheet_property_not_found_raises():
    """ValueError or KeyError is raised if booking.property_id has no config entry."""
    booking = _make_booking(property_id="unknown-property")
    task = _make_task()
    session = AsyncMock()

    config = _make_config(property_id="prop-1")  # booking uses different id

    with (
        patch("app.tasks.handlers.cleaner_sheet.get_sheets_service", return_value=MagicMock()),
        patch("app.tasks.handlers.cleaner_sheet.load_config", return_value=config),
    ):
        from app.tasks.handlers.cleaner_sheet import handle_cleaner_sheet
        with pytest.raises((ValueError, KeyError)):
            await handle_cleaner_sheet(booking, task, session)


# ---------------------------------------------------------------------------
# Sentinel threading — config value flows through to _find_sentinel_index
# ---------------------------------------------------------------------------

def test_find_sentinel_index_custom_pattern():
    """_find_sentinel_index locates a non-default sentinel pattern (case-insensitive)."""
    from app.tasks.handlers.cleaner_sheet import _find_sentinel_index

    rows = [
        ["FALSE", "Guest A", "8/1/2026", "8/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["** FIN **"],  # custom sentinel, uppercase
    ]
    idx = _find_sentinel_index(rows, "** fin **")
    assert idx == 1


def test_find_sentinel_index_sentinel_in_column_b():
    """_find_sentinel_index finds the sentinel when column A is empty and text is in column B.

    This matches the real sheet layout: the sentinel is a merged cell spanning
    columns B–G, so the Sheets API returns it at row[1] with row[0] empty.
    """
    from app.tasks.handlers.cleaner_sheet import _find_sentinel_index

    rows = [
        ["FALSE", "Guest A", "8/1/2026", "8/5/2026", "4:00:00 PM", "11:00:00 AM"],
        ["", "Do Not Use This Row - Leave This Row At Bottom"],
    ]
    idx = _find_sentinel_index(rows, "Do Not Use This Row - Leave This Row At Bottom")
    assert idx == 1


@pytest.mark.asyncio
async def test_handle_cleaner_sheet_uses_sentinel_from_config():
    """Handler passes prop.cleaner_schedule.sentinel_pattern to _find_sentinel_index.

    The mock sheet rows contain ONLY the custom sentinel, NOT the default
    '--- end ---'.  If the handler ignores the config and uses the module
    constant, _find_sentinel_index raises ValueError and task.state never
    reaches COMPLETE.
    """
    from app.db.models import TaskState

    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()

    custom_sentinel = "** end of schedule **"
    rows = [
        ["FALSE", "Guest A", "8/1/2026", "8/5/2026", "4:00:00 PM", "11:00:00 AM"],
        [custom_sentinel],
    ]
    mock_service = _make_mock_service(rows)
    config = _make_config(sentinel_pattern=custom_sentinel)

    with (
        patch("app.tasks.handlers.cleaner_sheet.get_sheets_service", return_value=mock_service),
        patch("app.tasks.handlers.cleaner_sheet.load_config", return_value=config),
    ):
        from app.tasks.handlers.cleaner_sheet import handle_cleaner_sheet
        await handle_cleaner_sheet(booking, task, session)

    assert task.state == TaskState.COMPLETE


@pytest.mark.asyncio
async def test_cleaner_sheet_mutations_serialize_on_module_lock():
    """F16 (bug hunt 2026-07-22): two bookings dispatched near-simultaneously
    both computed the same insertion index, then the second insert shifted the
    first's blank row so a fill landed on the wrong row. Sheet mutations must
    serialize on a module-level lock."""
    import asyncio
    import contextlib

    from app.tasks.handlers import cleaner_sheet

    lock = cleaner_sheet._get_sheet_lock()
    service_called = {"n": 0}

    def _service():
        service_called["n"] += 1
        return MagicMock()

    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()

    with (
        patch(
            "app.tasks.handlers.cleaner_sheet.get_sheets_service",
            side_effect=_service,
        ),
        patch("app.tasks.handlers.cleaner_sheet.load_config") as mock_cfg,
    ):
        prop = MagicMock(id="prop-1")  # matches _make_booking default
        mock_cfg.return_value.properties = [prop]

        async with lock:
            handler = asyncio.create_task(
                cleaner_sheet.handle_cleaner_sheet(booking, task, session)
            )
            await asyncio.sleep(0.05)
            # While the lock is held, the handler must not have touched Sheets.
            assert service_called["n"] == 0
            assert not handler.done()
        # Lock released -> the handler proceeds (and fails on the MagicMock
        # sheet data, which is fine: it got PAST the lock).
        with contextlib.suppress(Exception):
            await handler

    assert service_called["n"] == 1
