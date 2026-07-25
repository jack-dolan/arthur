"""Unit tests for the dashboard router (phase 3).

Tests are RED (failing) until app/routers/dashboard.py is implemented in plan 02.
Each test defers its production-code import inside the function body per the
project's established pattern (tests/unit/test_cancellation.py).

asyncio_mode = "auto" is set in pyproject.toml — no @pytest.mark.asyncio needed.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import (
    Booking,
    BookingStatus,
    BookingTask,
    DataPoint,
    DataPointSource,
    Platform,
    TaskState,
    TaskType,
)


def _make_booking(
    *,
    guest_phone: str | None = None,
    guest_email: str | None = None,
) -> Booking:
    """Create an in-memory Booking for unit tests — no DB required."""
    booking = Booking(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id="HMTEST001",
        property_id="property_1",
        guest_first_name="Test",
        guest_last_name="Guest",
        guest_phone=guest_phone,
        guest_email=guest_email,
        check_in_date=date(2026, 8, 1),
        check_out_date=date(2026, 8, 5),
        status=BookingStatus.ACTIVE,
        source_email_message_id="msg-1",
    )
    return booking


def _async_ctx(obj):
    """Return an async context manager that yields *obj*."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=obj)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


# Auth moved to app/routers/auth.py (Google OIDC). See tests/unit/test_auth.py.


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def test_validate_phone_strips_and_passes():
    from app.routers.dashboard import _validate_phone
    assert _validate_phone("(555) 867-5309") == "5558675309"


def test_validate_phone_rejects_short():
    from app.routers.dashboard import _validate_phone
    assert _validate_phone("123") is None


def test_validate_email_passes_valid():
    from app.routers.dashboard import _validate_email
    assert _validate_email("guest@example.com") == "guest@example.com"


def test_validate_email_rejects_no_at():
    from app.routers.dashboard import _validate_email
    assert _validate_email("notanemail") is None


def test_empty_string_coerced_to_none():
    from app.routers.dashboard import _validate_email, _validate_phone
    assert _validate_phone("") is None
    assert _validate_email("") is None


# ---------------------------------------------------------------------------
# Task flip helper
# ---------------------------------------------------------------------------

def test_flip_tasks_email_unblocks_docusign():
    from app.routers.dashboard import _flip_waiting_tasks

    booking = _make_booking()
    booking.tasks = [
        BookingTask(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.WAITING),
        BookingTask(task_type=TaskType.ACCESS_CODE_CREATE, state=TaskState.WAITING),
    ]

    _flip_waiting_tasks(booking, email_saved=True, phone_saved=False)

    states = {t.task_type: t.state for t in booking.tasks}
    assert states[TaskType.DOCUSIGN_SEND] == TaskState.PENDING
    assert states[TaskType.ACCESS_CODE_CREATE] == TaskState.WAITING


def test_flip_tasks_phone_unblocks_access_code():
    from app.routers.dashboard import _flip_waiting_tasks

    booking = _make_booking()
    booking.tasks = [
        BookingTask(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.WAITING),
        BookingTask(task_type=TaskType.ACCESS_CODE_CREATE, state=TaskState.WAITING),
    ]

    _flip_waiting_tasks(booking, email_saved=False, phone_saved=True)

    states = {t.task_type: t.state for t in booking.tasks}
    assert states[TaskType.ACCESS_CODE_CREATE] == TaskState.PENDING
    assert states[TaskType.DOCUSIGN_SEND] == TaskState.WAITING


def test_flip_tasks_does_not_touch_pending():
    from app.routers.dashboard import _flip_waiting_tasks

    booking = _make_booking()
    booking.tasks = [
        BookingTask(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.PENDING),
        BookingTask(task_type=TaskType.ACCESS_CODE_CREATE, state=TaskState.PENDING),
    ]

    _flip_waiting_tasks(booking, email_saved=True, phone_saved=True)

    states = {t.task_type: t.state for t in booking.tasks}
    assert states[TaskType.DOCUSIGN_SEND] == TaskState.PENDING
    assert states[TaskType.ACCESS_CODE_CREATE] == TaskState.PENDING


