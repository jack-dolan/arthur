"""Tests for the daily-reminder threshold and idempotency logic (Plans 02 and 03).

Plan 02 (pure-function tests):
  No DB, no scheduler, no async — `_pending_reminders` is a sync helper that takes
  (booking, today_et, tasks_by_type) and returns the list of tasks to fire.

Plan 03 (async job tests):
  Tests for check_daily_reminders() and check_hoa_window() coroutines with
  AsyncSessionLocal and collaborators (send_reminder_alert, handle_hoa_email) mocked.
  Uses @pytest.mark.asyncio and AsyncMock throughout.

Covers ALERT-01 through ALERT-06 requirements and the D-01/D-04/D-05/D-06/D-07/D-08/D-13
decisions.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from app.db.models import (
    Booking,
    BookingStatus,
    BookingTask,
    Platform,
    TaskState,
    TaskType,
)

# Fixed reference date so check-in offsets are deterministic
TODAY_ET = date(2026, 8, 1)

# The 6 reminder task types (ALERT-01 through ALERT-03)
_REMINDER_TASK_TYPES = [
    TaskType.OWNER_ALERT_MISSING_PHONE_7D,
    TaskType.OWNER_ALERT_MISSING_PHONE_4D,
    TaskType.OWNER_ALERT_MISSING_EMAIL_7D,
    TaskType.OWNER_ALERT_MISSING_EMAIL_4D,
    TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D,
    TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D,
]


def _make_booking(
    check_in_offset_days: int,
    *,
    guest_phone: str | None = None,
    guest_email: str | None = None,
    signed_pdf_path: str | None = None,
    status: BookingStatus = BookingStatus.ACTIVE,
) -> Booking:
    """Create an in-memory Booking with the 6 reminder task rows.

    Initial task states mirror what _initial_tasks in app/ingestion/poller.py creates:
    - Phone tasks: PENDING when guest_phone is None, SKIPPED when phone is present
    - Email tasks: PENDING when guest_email is None, SKIPPED when email is present
    - DocuSign-unsigned tasks: always PENDING (form may or may not have been sent)
    - DOCUSIGN_SEND and HOA_EMAIL are included as supporting context
    """
    booking = Booking(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id="HMT0001",
        property_id="property_1",
        guest_first_name="Test",
        guest_last_name="Guest",
        guest_phone=guest_phone,
        guest_email=guest_email,
        check_in_date=TODAY_ET + timedelta(days=check_in_offset_days),
        check_out_date=TODAY_ET + timedelta(days=check_in_offset_days + 3),
        status=status,
        source_email_message_id="msg-test",
        signed_pdf_path=signed_pdf_path,
    )

    # Phone reminder tasks: PENDING when phone missing, SKIPPED when present
    phone_state = TaskState.PENDING if guest_phone is None else TaskState.SKIPPED
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.OWNER_ALERT_MISSING_PHONE_7D,
            state=phone_state,
        )
    )
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.OWNER_ALERT_MISSING_PHONE_4D,
            state=phone_state,
        )
    )

    # Email reminder tasks: PENDING when email missing, SKIPPED when present
    email_state = TaskState.PENDING if guest_email is None else TaskState.SKIPPED
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.OWNER_ALERT_MISSING_EMAIL_7D,
            state=email_state,
        )
    )
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.OWNER_ALERT_MISSING_EMAIL_4D,
            state=email_state,
        )
    )

    # DocuSign-unsigned tasks: always PENDING (regardless of whether form was sent)
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D,
            state=TaskState.PENDING,
        )
    )
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D,
            state=TaskState.PENDING,
        )
    )

    # Supporting task rows for context
    booking.tasks.append(
        BookingTask(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.WAITING)
    )
    booking.tasks.append(
        BookingTask(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING)
    )

    return booking


def _tasks_by_type(booking: Booking) -> dict[TaskType, BookingTask]:
    """Build a {TaskType: BookingTask} lookup from a booking's task list."""
    return {task.task_type: task for task in booking.tasks}


# ---------------------------------------------------------------------------
# Threshold boundary tests (ALERT-01, D-05)
# ---------------------------------------------------------------------------


