"""Unit tests for the cancellation handler (step 2.7).

Phase 4 additions (Wave 3):
  - test_void_docusign_envelope_calls_idempotent_void_handler
  - test_delete_seam_access_code_calls_seam_sdk_delete
  - test_void_docusign_envelope_signature_remains_sync

On cancellation the system must:
  1. Mark the booking CANCELLED in the DB.
  2. Void the DocuSign envelope if one was sent (and not yet completed).
  3. Delete the Seam access code if one was created.
  4. Send an alert email — always — telling owners to check HOA and cleaner sheet.

Each mid-workflow state is tested to verify the right subset of actions fires:
  A. Booking detected, nothing sent yet (no DocuSign, no Seam code)
  B. DocuSign sent but not yet signed
  C. DocuSign completed (signed) — void is a no-op/skipped
  D. Seam access code created (phone was entered)
  E. Both DocuSign sent and Seam code created

External API calls (DocuSign void, Seam delete) are stubbed throughout.
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


def _make_booking_with_tasks(task_overrides: dict[TaskType, tuple[TaskState, str | None]] | None = None):
    """Create an in-memory Booking with a full task set, optionally overriding states."""
    booking = Booking(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id="HMCANCEL01",
        property_id="property_1",
        guest_first_name="Cancel",
        guest_last_name="Guest",
        check_in_date=date(2026, 9, 1),
        check_out_date=date(2026, 9, 4),
        status=BookingStatus.ACTIVE,
        source_email_message_id="msg-src",
    )
    default_tasks = {
        TaskType.OWNER_ALERT_NEW_BOOKING: (TaskState.COMPLETE, None),
        TaskType.DOCUSIGN_SEND: (TaskState.WAITING, None),
        TaskType.CLEANER_SHEET_ADD: (TaskState.PENDING, None),
        TaskType.ACCESS_CODE_CREATE: (TaskState.WAITING, None),
        TaskType.HOA_EMAIL: (TaskState.WAITING, None),
    }
    if task_overrides:
        default_tasks.update(task_overrides)
    for task_type, (state, ext_ref) in default_tasks.items():
        booking.tasks.append(BookingTask(task_type=task_type, state=state, external_ref=ext_ref))
    return booking


# ---------------------------------------------------------------------------
# parse_cancellation_code  — extract external ID from the cancellation email
# ---------------------------------------------------------------------------

def test_parse_airbnb_cancellation_code_from_subject():
    from app.ingestion.cancellation import parse_cancellation_external_id
    import email as email_lib
    from email import policy as email_policy

    raw = (
        "From: Airbnb <automated@airbnb.com>\r\n"
        "Subject: Canceled: Reservation HMTEST0001 for Feb 23 – 27, 2026\r\n"
        "Content-Type: text/plain\r\n\r\nSome body text.\r\n"
    )
    msg = email_lib.message_from_string(raw, policy=email_policy.default)
    assert parse_cancellation_external_id(msg, Platform.AIRBNB) == "HMTEST0001"


def test_parse_vrbo_cancellation_id_from_subject():
    from app.ingestion.cancellation import parse_cancellation_external_id
    import email as email_lib
    from email import policy as email_policy

    raw = (
        "From: Vrbo <vrbo@partners.expediagroup.com>\r\n"
        "Subject: Your reservation HA-TEST01 was canceled at Property 12345678\r\n"
        "Content-Type: text/plain\r\n\r\nSome body.\r\n"
    )
    msg = email_lib.message_from_string(raw, policy=email_policy.default)
    assert parse_cancellation_external_id(msg, Platform.VRBO) == "HA-TEST01"


def test_parse_cancellation_from_real_airbnb_fixture():
    """Smoke test: parse_cancellation_external_id extracts a non-empty code from the real fixture."""
    from app.ingestion.cancellation import parse_cancellation_external_id
    from tests.conftest import recorded_email_message

    msg = recorded_email_message("airbnb-cancellation-1.eml")
    result = parse_cancellation_external_id(msg, Platform.AIRBNB)
    assert result and len(result) >= 8  # Airbnb codes are at least 8 alphanumeric chars


def test_parse_cancellation_from_real_vrbo_fixture():
    """Smoke test: parse_cancellation_external_id extracts a non-empty VRBO ID from the real fixture."""
    from app.ingestion.cancellation import parse_cancellation_external_id
    from tests.conftest import recorded_email_message

    msg = recorded_email_message("vrbo-cancellation-1.eml")
    result = parse_cancellation_external_id(msg, Platform.VRBO)
    assert result and result.startswith("HA-")


# ---------------------------------------------------------------------------
# handle_cancellation — state A: nothing sent yet
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_a_no_docusign_no_seam_marks_cancelled_and_sends_alert():
    """Nothing sent: booking marked CANCELLED, alert sent, no API calls."""
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks()
    alert_sent = {}
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope") as mock_void,
        patch("app.ingestion.cancellation.delete_seam_access_code") as mock_seam,
        patch("app.ingestion.cancellation.send_cancellation_alert") as mock_alert,
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)

    assert booking.status == BookingStatus.CANCELLED
    mock_void.assert_not_called()
    mock_seam.assert_not_called()
    mock_alert.assert_called_once()


# ---------------------------------------------------------------------------
# state B: DocuSign envelope sent but not signed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_b_docusign_sent_not_signed_voids_envelope():
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks(task_overrides={
        TaskType.DOCUSIGN_SEND: (TaskState.COMPLETE, "envelope-abc"),
    })
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope") as mock_void,
        patch("app.ingestion.cancellation.delete_seam_access_code"),
        patch("app.ingestion.cancellation.send_cancellation_alert"),
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)

    mock_void.assert_called_once_with("envelope-abc")


# ---------------------------------------------------------------------------
# state C: DocuSign completed (signed) — void is skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_c_docusign_completed_does_not_void():
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks(task_overrides={
        TaskType.DOCUSIGN_SEND: (TaskState.COMPLETE, "envelope-abc"),
        TaskType.HOA_EMAIL: (TaskState.COMPLETE, None),  # form was signed and HOA emailed
    })
    # Mark the HOA email task complete to signal docusign is done too
    for t in booking.tasks:
        if t.task_type == TaskType.DOCUSIGN_SEND:
            t.external_ref = "envelope-abc"
        if t.task_type == TaskType.HOA_EMAIL:
            t.state = TaskState.COMPLETE
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope") as mock_void,
        patch("app.ingestion.cancellation.delete_seam_access_code"),
        patch("app.ingestion.cancellation.send_cancellation_alert"),
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)

    # Voiding a completed envelope is a no-op; void should not be called.
    mock_void.assert_not_called()


# ---------------------------------------------------------------------------
# state D: Seam access code created
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_d_seam_code_created_deletes_it():
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks(task_overrides={
        TaskType.ACCESS_CODE_CREATE: (TaskState.COMPLETE, "seam-code-xyz"),
    })
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope"),
        patch("app.ingestion.cancellation.delete_seam_access_code") as mock_seam,
        patch("app.ingestion.cancellation.send_cancellation_alert"),
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)

    mock_seam.assert_called_once_with("seam-code-xyz")


# ---------------------------------------------------------------------------
# state E: both DocuSign sent and Seam code created
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_e_docusign_and_seam_both_handled():
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks(task_overrides={
        TaskType.DOCUSIGN_SEND: (TaskState.COMPLETE, "envelope-def"),
        TaskType.ACCESS_CODE_CREATE: (TaskState.COMPLETE, "seam-code-456"),
    })
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope") as mock_void,
        patch("app.ingestion.cancellation.delete_seam_access_code") as mock_seam,
        patch("app.ingestion.cancellation.send_cancellation_alert"),
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)

    mock_void.assert_called_once_with("envelope-def")
    mock_seam.assert_called_once_with("seam-code-456")


# ---------------------------------------------------------------------------
# Idempotency — calling cancellation twice is safe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancellation_idempotent_if_already_cancelled():
    """Calling _apply_cancellation on an already-CANCELLED booking does nothing."""
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks()
    booking.status = BookingStatus.CANCELLED
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope") as mock_void,
        patch("app.ingestion.cancellation.delete_seam_access_code") as mock_seam,
        patch("app.ingestion.cancellation.send_cancellation_alert") as mock_alert,
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)

    mock_void.assert_not_called()
    mock_seam.assert_not_called()
    mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# Step 17 — partial-failure isolation: no external/alert failure may abort the
# cancellation (which would leave the booking ACTIVE after irreversible cleanups
# and cause an endless re-poll).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_cancellation_alert_failure_does_not_abort_cancellation():
    """If the owner-alert send raises, the booking is STILL marked CANCELLED and
    the cancellation message id recorded (durable, not re-polled); the exception
    does not propagate; the alert task rows are NOT marked COMPLETE."""
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks(task_overrides={
        TaskType.DOCUSIGN_SEND: (TaskState.COMPLETE, "env-1"),
        TaskType.ACCESS_CODE_CREATE: (TaskState.COMPLETE, "code-1"),
    })
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope") as mock_void,
        patch("app.ingestion.cancellation.delete_seam_access_code") as mock_seam,
        patch("app.ingestion.cancellation.send_cancellation_alert", side_effect=RuntimeError("gmail down")),
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)  # must not raise

    assert booking.status == BookingStatus.CANCELLED
    assert booking.cancellation_email_message_id == "msg-cancel"
    mock_void.assert_called_once_with("env-1")
    mock_seam.assert_called_once_with("code-1")
    by_type = {t.task_type: t for t in booking.tasks}
    hoa_alert = by_type.get(TaskType.OWNER_ALERT_CANCELLATION_HOA)
    assert hoa_alert is None or hoa_alert.state != TaskState.COMPLETE


@pytest.mark.asyncio
async def test_apply_cancellation_void_failure_isolated():
    """A DocuSign void failure must not stop the Seam delete + alert, or the CANCELLED state."""
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks(task_overrides={
        TaskType.DOCUSIGN_SEND: (TaskState.COMPLETE, "env-1"),
        TaskType.ACCESS_CODE_CREATE: (TaskState.COMPLETE, "code-1"),
    })
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope", side_effect=RuntimeError("ds down")),
        patch("app.ingestion.cancellation.delete_seam_access_code") as mock_seam,
        patch("app.ingestion.cancellation.send_cancellation_alert") as mock_alert,
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-c", session=session)

    assert booking.status == BookingStatus.CANCELLED
    mock_seam.assert_called_once_with("code-1")
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_apply_cancellation_seam_failure_isolated():
    """A Seam delete failure must not stop the alert, or the CANCELLED state."""
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks(task_overrides={
        TaskType.ACCESS_CODE_CREATE: (TaskState.COMPLETE, "code-1"),
    })
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope"),
        patch("app.ingestion.cancellation.delete_seam_access_code", side_effect=RuntimeError("seam down")),
        patch("app.ingestion.cancellation.send_cancellation_alert") as mock_alert,
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-c", session=session)

    assert booking.status == BookingStatus.CANCELLED
    mock_alert.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 4 Wave 3: stub replacement — verify real implementations call through
# ---------------------------------------------------------------------------

def test_void_docusign_envelope_calls_idempotent_void_handler():
    """void_docusign_envelope delegates to void_envelope_idempotent from handlers/docusign."""
    from app.ingestion.cancellation import void_docusign_envelope

    with patch("app.tasks.handlers.docusign.void_envelope_idempotent") as mock_void:
        void_docusign_envelope("env-1")

    mock_void.assert_called_once_with("env-1")


def test_delete_seam_access_code_calls_seam_sdk_delete():
    """delete_seam_access_code delegates to Seam SDK client.access_codes.delete."""
    from app.ingestion.cancellation import delete_seam_access_code

    mock_client = MagicMock()
    with patch("app.integrations.seam.client.get_seam_client", return_value=mock_client):
        delete_seam_access_code("ac-1")

    mock_client.access_codes.delete.assert_called_once_with(access_code_id="ac-1")


def test_void_docusign_envelope_signature_remains_sync():
    """Both void_docusign_envelope and delete_seam_access_code must be sync (not async)."""
    import asyncio
    from app.ingestion.cancellation import void_docusign_envelope, delete_seam_access_code

    assert not asyncio.iscoroutinefunction(void_docusign_envelope), (
        "void_docusign_envelope must remain a sync def (cancellation calls it without await)"
    )
    assert not asyncio.iscoroutinefunction(delete_seam_access_code), (
        "delete_seam_access_code must remain a sync def (cancellation calls it without await)"
    )


# ---------------------------------------------------------------------------
# Step 3: send_cancellation_alert real implementation (Gmail boundary mocked)
# ---------------------------------------------------------------------------

def test_send_cancellation_alert_sends_via_gmail_alerts_service():
    """send_cancellation_alert builds the email and sends it via get_alerts_service().

    Mocks ONLY the Gmail boundary (get_alerts_service) and the config loader;
    the (subject, body) builder runs for real.
    """
    import base64
    from app.ingestion.cancellation import send_cancellation_alert

    booking = _make_booking_with_tasks()
    service = MagicMock()
    service.users().messages().send().execute.return_value = {"id": "sent-cancel-1"}
    config = MagicMock()
    config.email.alerts = "alerts@example.com"

    with (
        patch("app.ingestion.cancellation.get_alerts_service", return_value=service) as mock_get,
        patch("app.ingestion.cancellation.load_config", return_value=config),
    ):
        send_cancellation_alert(booking)

    mock_get.assert_called_once()
    service.users.return_value.messages.return_value.send.assert_called()
    send_call = service.users.return_value.messages.return_value.send.call_args
    body_arg = send_call.kwargs.get("body") or send_call[1].get("body") or send_call[0][0]
    assert "raw" in body_arg
    base64.urlsafe_b64decode(body_arg["raw"] + "==")


def test_send_cancellation_alert_is_sync():
    """send_cancellation_alert must stay sync — _apply_cancellation calls it without await."""
    import asyncio
    from app.ingestion.cancellation import send_cancellation_alert
    assert not asyncio.iscoroutinefunction(send_cancellation_alert)


# ---------------------------------------------------------------------------
# Step 3: cancellation alert task rows are created + completed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_cancellation_completes_cancellation_alert_task_rows():
    """_apply_cancellation marks OWNER_ALERT_CANCELLATION_HOA/_CLEANER COMPLETE (creating if absent)."""
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks()  # no cancellation-alert tasks in the default set
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope"),
        patch("app.ingestion.cancellation.delete_seam_access_code"),
        patch("app.ingestion.cancellation.send_cancellation_alert"),
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)

    by_type = {t.task_type: t for t in booking.tasks}
    assert TaskType.OWNER_ALERT_CANCELLATION_HOA in by_type
    assert TaskType.OWNER_ALERT_CANCELLATION_CLEANER in by_type
    assert by_type[TaskType.OWNER_ALERT_CANCELLATION_HOA].state == TaskState.COMPLETE
    assert by_type[TaskType.OWNER_ALERT_CANCELLATION_CLEANER].state == TaskState.COMPLETE


@pytest.mark.asyncio
async def test_apply_cancellation_reuses_existing_alert_task_rows():
    """If the cancellation-alert task rows already exist, they are flipped to COMPLETE (not duplicated)."""
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks(task_overrides={
        TaskType.OWNER_ALERT_CANCELLATION_HOA: (TaskState.PENDING, None),
        TaskType.OWNER_ALERT_CANCELLATION_CLEANER: (TaskState.PENDING, None),
    })
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope"),
        patch("app.ingestion.cancellation.delete_seam_access_code"),
        patch("app.ingestion.cancellation.send_cancellation_alert"),
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)

    hoa_rows = [t for t in booking.tasks if t.task_type == TaskType.OWNER_ALERT_CANCELLATION_HOA]
    cleaner_rows = [t for t in booking.tasks if t.task_type == TaskType.OWNER_ALERT_CANCELLATION_CLEANER]
    assert len(hoa_rows) == 1 and hoa_rows[0].state == TaskState.COMPLETE
    assert len(cleaner_rows) == 1 and cleaner_rows[0].state == TaskState.COMPLETE


# ---------------------------------------------------------------------------
# F1b (bug hunt 2026-07-22): the Airbnb code regex must not extract garbage
# ---------------------------------------------------------------------------


def _cancel_msg(subject: str):
    import email as email_lib
    from email import policy as email_policy

    raw = (
        "From: Airbnb <automated@airbnb.com>\r\n"
        f"Subject: {subject}\r\n"
        "Content-Type: text/plain\r\n\r\nSome body text.\r\n"
    )
    return email_lib.message_from_string(raw, policy=email_policy.default)


def test_airbnb_cancellation_code_not_extracted_from_lowercase_word():
    """'Reservation canceled by guest' used to extract the garbage id 'CANCELED'
    (IGNORECASE made [A-Z0-9]{8,} match lowercase words), which then replayed
    as an unknown-booking cancellation every poll forever."""
    from app.ingestion.cancellation import parse_cancellation_external_id

    msg = _cancel_msg("Reservation canceled by guest")
    assert parse_cancellation_external_id(msg, Platform.AIRBNB) is None


def test_airbnb_cancellation_code_not_extracted_from_allcaps_word():
    """Even an ALL-CAPS subject must not treat a dictionary word as a code —
    real Airbnb confirmation codes contain at least one digit."""
    from app.ingestion.cancellation import parse_cancellation_external_id

    msg = _cancel_msg("RESERVATION CANCELED BY GUEST")
    assert parse_cancellation_external_id(msg, Platform.AIRBNB) is None


def test_airbnb_cancellation_real_code_still_extracted():
    from app.ingestion.cancellation import parse_cancellation_external_id

    msg = _cancel_msg("Canceled: Reservation HMFAKE0001 for Feb 23 - 27, 2026")
    assert parse_cancellation_external_id(msg, Platform.AIRBNB) == "HMFAKE0001"


# ---------------------------------------------------------------------------
# F5 (bug hunt 2026-07-22): cancellation must cancel the WORKFLOW too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cancellation_skips_unstarted_automation_tasks():
    """PENDING/WAITING automation tasks on a cancelled booking must flip to
    SKIPPED — otherwise they stay dispatchable and a later contact-save fires
    a real envelope / door code / cleaner row for a cancelled stay."""
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks()  # DOCUSIGN/ACCESS_CODE/HOA WAITING, CLEANER PENDING
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope"),
        patch("app.ingestion.cancellation.delete_seam_access_code"),
        patch("app.ingestion.cancellation.send_cancellation_alert"),
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)

    by_type = {t.task_type: t for t in booking.tasks}
    assert by_type[TaskType.CLEANER_SHEET_ADD].state == TaskState.SKIPPED
    assert by_type[TaskType.DOCUSIGN_SEND].state == TaskState.SKIPPED
    assert by_type[TaskType.ACCESS_CODE_CREATE].state == TaskState.SKIPPED
    assert by_type[TaskType.HOA_EMAIL].state == TaskState.SKIPPED


@pytest.mark.asyncio
async def test_apply_cancellation_leaves_completed_automation_tasks_untouched():
    """COMPLETE tasks are history (their side effects were handled by the
    void/delete cleanups) — cancellation must not rewrite them."""
    from app.ingestion.cancellation import _apply_cancellation

    booking = _make_booking_with_tasks(task_overrides={
        TaskType.DOCUSIGN_SEND: (TaskState.COMPLETE, "env-9"),
        TaskType.ACCESS_CODE_CREATE: (TaskState.COMPLETE, "code-9"),
    })
    session = AsyncMock()

    with (
        patch("app.ingestion.cancellation.void_docusign_envelope"),
        patch("app.ingestion.cancellation.delete_seam_access_code"),
        patch("app.ingestion.cancellation.send_cancellation_alert"),
    ):
        await _apply_cancellation(booking, cancellation_msg_id="msg-cancel", session=session)

    by_type = {t.task_type: t for t in booking.tasks}
    assert by_type[TaskType.DOCUSIGN_SEND].state == TaskState.COMPLETE
    assert by_type[TaskType.ACCESS_CODE_CREATE].state == TaskState.COMPLETE
    # Unstarted ones still flip.
    assert by_type[TaskType.CLEANER_SHEET_ADD].state == TaskState.SKIPPED
    assert by_type[TaskType.HOA_EMAIL].state == TaskState.SKIPPED