def test_flip_tasks_email_saved_skips_missing_email_reminders():
    """Entering the email makes the missing-email reminders moot — they must
    flip PENDING→SKIPPED so the dashboard stops showing them as outstanding."""
    from app.routers.dashboard import _flip_waiting_tasks

    booking = _make_booking()
    booking.tasks = [
        BookingTask(task_type=TaskType.OWNER_ALERT_MISSING_EMAIL_7D, state=TaskState.PENDING),
        BookingTask(task_type=TaskType.OWNER_ALERT_MISSING_EMAIL_4D, state=TaskState.PENDING),
        BookingTask(task_type=TaskType.OWNER_ALERT_MISSING_PHONE_7D, state=TaskState.PENDING),
        BookingTask(task_type=TaskType.OWNER_ALERT_MISSING_PHONE_4D, state=TaskState.PENDING),
    ]

    _flip_waiting_tasks(booking, email_saved=True, phone_saved=False)

    states = {t.task_type: t.state for t in booking.tasks}
    assert states[TaskType.OWNER_ALERT_MISSING_EMAIL_7D] == TaskState.SKIPPED
    assert states[TaskType.OWNER_ALERT_MISSING_EMAIL_4D] == TaskState.SKIPPED
    assert states[TaskType.OWNER_ALERT_MISSING_PHONE_7D] == TaskState.PENDING
    assert states[TaskType.OWNER_ALERT_MISSING_PHONE_4D] == TaskState.PENDING


def test_flip_tasks_phone_saved_skips_missing_phone_reminders():
    from app.routers.dashboard import _flip_waiting_tasks

    booking = _make_booking()
    booking.tasks = [
        BookingTask(task_type=TaskType.OWNER_ALERT_MISSING_PHONE_7D, state=TaskState.PENDING),
        BookingTask(task_type=TaskType.OWNER_ALERT_MISSING_PHONE_4D, state=TaskState.PENDING),
        BookingTask(task_type=TaskType.OWNER_ALERT_MISSING_EMAIL_7D, state=TaskState.PENDING),
    ]

    _flip_waiting_tasks(booking, email_saved=False, phone_saved=True)

    states = {t.task_type: t.state for t in booking.tasks}
    assert states[TaskType.OWNER_ALERT_MISSING_PHONE_7D] == TaskState.SKIPPED
    assert states[TaskType.OWNER_ALERT_MISSING_PHONE_4D] == TaskState.SKIPPED
    assert states[TaskType.OWNER_ALERT_MISSING_EMAIL_7D] == TaskState.PENDING


def test_flip_tasks_never_touches_fired_reminders():
    """A reminder that actually fired (COMPLETE) is history — never rewritten."""
    from app.routers.dashboard import _flip_waiting_tasks

    booking = _make_booking()
    booking.tasks = [
        BookingTask(task_type=TaskType.OWNER_ALERT_MISSING_EMAIL_7D, state=TaskState.COMPLETE),
        BookingTask(task_type=TaskType.OWNER_ALERT_MISSING_EMAIL_4D, state=TaskState.PENDING),
    ]

    _flip_waiting_tasks(booking, email_saved=True, phone_saved=False)

    states = {t.task_type: t.state for t in booking.tasks}
    assert states[TaskType.OWNER_ALERT_MISSING_EMAIL_7D] == TaskState.COMPLETE
    assert states[TaskType.OWNER_ALERT_MISSING_EMAIL_4D] == TaskState.SKIPPED


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def test_dispatch_called_after_commit():
    from fastapi import Request

    from app.routers.dashboard import save_contact

    booking = _make_booking(guest_email=None, guest_phone=None)

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    mock_request = MagicMock(spec=Request)
    mock_request.method = "POST"
    mock_request.url = MagicMock()

    with (
        patch("app.routers.dashboard._dispatch_pending_tasks") as mock_dispatch,
        patch("app.routers.dashboard.asyncio.create_task") as mock_create_task,
    ):
        mock_dispatch.return_value = AsyncMock()
        await save_contact(
            booking_id=booking.id,
            request=mock_request,
            guest_phone="5551234567",
            guest_email=None,
            db=mock_session,
        )

        mock_dispatch.assert_called_once()
        call_args = mock_dispatch.call_args
        assert call_args[0][0] == booking.id
        mock_create_task.assert_called_once()