def test_phone_7d_fires_at_exactly_7_days():
    """7d phone alert fires when days_until == 7 (D-05: <= threshold)."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(7, guest_phone=None)
    result = _pending_reminders(booking, TODAY_ET, _tasks_by_type(booking))

    task_types = {t.task_type for t, _ in result}
    assert TaskType.OWNER_ALERT_MISSING_PHONE_7D in task_types
    assert TaskType.OWNER_ALERT_MISSING_PHONE_4D not in task_types


def test_phone_not_yet_at_8_days():
    """Neither phone alert fires when days_until == 8 (above both thresholds)."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(8, guest_phone=None)
    result = _pending_reminders(booking, TODAY_ET, _tasks_by_type(booking))

    task_types = {t.task_type for t, _ in result}
    assert TaskType.OWNER_ALERT_MISSING_PHONE_7D not in task_types
    assert TaskType.OWNER_ALERT_MISSING_PHONE_4D not in task_types


def test_phone_4d_fires_at_exactly_4_days():
    """Both 7d AND 4d phone alerts fire when days_until == 4 (within both thresholds)."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(4, guest_phone=None)
    result = _pending_reminders(booking, TODAY_ET, _tasks_by_type(booking))

    task_types = {t.task_type for t, _ in result}
    # F16: the 7d sibling is suppressed when the 4d fires the same morning.
    assert TaskType.OWNER_ALERT_MISSING_PHONE_7D not in task_types
    assert TaskType.OWNER_ALERT_MISSING_PHONE_4D in task_types


# ---------------------------------------------------------------------------
# Idempotency tests (ALERT-04, D-06)
# ---------------------------------------------------------------------------


def test_phone_idempotent_skips_complete():
    """7d task already COMPLETE is skipped; only 4d fires when within that threshold."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(4, guest_phone=None)
    # Override: 7d task is already complete
    tasks = _tasks_by_type(booking)
    tasks[TaskType.OWNER_ALERT_MISSING_PHONE_7D].state = TaskState.COMPLETE

    result = _pending_reminders(booking, TODAY_ET, tasks)
    task_types = {t.task_type for t, _ in result}

    assert TaskType.OWNER_ALERT_MISSING_PHONE_7D not in task_types
    assert TaskType.OWNER_ALERT_MISSING_PHONE_4D in task_types


def test_phone_skipped_state_never_fires():
    """Phone tasks in SKIPPED state never fire (phone was present at booking creation)."""
    from app.tasks.scheduled import _pending_reminders

    # guest_phone=None but tasks already SKIPPED (simulating creation-time skip)
    # We set guest_phone to None after factory sets tasks to SKIPPED to test state filter
    booking = _make_booking(7, guest_phone="555-0100")  # creates SKIPPED tasks
    # Now pretend field is gone — but state is still SKIPPED
    booking.guest_phone = None

    result = _pending_reminders(booking, TODAY_ET, _tasks_by_type(booking))
    task_types = {t.task_type for t, _ in result}

    # SKIPPED tasks are never processed regardless of field state (D-06)
    assert TaskType.OWNER_ALERT_MISSING_PHONE_7D not in task_types
    assert TaskType.OWNER_ALERT_MISSING_PHONE_4D not in task_types


# ---------------------------------------------------------------------------
# ALERT-06: phone and email independence
# ---------------------------------------------------------------------------


def test_email_independent_of_phone():
    """Email alert fires when email is missing, even if phone is present (ALERT-06)."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(7, guest_phone="555-0100", guest_email=None)
    result = _pending_reminders(booking, TODAY_ET, _tasks_by_type(booking))

    task_types = {t.task_type for t, _ in result}
    # Phone tasks are SKIPPED (created that way); email task should fire
    assert TaskType.OWNER_ALERT_MISSING_EMAIL_7D in task_types
    assert TaskType.OWNER_ALERT_MISSING_PHONE_7D not in task_types
    assert TaskType.OWNER_ALERT_MISSING_PHONE_4D not in task_types


def test_email_4d_fires_when_email_missing():
    """At 4 days out the urgent 4d email alert fires; the 7d sibling is
    suppressed the same morning (F16 — one email per condition, not two)."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(4, guest_email=None)
    result = _pending_reminders(booking, TODAY_ET, _tasks_by_type(booking))

    task_types = {t.task_type for t, _ in result}
    assert TaskType.OWNER_ALERT_MISSING_EMAIL_4D in task_types
    assert TaskType.OWNER_ALERT_MISSING_EMAIL_7D not in task_types