# ---------------------------------------------------------------------------
# Provenance map
# ---------------------------------------------------------------------------

def test_provenance_map_returns_most_recent():
    from app.routers.dashboard import PROVENANCE_FIELDS, _build_provenance_map

    booking = _make_booking()
    t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
    t3 = datetime(2026, 1, 3, 0, 0, 0, tzinfo=timezone.utc)

    dp1 = DataPoint(
        booking_id=booking.id,
        field_name="guest_email",
        value="old@example.com",
        source=DataPointSource.EMAIL_PARSE,
        recorded_at=t1,
    )
    dp2 = DataPoint(
        booking_id=booking.id,
        field_name="guest_email",
        value="newer@example.com",
        source=DataPointSource.EMAIL_PARSE,
        recorded_at=t2,
    )
    dp3 = DataPoint(
        booking_id=booking.id,
        field_name="guest_email",
        value="newest@example.com",
        source=DataPointSource.MANUAL_ENTRY,
        recorded_at=t3,
    )
    booking.data_points = [dp1, dp2, dp3]

    provenance = _build_provenance_map(booking)

    assert provenance["guest_email"] is dp3
    for field in PROVENANCE_FIELDS:
        assert field in provenance


# ---------------------------------------------------------------------------
# Task group assignment
# ---------------------------------------------------------------------------

def test_task_group_assignment_exhaustive():
    from app.routers.dashboard import TASK_GROUPS

    all_task_types = []
    for group_name, task_types in TASK_GROUPS.items():
        all_task_types.extend(task_types)

    assert set(all_task_types) == set(TaskType)
    assert len(all_task_types) == len(set(all_task_types)), "Duplicate task types in TASK_GROUPS"
    assert set(TASK_GROUPS.keys()) == {"Alerts", "Automations", "Reminders"}


# ---------------------------------------------------------------------------
# POST handler redirect
# ---------------------------------------------------------------------------

async def test_save_contact_returns_303_on_success():
    from fastapi import Request
    from fastapi.responses import RedirectResponse

    from app.routers.dashboard import save_contact

    booking = _make_booking(guest_email=None, guest_phone=None)

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    mock_request = MagicMock(spec=Request)
    mock_request.method = "POST"
    mock_request.url = MagicMock()

    with (
        patch("app.routers.dashboard._dispatch_pending_tasks") as mock_dispatch,
        patch("app.routers.dashboard.asyncio.create_task"),
    ):
        mock_dispatch.return_value = AsyncMock()
        response = await save_contact(
            booking_id=booking.id,
            request=mock_request,
            guest_phone="5551234567",
            guest_email=None,
            db=mock_session,
        )

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == f"/bookings/{booking.id}"


# ---------------------------------------------------------------------------
# F13 (bug hunt 2026-07-22): editable contact fields, not write-once
# ---------------------------------------------------------------------------


def _post_ctx():
    from fastapi import Request

    mock_request = MagicMock(spec=Request)
    mock_request.method = "POST"
    mock_request.url = MagicMock()
    return mock_request


def _mock_session_for(booking):
    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    return mock_session


async def test_save_contact_overwrite_without_confirmation_is_rejected():
    """Changing an already-set phone without ticking the confirm box does not
    save the new value and does not touch Seam — a typo'd resubmit shouldn't
    silently re-trigger the door-code automation."""
    from app.routers.dashboard import save_contact

    booking = _make_booking(guest_phone="5551234567", guest_email=None)
    booking.tasks = []
    mock_session = _mock_session_for(booking)

    with patch("app.routers.dashboard.delete_seam_access_code") as mock_delete:
        response = await save_contact(
            booking_id=booking.id,
            request=_post_ctx(),
            guest_phone="5559876543",
            guest_email=None,
            confirm_overwrite=None,
            db=mock_session,
        )

    assert response.status_code == 200
    assert booking.guest_phone == "5551234567"  # unchanged
    mock_delete.assert_not_called()
    mock_session.commit.assert_not_awaited()


async def test_save_contact_resubmitting_same_value_needs_no_confirmation():
    """Resubmitting the field's current value isn't a change — no confirm
    checkbox required, no Seam call, still redirects."""
    from app.routers.dashboard import save_contact

    booking = _make_booking(guest_phone="5551234567", guest_email=None)
    booking.tasks = []
    mock_session = _mock_session_for(booking)

    with (
        patch("app.routers.dashboard.delete_seam_access_code") as mock_delete,
        patch("app.routers.dashboard._dispatch_pending_tasks") as mock_dispatch,
        patch("app.routers.dashboard.asyncio.create_task"),
    ):
        mock_dispatch.return_value = AsyncMock()
        response = await save_contact(
            booking_id=booking.id,
            request=_post_ctx(),
            guest_phone="5551234567",
            guest_email=None,
            confirm_overwrite=None,
            db=mock_session,
        )

    assert response.status_code == 303
    mock_delete.assert_not_called()


async def test_save_contact_phone_change_confirmed_recreates_access_code():
    """A confirmed phone correction after ACCESS_CODE_CREATE completed deletes
    the stale Seam code and resets the task so the dispatcher (fired after
    commit) creates a fresh one for the corrected number."""
    from app.routers.dashboard import save_contact

    booking = _make_booking(guest_phone="5551234567", guest_email=None)
    access_task = BookingTask(
        id=uuid.uuid4(),
        booking_id=booking.id,
        task_type=TaskType.ACCESS_CODE_CREATE,
        state=TaskState.COMPLETE,
        external_ref="seam-code-old",
        completed_at=datetime.now(timezone.utc),
    )
    booking.tasks = [access_task]
    mock_session = _mock_session_for(booking)

    with (
        patch("app.routers.dashboard.delete_seam_access_code") as mock_delete,
        patch("app.routers.dashboard._dispatch_pending_tasks") as mock_dispatch,
        patch("app.routers.dashboard.asyncio.create_task"),
    ):
        mock_dispatch.return_value = AsyncMock()
        response = await save_contact(
            booking_id=booking.id,
            request=_post_ctx(),
            guest_phone="5559876543",
            guest_email=None,
            confirm_overwrite="1",
            db=mock_session,
        )

    assert response.status_code == 303
    assert booking.guest_phone == "5559876543"
    mock_delete.assert_called_once_with("seam-code-old")
    assert access_task.external_ref is None
    assert access_task.state == TaskState.PENDING
    assert access_task.completed_at is None


async def test_save_contact_email_change_before_signing_voids_and_resends():
    """A confirmed email correction before the guest has signed voids the
    stale envelope and resets DOCUSIGN_SEND so a fresh one goes to the
    corrected address."""
    from app.routers.dashboard import save_contact

    booking = _make_booking(guest_phone=None, guest_email="old@example.com")
    booking.signed_pdf_path = None
    docusign_task = BookingTask(
        id=uuid.uuid4(),
        booking_id=booking.id,
        task_type=TaskType.DOCUSIGN_SEND,
        state=TaskState.COMPLETE,
        external_ref="envelope-old",
        completed_at=datetime.now(timezone.utc),
    )
    booking.tasks = [docusign_task]
    mock_session = _mock_session_for(booking)

    with (
        patch("app.routers.dashboard.void_envelope_idempotent") as mock_void,
        patch("app.routers.dashboard._dispatch_pending_tasks") as mock_dispatch,
        patch("app.routers.dashboard.asyncio.create_task"),
    ):
        mock_dispatch.return_value = AsyncMock()
        response = await save_contact(
            booking_id=booking.id,
            request=_post_ctx(),
            guest_phone=None,
            guest_email="new@example.com",
            confirm_overwrite="1",
            db=mock_session,
        )

    assert response.status_code == 303
    assert booking.guest_email == "new@example.com"
    mock_void.assert_called_once_with("envelope-old")
    assert docusign_task.external_ref is None
    assert docusign_task.state == TaskState.PENDING
    assert docusign_task.completed_at is None