# ---------------------------------------------------------------------------
# DocuSign unsigned tests (ALERT-03, D-12, Pattern 3)
# ---------------------------------------------------------------------------


def test_unsigned_fires_when_signed_pdf_path_is_none():
    """DocuSign-unsigned alerts fire when signed_pdf_path is None (D-12)."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(7, signed_pdf_path=None)
    result = _pending_reminders(booking, TODAY_ET, _tasks_by_type(booking))

    task_types = {t.task_type for t, _ in result}
    assert TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D in task_types


def test_unsigned_fires_only_4d_at_4_days():
    """At 4 days out only the urgent 4d docusign-unsigned alert fires; the 7d
    sibling is suppressed the same morning (F16)."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(4, signed_pdf_path=None)
    result = _pending_reminders(booking, TODAY_ET, _tasks_by_type(booking))

    task_types = {t.task_type for t, _ in result}
    assert TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D in task_types
    assert TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D not in task_types


def test_unsigned_does_not_fire_when_signed_pdf_path_set():
    """DocuSign-unsigned alerts do NOT fire when signed_pdf_path is set."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(4, signed_pdf_path="/app/data/pdfs/signed.pdf")
    result = _pending_reminders(booking, TODAY_ET, _tasks_by_type(booking))

    task_types = {t.task_type for t, _ in result}
    assert TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D not in task_types
    assert TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D not in task_types


# ---------------------------------------------------------------------------
# _is_docusign_unsigned pure function test (Pattern 3)
# ---------------------------------------------------------------------------


def test_is_docusign_unsigned_pure_function():
    """_is_docusign_unsigned returns True only when signed_pdf_path is None."""
    from app.tasks.scheduled import _is_docusign_unsigned

    booking_unsigned = _make_booking(7, signed_pdf_path=None)
    booking_signed = _make_booking(7, signed_pdf_path="/app/data/pdfs/form.pdf")

    assert _is_docusign_unsigned(booking_unsigned) is True
    assert _is_docusign_unsigned(booking_signed) is False


# ---------------------------------------------------------------------------
# Runtime field re-check test (RESEARCH.md Anti-pattern 5, D-05)
# ---------------------------------------------------------------------------


def test_field_filled_during_window_does_not_fire():
    """Phone tasks are PENDING but guest_phone has since been set — no alert fires.

    Validates that _pending_reminders re-checks field presence at runtime (D-05),
    not just task state. A PENDING task where the field is now filled must be skipped.
    """
    from app.tasks.scheduled import _pending_reminders

    # Start with phone absent (tasks created as PENDING)
    booking = _make_booking(7, guest_phone=None)
    # Field is now filled (entered via dashboard between booking creation and today)
    booking.guest_phone = "555-9999"

    result = _pending_reminders(booking, TODAY_ET, _tasks_by_type(booking))
    task_types = {t.task_type for t, _ in result}

    # Despite tasks being PENDING, no phone alert fires because field is now set
    assert TaskType.OWNER_ALERT_MISSING_PHONE_7D not in task_types
    assert TaskType.OWNER_ALERT_MISSING_PHONE_4D not in task_types


# ---------------------------------------------------------------------------
# _REMINDER_MATRIX structure test
# ---------------------------------------------------------------------------


def test_reminder_matrix_has_six_entries():
    """_REMINDER_MATRIX has exactly 6 entries covering all 6 reminder TaskType variants."""
    from app.tasks.scheduled import _REMINDER_MATRIX

    assert len(_REMINDER_MATRIX) == 6

    expected_pairs = {
        (TaskType.OWNER_ALERT_MISSING_PHONE_7D, 7),
        (TaskType.OWNER_ALERT_MISSING_PHONE_4D, 4),
        (TaskType.OWNER_ALERT_MISSING_EMAIL_7D, 7),
        (TaskType.OWNER_ALERT_MISSING_EMAIL_4D, 4),
        (TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D, 7),
        (TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D, 4),
    }
    actual_pairs = {(task_type, threshold) for task_type, threshold, _ in _REMINDER_MATRIX}
    assert actual_pairs == expected_pairs


# ===========================================================================
# Plan 03 — Async job tests (check_daily_reminders, check_hoa_window)
# D-01/D-04/D-05/D-06/D-07/D-08/D-13
# ===========================================================================

import pytest
from contextlib import asynccontextmanager
from datetime import datetime as _real_datetime
from unittest.mock import AsyncMock, MagicMock, patch, call
from zoneinfo import ZoneInfo

# Fixed Eastern date used for async job tests — must be consistent with check_in_dates
# on test bookings.  check_daily_reminders computes today_et = datetime.now(ET).date();
# we patch datetime.now to return this value so _pending_reminders sees the right offsets.
FIXED_EASTERN_DT = _real_datetime(2026, 8, 1, 9, 0, 0, tzinfo=ZoneInfo("America/New_York"))
FIXED_EASTERN_DATE = FIXED_EASTERN_DT.date()  # 2026-08-01


class _MockDatetime:
    """Minimal datetime stand-in: only now() is needed by check_daily_reminders."""

    @staticmethod
    def now(tz=None):
        return FIXED_EASTERN_DT

    @classmethod
    def __instancecheck__(cls, instance):
        return isinstance(instance, _real_datetime)


def _make_async_booking(check_in_offset_days, **kwargs):
    """Create a booking with check_in_date relative to FIXED_EASTERN_DATE.

    Wraps _make_booking but overrides check_in_date/check_out_date so the
    async coroutines (which patch datetime to FIXED_EASTERN_DATE) see the
    correct offsets when computing days_until.
    """
    booking = _make_booking(
        # offset from the module-level TODAY_ET = 2026-08-01, same as FIXED_EASTERN_DATE
        check_in_offset_days,
        **kwargs,
    )
    # Both dates are already relative to TODAY_ET (= FIXED_EASTERN_DATE), so no adjustment needed.
    return booking


def _make_async_session_local(bookings_list):
    """Build a mock AsyncSessionLocal context manager that returns bookings_list from
    session.execute(...).scalars().all() and supports commit(), add(), and scalars().unique().all().

    Returns (mock_session_local, mock_session) so tests can inspect mock_session.execute.
    """
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = bookings_list
    # Also support .unique().all() path for HOA query
    mock_result.scalars.return_value.unique.return_value.all.return_value = bookings_list

    mock_session = AsyncMock()
    mock_session.execute.return_value = mock_result
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    @asynccontextmanager
    async def _mock_session_local():
        yield mock_session

    return _mock_session_local, mock_session


def _make_hoa_session_local(bookings_list):
    """Mock AsyncSessionLocal for check_hoa_window's two-phase query.

    check_hoa_window first scans for candidate ids (``select(Booking.id)`` →
    ``result.all()``), then fetches + processes each booking in its OWN session
    (``result.scalar_one_or_none()``). The same mock_session is yielded for every
    ``AsyncSessionLocal()`` call; ``execute`` returns the scan result first, then
    one per-booking result in order.
    """
    scan_result = MagicMock()
    scan_result.all.return_value = [(b.id,) for b in bookings_list]

    per_booking_results = []
    for b in bookings_list:
        r = MagicMock()
        r.scalar_one_or_none.return_value = b
        per_booking_results.append(r)

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=[scan_result, *per_booking_results])
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()

    @asynccontextmanager
    async def _mock_session_local():
        yield mock_session

    return _mock_session_local, mock_session


# ---------------------------------------------------------------------------
# check_daily_reminders — send + state-transition wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_daily_reminders_calls_send_for_each_pending_match():
    """With one ACTIVE booking 7 days out (relative to fixed Eastern date), guest_phone=None
    and guest_email present, phone_7d and docusign_unsigned_7d tasks fire (2 sends).

    Patches datetime.now to return FIXED_EASTERN_DT so _pending_reminders sees the
    correct offsets (D-04, ALERT-01, D-06).
    """
    from app.tasks.scheduled import check_daily_reminders

    # Email present so email tasks are SKIPPED; phone missing so phone tasks PENDING
    # DocuSign tasks PENDING (signed_pdf_path=None)
    # 7 days from FIXED_EASTERN_DATE: only 7d tasks fire (not 4d)
    booking = _make_async_booking(7, guest_phone=None, guest_email="g@example.com", signed_pdf_path=None)

    mock_session_local, mock_session = _make_async_session_local([booking])

    with (
        patch("app.tasks.scheduled.datetime", _MockDatetime),
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.send_reminder_alert") as mock_send,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
        patch("app.tasks.scheduled.load_config") as mock_cfg,
        patch("app.tasks.scheduled.settings") as mock_settings,
    ):
        mock_settings.domain = "example.com"
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        await check_daily_reminders()

    # phone_7d PENDING (phone missing, 7 days out, within 7d threshold)
    # docusign_unsigned_7d PENDING (signed_pdf_path None, 7 days out)
    # => 2 sends
    assert mock_send.call_count == 2


@pytest.mark.asyncio
async def test_check_daily_reminders_excludes_cancelled_bookings():
    """The SQLAlchemy query filters on Booking.status == BookingStatus.ACTIVE (ALERT-05).

    Verifies by inspecting the compiled query string.
    """
    from app.tasks.scheduled import check_daily_reminders

    mock_session_local, mock_session = _make_async_session_local([])

    with (
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.send_reminder_alert"),
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
        patch("app.tasks.scheduled.load_config"),
        patch("app.tasks.scheduled.settings") as mock_settings,
    ):
        mock_settings.domain = "example.com"
        await check_daily_reminders()

    assert mock_session.execute.called, "session.execute was not called"
    stmt = mock_session.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "status" in compiled, f"Expected 'status' in compiled query; got:\n{compiled}"
    assert "active" in compiled, f"Expected 'active' in compiled query; got:\n{compiled}"


@pytest.mark.asyncio
async def test_check_daily_reminders_excludes_past_checkin():
    """The SQLAlchemy query filters check_in_date > today_et (RESEARCH.md Pitfall 4).

    Verifies by inspecting the compiled query string.
    """
    from app.tasks.scheduled import check_daily_reminders

    mock_session_local, mock_session = _make_async_session_local([])

    with (
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.send_reminder_alert"),
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
        patch("app.tasks.scheduled.load_config"),
        patch("app.tasks.scheduled.settings") as mock_settings,
    ):
        mock_settings.domain = "example.com"
        await check_daily_reminders()

    assert mock_session.execute.called, "session.execute was not called"
    stmt = mock_session.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "check_in_date" in compiled, (
        f"Expected 'check_in_date' in compiled query; got:\n{compiled}"
    )
    # SQLAlchemy renders > as > in the string
    assert ">" in compiled, f"Expected '>' comparison in compiled query; got:\n{compiled}"


@pytest.mark.asyncio
async def test_check_daily_reminders_flips_task_to_complete_after_send():
    """After a successful send, task.state is set to COMPLETE and task.completed_at is set (D-06).

    Patches datetime.now to FIXED_EASTERN_DT so booking 7 days out triggers the 7d reminder.
    """
    from app.tasks.scheduled import check_daily_reminders

    # phone only missing, 7 days out; signed PDF present so docusign task won't fire
    booking = _make_async_booking(7, guest_phone=None, guest_email="g@example.com", signed_pdf_path="/path/pdf")

    mock_session_local, mock_session = _make_async_session_local([booking])

    with (
        patch("app.tasks.scheduled.datetime", _MockDatetime),
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.send_reminder_alert"),  # no-op
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
        patch("app.tasks.scheduled.load_config") as mock_cfg,
        patch("app.tasks.scheduled.settings") as mock_settings,
    ):
        mock_settings.domain = "example.com"
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        await check_daily_reminders()

    # Find the phone_7d task
    phone_7d_task = next(
        t for t in booking.tasks if t.task_type == TaskType.OWNER_ALERT_MISSING_PHONE_7D
    )
    assert phone_7d_task.state == TaskState.COMPLETE, (
        f"Expected phone_7d task to be COMPLETE, got {phone_7d_task.state}"
    )
    assert phone_7d_task.completed_at is not None, "Expected completed_at to be set"


@pytest.mark.asyncio
async def test_check_daily_reminders_isolates_per_booking_failures(caplog):
    """One booking failing to send does not abort processing of other bookings (Pattern 4, T-05-09).

    The second booking's task must still be flipped to COMPLETE.
    No exception escapes check_daily_reminders().
    Patches datetime.now to FIXED_EASTERN_DT so bookings 7 days out trigger 7d reminders.
    """
    import logging
    from app.tasks.scheduled import check_daily_reminders

    # Both bookings: phone missing, 7 days from fixed Eastern date, signed PDF present
    booking1 = _make_async_booking(7, guest_phone=None, guest_email="g@example.com", signed_pdf_path="/pdf")
    booking2 = _make_async_booking(7, guest_phone=None, guest_email="g@example.com", signed_pdf_path="/pdf")

    mock_session_local, mock_session = _make_async_session_local([booking1, booking2])

    call_count = {"n": 0}

    def _raise_on_first(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated send failure")

    with (
        patch("app.tasks.scheduled.datetime", _MockDatetime),
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.send_reminder_alert", side_effect=_raise_on_first),
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
        patch("app.tasks.scheduled.load_config") as mock_cfg,
        patch("app.tasks.scheduled.settings") as mock_settings,
        caplog.at_level(logging.ERROR, logger="app.tasks.scheduled"),
    ):
        mock_settings.domain = "example.com"
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        # Must not raise
        await check_daily_reminders()

    # At least 2 send attempts total (one fails, one succeeds)
    assert call_count["n"] >= 2, f"Expected >= 2 send attempts, got {call_count['n']}"

    # Second booking's task should be COMPLETE (isolation worked)
    phone_7d_task_b2 = next(
        t for t in booking2.tasks if t.task_type == TaskType.OWNER_ALERT_MISSING_PHONE_7D
    )
    assert phone_7d_task_b2.state == TaskState.COMPLETE, (
        f"Second booking phone_7d task should be COMPLETE, got {phone_7d_task_b2.state}"
    )

    # Exception was logged (now caught per-task inside _process_booking_reminders)
    assert any(
        "_process_booking_reminders" in r.message or "check_daily_reminders" in r.message
        for r in caplog.records
    ), "Expected log.exception call for the failed booking"


@pytest.mark.asyncio
async def test_check_daily_reminders_uses_eastern_date():
    """check_daily_reminders uses datetime.now(ZoneInfo("America/New_York")) for today_et (D-04).

    Monkeypatches datetime via _MockDatetime so we control what date is used.
    A booking 7 days from FIXED_EASTERN_DATE must trigger the 7d reminder.
    """
    from app.tasks.scheduled import check_daily_reminders

    # Booking 7 days out from FIXED_EASTERN_DATE (= TODAY_ET in _make_booking)
    booking = _make_async_booking(7, guest_phone=None, guest_email="g@example.com", signed_pdf_path="/pdf")

    mock_session_local, mock_session = _make_async_session_local([booking])

    with (
        patch("app.tasks.scheduled.datetime", _MockDatetime),
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.send_reminder_alert") as mock_send,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
        patch("app.tasks.scheduled.load_config") as mock_cfg,
        patch("app.tasks.scheduled.settings") as mock_settings,
    ):
        mock_settings.domain = "example.com"
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        await check_daily_reminders()

    # phone_7d fires because booking is 7 days from the fixed Eastern date
    assert mock_send.call_count >= 1, "Expected send_reminder_alert to be called"


# ---------------------------------------------------------------------------
# check_hoa_window — D-13 delegation pattern
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_hoa_window_calls_handle_hoa_email_when_window_open():
    """With one ACTIVE booking with signed_pdf_path set and HOA_EMAIL task in WAITING,
    check_hoa_window delegates to handle_hoa_email with (booking, task, session) args
    and commits the session (D-13).
    """
    from app.tasks.scheduled import check_hoa_window

    booking = _make_booking(10, signed_pdf_path="/app/data/pdfs/x.pdf")
    # Ensure HOA_EMAIL task is in WAITING
    hoa_task = next(t for t in booking.tasks if t.task_type == TaskType.HOA_EMAIL)
    hoa_task.state = TaskState.WAITING

    mock_session_local, mock_session = _make_hoa_session_local([booking])

    mock_handle = AsyncMock()

    with (
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.handle_hoa_email", mock_handle),
    ):
        await check_hoa_window()

    mock_handle.assert_awaited_once()
    call_args = mock_handle.call_args
    assert call_args[0][0] is booking, "First arg must be the booking"
    assert call_args[0][1] is hoa_task, "Second arg must be the HOA task"
    assert call_args[0][2] is mock_session, "Third arg must be the session"
    mock_session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_check_hoa_window_skips_when_signed_pdf_path_none():
    """The HOA query filters on signed_pdf_path IS NOT NULL.

    Verifies by inspecting the compiled query string.
    """
    from app.tasks.scheduled import check_hoa_window

    mock_session_local, mock_session = _make_async_session_local([])

    with (
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.handle_hoa_email", AsyncMock()),
    ):
        await check_hoa_window()

    assert mock_session.execute.called, "session.execute was not called"
    stmt = mock_session.execute.call_args[0][0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "signed_pdf_path" in compiled, (
        f"Expected 'signed_pdf_path' in compiled query; got:\n{compiled}"
    )
    assert "IS NOT NULL" in compiled, (
        f"Expected 'IS NOT NULL' in compiled query for signed_pdf_path; got:\n{compiled}"
    )


@pytest.mark.asyncio
async def test_check_hoa_window_isolates_per_booking_failures(caplog):
    """handle_hoa_email raising on first booking does not abort processing second booking (Pattern 4).

    No exception escapes check_hoa_window().
    """
    import logging
    from app.tasks.scheduled import check_hoa_window

    booking1 = _make_booking(10, signed_pdf_path="/app/data/pdfs/x.pdf")
    booking2 = _make_booking(15, signed_pdf_path="/app/data/pdfs/y.pdf")
    for b in (booking1, booking2):
        hoa = next(t for t in b.tasks if t.task_type == TaskType.HOA_EMAIL)
        hoa.state = TaskState.WAITING

    call_count = {"n": 0}

    async def _raise_on_first(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated HOA failure")

    mock_session_local, mock_session = _make_hoa_session_local([booking1, booking2])

    with (
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.handle_hoa_email", side_effect=_raise_on_first),
        caplog.at_level(logging.ERROR, logger="app.tasks.scheduled"),
    ):
        # Must not raise
        await check_hoa_window()

    assert call_count["n"] == 2, f"Expected 2 handle_hoa_email calls, got {call_count['n']}"
    assert any("check_hoa_window" in r.message for r in caplog.records), (
        "Expected log.exception call for the failed booking"
    )


# ---------------------------------------------------------------------------
# _stale_reminders — resolved-condition PENDING reminders flip to SKIPPED
# (dashboard truthfulness fix: a reminder whose condition resolved must not
#  read as outstanding work forever)
# ---------------------------------------------------------------------------


def test_stale_reminders_flags_resolved_email_reminders():
    """Email entered after booking creation → both email reminders are stale."""
    from app.tasks.scheduled import _stale_reminders

    booking = _make_booking(10)  # phone+email missing → all reminders PENDING
    booking.guest_email = "g@example.com"  # resolved via dashboard afterwards

    stale = _stale_reminders(booking, _tasks_by_type(booking))
    assert {t.task_type for t in stale} == {
        TaskType.OWNER_ALERT_MISSING_EMAIL_7D,
        TaskType.OWNER_ALERT_MISSING_EMAIL_4D,
    }


def test_stale_reminders_flags_signed_docusign_reminders():
    """Signed PDF on file → both unsigned-DocuSign reminders are stale."""
    from app.tasks.scheduled import _stale_reminders

    booking = _make_booking(10, guest_phone="5551234567", guest_email="g@example.com")
    booking.signed_pdf_path = "/app/data/pdfs/x.pdf"

    stale = _stale_reminders(booking, _tasks_by_type(booking))
    assert {t.task_type for t in stale} == {
        TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D,
        TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D,
    }


def test_stale_reminders_empty_when_conditions_still_present():
    """Nothing resolved → nothing stale (reminders remain legitimately pending)."""
    from app.tasks.scheduled import _stale_reminders

    booking = _make_booking(10)  # phone, email missing; unsigned
    assert _stale_reminders(booking, _tasks_by_type(booking)) == []


def test_stale_reminders_ignores_non_pending_states():
    """COMPLETE (alert really fired) and SKIPPED rows are never re-flagged."""
    from app.tasks.scheduled import _stale_reminders

    booking = _make_booking(10)
    booking.guest_email = "g@example.com"
    tasks = _tasks_by_type(booking)
    tasks[TaskType.OWNER_ALERT_MISSING_EMAIL_7D].state = TaskState.COMPLETE

    stale = _stale_reminders(booking, tasks)
    assert {t.task_type for t in stale} == {TaskType.OWNER_ALERT_MISSING_EMAIL_4D}


@pytest.mark.asyncio
async def test_daily_job_flips_resolved_reminders_to_skipped():
    """check_daily_reminders marks resolved-condition PENDING reminders SKIPPED,
    even when the booking is far outside the alert threshold window."""
    from app.tasks.scheduled import check_daily_reminders

    # 20 days out: no reminder can fire (threshold is 7); email + signature
    # resolved after creation, phone still genuinely missing.
    booking = _make_async_booking(20, guest_phone=None, guest_email=None)
    booking.guest_email = "g@example.com"
    booking.signed_pdf_path = "/app/data/pdfs/x.pdf"

    mock_session_local, _ = _make_async_session_local([booking])

    with (
        patch("app.tasks.scheduled.datetime", _MockDatetime),
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.send_reminder_alert") as mock_send,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
        patch("app.tasks.scheduled.load_config") as mock_cfg,
        patch("app.tasks.scheduled.settings") as mock_settings,
    ):
        mock_settings.domain = "example.com"
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        await check_daily_reminders()

    states = {t.task_type: t.state for t in booking.tasks}
    assert states[TaskType.OWNER_ALERT_MISSING_EMAIL_7D] == TaskState.SKIPPED
    assert states[TaskType.OWNER_ALERT_MISSING_EMAIL_4D] == TaskState.SKIPPED
    assert states[TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D] == TaskState.SKIPPED
    assert states[TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D] == TaskState.SKIPPED
    # Phone is still missing — those reminders stay PENDING (they may yet fire).
    assert states[TaskType.OWNER_ALERT_MISSING_PHONE_7D] == TaskState.PENDING
    assert states[TaskType.OWNER_ALERT_MISSING_PHONE_4D] == TaskState.PENDING
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_daily_job_sweeps_moot_reminders_bulk():
    """check_daily_reminders issues a bulk UPDATE flipping PENDING reminders to
    SKIPPED for past-check-in or cancelled bookings (they can never fire)."""
    from app.tasks.scheduled import check_daily_reminders

    mock_session_local, mock_session = _make_async_session_local([])

    with (
        patch("app.tasks.scheduled.datetime", _MockDatetime),
        patch("app.tasks.scheduled.AsyncSessionLocal", mock_session_local),
        patch("app.tasks.scheduled.send_reminder_alert"),
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
        patch("app.tasks.scheduled.load_config"),
        patch("app.tasks.scheduled.settings") as mock_settings,
    ):
        mock_settings.domain = "example.com"
        await check_daily_reminders()

    # The sweep runs first (its own session), then the reminder scan.
    sweep_stmt = mock_session.execute.call_args_list[0][0][0]
    compiled = str(sweep_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "UPDATE booking_tasks" in compiled, compiled
    assert "skipped" in compiled, compiled
    assert "cancelled" in compiled, compiled
    assert "check_in_date" in compiled, compiled


def test_pending_reminders_drops_7d_when_4d_also_fires_same_day():
    """F16 (bug hunt 2026-07-22): a booking created <=4 days out qualified for
    BOTH the 7d and 4d thresholds the same morning — up to 4 near-duplicate
    reminder emails at 8 AM. Only the more urgent sibling should fire."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(3, guest_phone=None, guest_email=None)
    tasks = {t.task_type: t for t in booking.tasks}

    result = _pending_reminders(booking, TODAY_ET, tasks)
    fired = {t.task_type for t, _ in result}

    assert TaskType.OWNER_ALERT_MISSING_PHONE_4D in fired
    assert TaskType.OWNER_ALERT_MISSING_EMAIL_4D in fired
    assert TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D in fired
    # The 7d siblings must NOT double-fire the same morning.
    assert TaskType.OWNER_ALERT_MISSING_PHONE_7D not in fired
    assert TaskType.OWNER_ALERT_MISSING_EMAIL_7D not in fired
    assert TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D not in fired


def test_pending_reminders_7d_still_fires_alone_at_7_days():
    """At exactly 7 days out only the 7d thresholds fire (regression guard for
    the sibling-suppression rule)."""
    from app.tasks.scheduled import _pending_reminders

    booking = _make_booking(7, guest_phone=None, guest_email=None)
    tasks = {t.task_type: t for t in booking.tasks}

    result = _pending_reminders(booking, TODAY_ET, tasks)
    fired = {t.task_type for t, _ in result}

    assert TaskType.OWNER_ALERT_MISSING_PHONE_7D in fired
    assert TaskType.OWNER_ALERT_MISSING_PHONE_4D not in fired