async def test_save_contact_email_change_after_signing_does_not_resend():
    """Once the guest has signed (signed_pdf_path set), an email correction
    is saved for the record but the envelope is left alone — DocuSign
    refuses to void a completed envelope, and the guest already has a
    validly signed copy."""
    from app.routers.dashboard import save_contact

    booking = _make_booking(guest_phone=None, guest_email="old@example.com")
    booking.signed_pdf_path = "/app/data/pdfs/x.pdf"
    docusign_task = BookingTask(
        id=uuid.uuid4(),
        booking_id=booking.id,
        task_type=TaskType.DOCUSIGN_SEND,
        state=TaskState.COMPLETE,
        external_ref="envelope-old",
        completed_at=datetime.now(timezone.utc),
    )
    booking.tasks = [docusign_task]
    mock_session = _mock_session_for(booking)

    with (
        patch("app.routers.dashboard.void_envelope_idempotent") as mock_void,
        patch("app.routers.dashboard._dispatch_pending_tasks") as mock_dispatch,
        patch("app.routers.dashboard.asyncio.create_task"),
    ):
        mock_dispatch.return_value = AsyncMock()
        response = await save_contact(
            booking_id=booking.id,
            request=_post_ctx(),
            guest_phone=None,
            guest_email="new@example.com",
            confirm_overwrite="1",
            db=mock_session,
        )

    assert response.status_code == 303
    assert booking.guest_email == "new@example.com"
    mock_void.assert_not_called()
    assert docusign_task.external_ref == "envelope-old"
    assert docusign_task.state == TaskState.COMPLETE


# ---------------------------------------------------------------------------
# Task summary (booking-list Status + Tasks columns)
#
# The status answers "who is the ball with", derived from task states plus the
# booking's contact fields and signed_pdf_path:
#   failed > action_needed > in_progress > waiting_on_guest > scheduled >
#   complete > pending (transient fallback)
# ---------------------------------------------------------------------------


def _booking_with_states(*states: TaskState) -> Booking:
    """Booking carrying one task per given state (task_type is irrelevant here)."""
    booking = _make_booking()
    types = list(TaskType)
    booking.tasks = [
        BookingTask(id=uuid.uuid4(), booking_id=booking.id, task_type=types[i], state=s)
        for i, s in enumerate(states)
    ]
    return booking


# A realistic post-dispatch task set; tests override individual entries.
_WORKFLOW_DEFAULTS: dict[TaskType, TaskState] = {
    TaskType.OWNER_ALERT_NEW_BOOKING: TaskState.COMPLETE,
    TaskType.CLEANER_SHEET_ADD: TaskState.COMPLETE,
    TaskType.DOCUSIGN_SEND: TaskState.COMPLETE,
    TaskType.ACCESS_CODE_CREATE: TaskState.COMPLETE,
    TaskType.HOA_EMAIL: TaskState.WAITING,
    TaskType.OWNER_ALERT_MISSING_PHONE_7D: TaskState.SKIPPED,
    TaskType.OWNER_ALERT_MISSING_PHONE_4D: TaskState.SKIPPED,
    TaskType.OWNER_ALERT_MISSING_EMAIL_7D: TaskState.SKIPPED,
    TaskType.OWNER_ALERT_MISSING_EMAIL_4D: TaskState.SKIPPED,
    TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D: TaskState.SKIPPED,
    TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D: TaskState.SKIPPED,
}


def _workflow_booking(
    *,
    guest_phone: str | None = "5551234567",
    guest_email: str | None = "g@example.com",
    signed_pdf_path: str | None = None,
    **state_overrides: TaskState,
) -> Booking:
    """Booking with the full 11-task set; override states by task-type value name."""
    booking = _make_booking(guest_phone=guest_phone, guest_email=guest_email)
    booking.signed_pdf_path = signed_pdf_path
    overrides = {TaskType(name): state for name, state in state_overrides.items()}
    booking.tasks = [
        BookingTask(
            id=uuid.uuid4(),
            booking_id=booking.id,
            task_type=tt,
            state=overrides.get(tt, default),
        )
        for tt, default in _WORKFLOW_DEFAULTS.items()
    ]
    return booking


def test_summarize_tasks_counts_cover_automations_only():
    """The list's Tasks column counts the 4 automations — reminders and alerts
    are notifications, not work items, and would inflate the metric."""
    from app.routers.dashboard import _summarize_tasks

    # _workflow_booking: cleaner/docusign/access COMPLETE, HOA WAITING,
    # alert COMPLETE + 6 reminders SKIPPED (which must NOT count).
    summary = _summarize_tasks(_workflow_booking())
    assert summary["done"] == 3
    assert summary["total"] == 4
    assert summary["failed"] == 0


def test_summarize_tasks_counts_done_over_total():
    from app.routers.dashboard import _summarize_tasks

    # types[0..2] = new-booking alert, DOCUSIGN_SEND, CLEANER_SHEET_ADD —
    # only the two automations count toward done/total.
    summary = _summarize_tasks(
        _booking_with_states(
            TaskState.COMPLETE, TaskState.COMPLETE, TaskState.PENDING
        )
    )
    assert summary["done"] == 1
    assert summary["total"] == 2
    assert summary["failed"] == 0


def test_summarize_tasks_failed_counts_any_task_type():
    """A failure anywhere (even a non-automation row) must surface."""
    from app.routers.dashboard import _summarize_tasks

    summary = _summarize_tasks(
        _workflow_booking(owner_alert_new_booking=TaskState.FAILED)
    )
    assert summary["status"] == "failed"
    assert summary["failed"] == 1
    # Automation counts unaffected by the alert row.
    assert summary["total"] == 4


def test_summarize_tasks_not_complete_while_alert_outstanding():
    """All automations settled but a PENDING alert row (send failed) must not
    read as complete."""
    from app.routers.dashboard import _summarize_tasks

    summary = _summarize_tasks(
        _workflow_booking(
            signed_pdf_path="/app/data/pdfs/x.pdf",
            hoa_email=TaskState.COMPLETE,
            owner_alert_new_booking=TaskState.PENDING,
        )
    )
    assert summary["done"] == 4
    assert summary["total"] == 4
    assert summary["status"] != "complete"


def test_summarize_tasks_failed_dominates():
    """A failure is the thing the owner must see, even mid-progress."""
    from app.routers.dashboard import _summarize_tasks

    summary = _summarize_tasks(
        _booking_with_states(TaskState.COMPLETE, TaskState.FAILED, TaskState.IN_PROGRESS)
    )
    assert summary["status"] == "failed"
    assert summary["failed"] == 1


def test_summarize_tasks_action_needed_when_contact_missing():
    """Missing phone or email means the system is waiting on the OWNER."""
    from app.routers.dashboard import _summarize_tasks

    no_email = _workflow_booking(
        guest_email=None,
        docusign_send=TaskState.WAITING,
        owner_alert_missing_email_7d=TaskState.PENDING,
        owner_alert_missing_email_4d=TaskState.PENDING,
    )
    assert _summarize_tasks(no_email)["status"] == "action_needed"

    no_phone = _workflow_booking(
        guest_phone=None,
        access_code_create=TaskState.WAITING,
        owner_alert_missing_phone_7d=TaskState.PENDING,
        owner_alert_missing_phone_4d=TaskState.PENDING,
    )
    assert _summarize_tasks(no_phone)["status"] == "action_needed"


def test_summarize_tasks_action_needed_beats_waiting_on_guest():
    """Owner input missing outranks the guest's unsigned form."""
    from app.routers.dashboard import _summarize_tasks

    booking = _workflow_booking(
        guest_phone=None,
        access_code_create=TaskState.WAITING,
        signed_pdf_path=None,  # DOCUSIGN_SEND complete, guest hasn't signed
    )
    assert _summarize_tasks(booking)["status"] == "action_needed"


def test_summarize_tasks_waiting_on_guest_when_sent_but_unsigned():
    """Contacts entered, envelope sent, no signed PDF → ball is with the guest."""
    from app.routers.dashboard import _summarize_tasks

    booking = _workflow_booking(signed_pdf_path=None)
    assert _summarize_tasks(booking)["status"] == "waiting_on_guest"


def test_summarize_tasks_scheduled_when_only_hoa_window_remains():
    """Everything settled except HOA_EMAIL waiting for its send window."""
    from app.routers.dashboard import _summarize_tasks

    booking = _workflow_booking(signed_pdf_path="/app/data/pdfs/x.pdf")
    assert _summarize_tasks(booking)["status"] == "scheduled"


def test_summarize_tasks_complete_when_all_settled():
    from app.routers.dashboard import _summarize_tasks

    booking = _workflow_booking(
        signed_pdf_path="/app/data/pdfs/x.pdf",
        hoa_email=TaskState.COMPLETE,
    )
    summary = _summarize_tasks(booking)
    assert summary["status"] == "complete"
    assert summary["done"] == summary["total"]


def test_summarize_tasks_in_progress_when_automation_running():
    from app.routers.dashboard import _summarize_tasks

    booking = _workflow_booking(
        signed_pdf_path=None,
        access_code_create=TaskState.IN_PROGRESS,
    )
    assert _summarize_tasks(booking)["status"] == "in_progress"


def test_summarize_tasks_pending_fallback_for_transient_dispatch():
    """Contacts present but dispatch hasn't run yet — transient, not a wait."""
    from app.routers.dashboard import _summarize_tasks

    booking = _workflow_booking(
        cleaner_sheet_add=TaskState.PENDING,
        docusign_send=TaskState.PENDING,
        access_code_create=TaskState.PENDING,
    )
    assert _summarize_tasks(booking)["status"] == "pending"


def test_summarize_tasks_handles_no_tasks():
    """An empty task list must not report 'complete' via 0 == 0."""
    from app.routers.dashboard import _summarize_tasks

    summary = _summarize_tasks(_booking_with_states())
    assert summary["total"] == 0
    assert summary["status"] == "pending"


def test_summarize_tasks_statuses_all_have_badge_css():
    """Every derivable status feeds `badge-{{ status }}` — the stylesheet must
    style each one, and _summarize_tasks must never emit anything else."""
    from pathlib import Path

    from app.routers.dashboard import BOOKING_STATUSES, _summarize_tasks

    css = (Path(__file__).parents[2] / "app" / "static" / "style.css").read_text()
    for status in BOOKING_STATUSES:
        assert f".badge-{status}" in css, f"style.css missing .badge-{status}"

    samples = [
        _workflow_booking(),
        _workflow_booking(guest_email=None),
        _workflow_booking(signed_pdf_path="/x.pdf"),
        _workflow_booking(hoa_email=TaskState.FAILED),
        _booking_with_states(),
    ]
    for b in samples:
        assert _summarize_tasks(b)["status"] in BOOKING_STATUSES


# ---------------------------------------------------------------------------
# HOA pipeline (detail-page panel)
# ---------------------------------------------------------------------------


def _pipeline_config(prop_id: str = "property_1") -> MagicMock:
    """Config stub: HOA open every day so window math is weekday-independent
    (latest = check_in - days_min, earliest = check_in - days_max)."""
    prop = MagicMock()
    prop.id = prop_id
    prop.hoa.open_days = [1, 2, 3, 4, 5, 6, 7]
    prop.hoa.email_window_days_min = 2
    prop.hoa.email_window_days_max = 7
    cfg = MagicMock()
    cfg.properties = [prop]
    return cfg


def _pipeline_for(booking, cfg=None):
    from app.routers.dashboard import _hoa_pipeline

    tasks_by_type = {t.task_type: t for t in booking.tasks}
    with patch("app.routers.dashboard.load_config", return_value=cfg or _pipeline_config()):
        return _hoa_pipeline(booking, tasks_by_type)


def test_hoa_pipeline_before_window():
    from datetime import timedelta

    booking = _workflow_booking(signed_pdf_path="/app/data/pdfs/x.pdf")
    booking.check_in_date = date.today() + timedelta(days=30)

    p = _pipeline_for(booking)
    assert p["window_status"] == "before"
    # The panel must mirror hoa_window's output for the property's config —
    # the window math itself is pinned in tests/unit/test_hoa_window.py.
    from app.integrations.hoa.window import hoa_window

    # open_days must match _pipeline_config's stub, or this assertion becomes
    # weekday-dependent: the two disagree only when a closed Sunday falls in
    # the lead-time window, so it would pass on most days and fail on some.
    expected_earliest, expected_latest = hoa_window(
        booking.check_in_date, [1, 2, 3, 4, 5, 6, 7], 2, 7
    )
    assert p["earliest"] == expected_earliest
    assert p["latest"] == expected_latest
    assert p["docusign_sent"] is True
    assert p["signed"] is True
    assert p["hoa_sent"] is False


def test_hoa_pipeline_open_window():
    from datetime import timedelta

    booking = _workflow_booking(signed_pdf_path="/app/data/pdfs/x.pdf")
    booking.check_in_date = date.today() + timedelta(days=3)  # latest = today+1

    p = _pipeline_for(booking)
    assert p["window_status"] == "open"


def test_hoa_pipeline_passed_window():
    from datetime import timedelta

    booking = _workflow_booking(signed_pdf_path=None)
    booking.check_in_date = date.today() + timedelta(days=1)  # latest = today-1

    p = _pipeline_for(booking)
    assert p["window_status"] == "passed"


def test_hoa_pipeline_sent():
    from datetime import timedelta

    booking = _workflow_booking(
        signed_pdf_path="/app/data/pdfs/x.pdf",
        hoa_email=TaskState.COMPLETE,
    )
    booking.check_in_date = date.today() + timedelta(days=3)

    p = _pipeline_for(booking)
    assert p["window_status"] == "sent"
    assert p["hoa_sent"] is True


def test_hoa_pipeline_unsent_unsigned_flags():
    from datetime import timedelta

    booking = _workflow_booking(
        guest_email=None,
        signed_pdf_path=None,
        docusign_send=TaskState.WAITING,
    )
    booking.check_in_date = date.today() + timedelta(days=30)

    p = _pipeline_for(booking)
    assert p["docusign_sent"] is False
    assert p["signed"] is False


def test_hoa_pipeline_none_when_config_unavailable():
    from app.routers.dashboard import _hoa_pipeline

    booking = _workflow_booking()
    tasks_by_type = {t.task_type: t for t in booking.tasks}
    with patch("app.routers.dashboard.load_config", side_effect=OSError("no config")):
        assert _hoa_pipeline(booking, tasks_by_type) is None


def test_hoa_pipeline_none_when_property_unknown():
    booking = _workflow_booking()
    assert _pipeline_for(booking, cfg=_pipeline_config(prop_id="other")) is None


def test_task_labels_cover_every_task_type():
    """Every TaskType renders with a human label on the detail page."""
    from app.routers.dashboard import TASK_LABELS

    assert set(TASK_LABELS) == set(TaskType)


# ---------------------------------------------------------------------------
# Access code display (detail page)
# ---------------------------------------------------------------------------


def test_latest_access_code_returns_most_recent_value():
    from app.routers.dashboard import _latest_access_code

    booking = _make_booking()
    booking.data_points = [
        DataPoint(
            booking_id=booking.id,
            field_name="access_code",
            value="1111",
            source=DataPointSource.SEAM_API,
            recorded_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
        DataPoint(
            booking_id=booking.id,
            field_name="guest_phone",
            value="5551234999",
            source=DataPointSource.EMAIL_PARSE,
            recorded_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        ),
        DataPoint(
            booking_id=booking.id,
            field_name="access_code",
            value="2222",
            source=DataPointSource.SEAM_API,
            recorded_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        ),
    ]
    assert _latest_access_code(booking) == "2222"


def test_latest_access_code_none_when_absent():
    from app.routers.dashboard import _latest_access_code

    booking = _make_booking()
    booking.data_points = []
    assert _latest_access_code(booking) is None
