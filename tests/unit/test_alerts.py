"""Unit tests for the new-booking alert email builder (step 2.6).

The alert is a plain-text email sent to the alerts inbox when a booking is
first persisted.  It must include:
  - Guest name
  - Platform
  - Check-in / checkout dates
  - A deep link to /bookings/{booking_id} on the dashboard
  - Which fields are missing and need manual entry (phone / email)
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest


def _make_booking(platform_value="airbnb", guest_phone=None, guest_email=None):
    from app.db.models import Booking, BookingStatus, Platform
    return Booking(
        id=uuid.uuid4(),
        platform=Platform(platform_value),
        external_id="HMTEST01",
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


# ---------------------------------------------------------------------------
# build_new_booking_alert
# ---------------------------------------------------------------------------

def test_alert_subject_contains_guest_name():
    from app.ingestion.alerts import build_new_booking_alert
    booking = _make_booking()
    subject, _ = build_new_booking_alert(booking, dashboard_base_url="https://example.com")
    assert "Test Guest" in subject


def test_alert_subject_contains_platform():
    from app.ingestion.alerts import build_new_booking_alert
    booking = _make_booking(platform_value="vrbo")
    subject, _ = build_new_booking_alert(booking, dashboard_base_url="https://example.com")
    assert "VRBO" in subject or "vrbo" in subject.lower()


def test_alert_body_contains_deep_link():
    from app.ingestion.alerts import build_new_booking_alert
    booking = _make_booking()
    _, body = build_new_booking_alert(booking, dashboard_base_url="https://example.com")
    assert f"https://example.com/bookings/{booking.id}" in body


def test_alert_body_contains_check_in_and_checkout():
    from app.ingestion.alerts import build_new_booking_alert
    booking = _make_booking()
    _, body = build_new_booking_alert(booking, dashboard_base_url="https://example.com")
    assert "Aug 1" in body or "2026-08-01" in body or "08/01" in body
    assert "Aug 5" in body or "2026-08-05" in body or "08/05" in body


def test_alert_body_flags_missing_phone_for_airbnb():
    from app.ingestion.alerts import build_new_booking_alert
    booking = _make_booking(platform_value="airbnb", guest_phone=None)
    _, body = build_new_booking_alert(booking, dashboard_base_url="https://example.com")
    assert "phone" in body.lower()


def test_alert_body_flags_missing_email():
    from app.ingestion.alerts import build_new_booking_alert
    booking = _make_booking(guest_email=None)
    _, body = build_new_booking_alert(booking, dashboard_base_url="https://example.com")
    assert "email" in body.lower()


def test_alert_body_does_not_flag_phone_when_present():
    from app.ingestion.alerts import build_new_booking_alert
    booking = _make_booking(guest_phone="+15551234567")
    _, body = build_new_booking_alert(booking, dashboard_base_url="https://example.com")
    # Should not call out missing phone when it's already there
    assert "missing phone" not in body.lower() and "need phone" not in body.lower()


def test_alert_body_does_not_flag_email_when_present():
    from app.ingestion.alerts import build_new_booking_alert
    booking = _make_booking(guest_email="guest@example.com")
    _, body = build_new_booking_alert(booking, dashboard_base_url="https://example.com")
    assert "missing email" not in body.lower() and "need email" not in body.lower()


# ---------------------------------------------------------------------------
# send_new_booking_alert  (Gmail API mocked)
# ---------------------------------------------------------------------------

def test_send_new_booking_alert_calls_gmail_send():
    from unittest.mock import MagicMock, patch
    from app.ingestion.alerts import send_new_booking_alert

    booking = _make_booking()
    service = MagicMock()

    with patch("app.ingestion.alerts.build_new_booking_alert", return_value=("Subject", "Body")):
        send_new_booking_alert(booking, alerts_service=service, alerts_address="alerts@example.com",
                               dashboard_base_url="https://example.com")

    service.users.return_value.messages.return_value.send.assert_called_once()


def test_send_encodes_message_as_base64():
    """The message passed to Gmail send must have a 'raw' key with base64 content."""
    from unittest.mock import MagicMock, call, patch
    from app.ingestion.alerts import send_new_booking_alert
    import base64

    booking = _make_booking()
    service = MagicMock()
    service.users().messages().send().execute.return_value = {"id": "sent-1"}

    send_new_booking_alert(booking, alerts_service=service, alerts_address="alerts@example.com",
                           dashboard_base_url="https://example.com")

    send_call = service.users().messages().send.call_args
    body_arg = send_call.kwargs.get("body") or send_call[1].get("body") or send_call[0][0]
    assert "raw" in body_arg
    # Must be valid base64
    base64.urlsafe_b64decode(body_arg["raw"] + "==")


# ---------------------------------------------------------------------------
# build_reminder_alert / send_reminder_alert (Phase 5)
# ---------------------------------------------------------------------------

def test_reminder_alert_phone_7d_subject_action_needed():
    """OWNER_ALERT_MISSING_PHONE_7D: subject uses 'Action needed' framing (D-10)."""
    from app.db.models import TaskType
    from app.ingestion.alerts import build_reminder_alert
    booking = _make_booking(platform_value="airbnb", guest_phone=None)
    subject, _ = build_reminder_alert(
        TaskType.OWNER_ALERT_MISSING_PHONE_7D,
        booking,
        threshold_days=7,
        dashboard_base_url="https://example.com",
    )
    assert "Action needed" in subject
    assert "Test Guest" in subject
    # Check-in date present in subject (Aug 1, 2026)
    assert "Aug 1" in subject or "2026-08-01" in subject


def test_reminder_alert_phone_7d_body_contains_phone_and_airbnb_routing():
    """OWNER_ALERT_MISSING_PHONE_7D: body mentions phone and Airbnb routing (D-09)."""
    from app.db.models import TaskType
    from app.ingestion.alerts import build_reminder_alert
    booking = _make_booking(platform_value="airbnb", guest_phone=None)
    _, body = build_reminder_alert(
        TaskType.OWNER_ALERT_MISSING_PHONE_7D,
        booking,
        threshold_days=7,
        dashboard_base_url="https://example.com",
    )
    assert "phone" in body.lower()
    # Airbnb routing copy: check booking page in the Airbnb app
    assert "airbnb" in body.lower()
    # Dashboard deep link
    assert f"https://example.com/bookings/{booking.id}" in body


def test_reminder_alert_phone_4d_subject_urgent():
    """OWNER_ALERT_MISSING_PHONE_4D: subject uses 'Urgent' framing (D-10)."""
    from app.db.models import TaskType
    from app.ingestion.alerts import build_reminder_alert
    booking = _make_booking(platform_value="airbnb", guest_phone=None)
    subject, body = build_reminder_alert(
        TaskType.OWNER_ALERT_MISSING_PHONE_4D,
        booking,
        threshold_days=4,
        dashboard_base_url="https://example.com",
    )
    assert "Urgent" in subject
    # Body contains phone and Airbnb routing
    assert "phone" in body.lower()
    assert "airbnb" in body.lower()


def test_reminder_alert_email_7d_vrbo_routing():
    """OWNER_ALERT_MISSING_EMAIL_7D on VRBO: subject 'Action needed'; body has VRBO routing (D-09)."""
    from app.db.models import TaskType
    from app.ingestion.alerts import build_reminder_alert
    booking = _make_booking(platform_value="vrbo", guest_email=None)
    subject, body = build_reminder_alert(
        TaskType.OWNER_ALERT_MISSING_EMAIL_7D,
        booking,
        threshold_days=7,
        dashboard_base_url="https://example.com",
    )
    assert "Action needed" in subject
    assert "Test Guest" in subject
    assert "email" in body.lower()
    # VRBO routing copy: look up the booking details page in the VRBO app
    assert "vrbo" in body.lower()
    assert "booking details page" in body.lower() or "vrbo app" in body.lower()
    assert f"https://example.com/bookings/{booking.id}" in body


def test_reminder_alert_email_4d_airbnb_routing():
    """OWNER_ALERT_MISSING_EMAIL_4D on Airbnb: subject 'Urgent'; body has Airbnb routing."""
    from app.db.models import TaskType
    from app.ingestion.alerts import build_reminder_alert
    booking = _make_booking(platform_value="airbnb", guest_email=None)
    subject, body = build_reminder_alert(
        TaskType.OWNER_ALERT_MISSING_EMAIL_4D,
        booking,
        threshold_days=4,
        dashboard_base_url="https://example.com",
    )
    assert "Urgent" in subject
    assert "email" in body.lower()
    # Airbnb routing: ask the guest via Airbnb messaging
    assert "airbnb" in body.lower()
    assert "messaging" in body.lower()
    assert f"https://example.com/bookings/{booking.id}" in body


def test_reminder_alert_docusign_unsigned_7d_action_needed():
    """OWNER_ALERT_DOCUSIGN_UNSIGNED_7D: 'Action needed' subject; D-12 content in body."""
    from app.db.models import TaskType
    from app.ingestion.alerts import build_reminder_alert
    booking = _make_booking(platform_value="airbnb", guest_email="guest@example.com")
    subject, body = build_reminder_alert(
        TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D,
        booking,
        threshold_days=7,
        dashboard_base_url="https://example.com",
    )
    assert "Action needed" in subject
    assert "Test Guest" in subject
    # Body must mention DocuSign and unsigned form
    assert "DocuSign" in body
    # D-12: mention built-in 7-day auto-reminder
    assert ("7-day" in body or "7 day" in body)
    assert "auto" in body.lower()
    # D-12: frame owner chat-nudge as optional
    assert "optional" in body.lower()
    # Dashboard deep link
    assert f"https://example.com/bookings/{booking.id}" in body


def test_reminder_alert_docusign_unsigned_4d_urgent():
    """OWNER_ALERT_DOCUSIGN_UNSIGNED_4D: 'Urgent' subject; DocuSign context still present."""
    from app.db.models import TaskType
    from app.ingestion.alerts import build_reminder_alert
    booking = _make_booking(platform_value="airbnb", guest_email="guest@example.com")
    subject, body = build_reminder_alert(
        TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D,
        booking,
        threshold_days=4,
        dashboard_base_url="https://example.com",
    )
    assert "Urgent" in subject
    assert "DocuSign" in body
    assert f"https://example.com/bookings/{booking.id}" in body


def test_reminder_alert_all_types_have_guest_name_and_checkin_in_subject():
    """All 6 task types include guest name and check-in date in subject."""
    from app.db.models import TaskType
    from app.ingestion.alerts import build_reminder_alert
    reminder_types = [
        (TaskType.OWNER_ALERT_MISSING_PHONE_7D, 7),
        (TaskType.OWNER_ALERT_MISSING_PHONE_4D, 4),
        (TaskType.OWNER_ALERT_MISSING_EMAIL_7D, 7),
        (TaskType.OWNER_ALERT_MISSING_EMAIL_4D, 4),
        (TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D, 7),
        (TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D, 4),
    ]
    booking = _make_booking(platform_value="airbnb", guest_email="guest@example.com")
    for task_type, threshold in reminder_types:
        subject, _ = build_reminder_alert(
            task_type,
            booking,
            threshold_days=threshold,
            dashboard_base_url="https://example.com",
        )
        assert "Test Guest" in subject, f"Guest name missing from subject for {task_type}"
        assert "Aug 1" in subject or "2026-08-01" in subject, (
            f"Check-in date missing from subject for {task_type}"
        )


def test_reminder_alert_all_types_have_dashboard_deep_link_in_body():
    """All 6 task types include dashboard deep link in body."""
    from app.db.models import TaskType
    from app.ingestion.alerts import build_reminder_alert
    reminder_types = [
        (TaskType.OWNER_ALERT_MISSING_PHONE_7D, 7),
        (TaskType.OWNER_ALERT_MISSING_PHONE_4D, 4),
        (TaskType.OWNER_ALERT_MISSING_EMAIL_7D, 7),
        (TaskType.OWNER_ALERT_MISSING_EMAIL_4D, 4),
        (TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D, 7),
        (TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D, 4),
    ]
    booking = _make_booking(platform_value="airbnb", guest_email="guest@example.com")
    for task_type, threshold in reminder_types:
        _, body = build_reminder_alert(
            task_type,
            booking,
            threshold_days=threshold,
            dashboard_base_url="https://example.com",
        )
        expected_link = f"https://example.com/bookings/{booking.id}"
        assert expected_link in body, f"Dashboard link missing from body for {task_type}"


def test_send_reminder_alert_calls_gmail_send():
    """send_reminder_alert sends base64-encoded MIMEText through the alerts Gmail service."""
    from unittest.mock import MagicMock, patch
    from app.db.models import TaskType
    from app.ingestion.alerts import send_reminder_alert
    import base64

    booking = _make_booking(platform_value="airbnb", guest_email="guest@example.com")
    service = MagicMock()

    with patch("app.ingestion.alerts.build_reminder_alert", return_value=("Subject", "Body")):
        send_reminder_alert(
            TaskType.OWNER_ALERT_MISSING_PHONE_7D,
            booking,
            alerts_service=service,
            alerts_address="alerts@example.com",
            dashboard_base_url="https://example.com",
            threshold_days=7,
        )

    service.users.return_value.messages.return_value.send.assert_called_once()
    send_call = service.users.return_value.messages.return_value.send.call_args
    body_arg = send_call.kwargs.get("body") or send_call[1].get("body") or send_call[0][0]
    assert "raw" in body_arg
    # Must be valid base64
    base64.urlsafe_b64decode(body_arg["raw"] + "==")


# ---------------------------------------------------------------------------
# build_cancellation_alert (Step 3) — pure builder
# ---------------------------------------------------------------------------
# On cancellation the owner gets ONE summary email naming the guest, the
# platform + external id, the check-in date, and explicitly listing the two
# ALERT-ONLY manual cleanups (HOA notification + cleaner-sheet row) with the
# dashboard deep link. DocuSign void + Seam delete are automatic and are NOT
# the owner's job, so they must not be listed as manual actions.


def test_cancellation_alert_subject_contains_guest_and_platform():
    from app.ingestion.alerts import build_cancellation_alert
    booking = _make_booking(platform_value="airbnb")
    subject, _ = build_cancellation_alert(booking, dashboard_base_url="https://example.com")
    assert "Test Guest" in subject
    assert "Airbnb" in subject or "AIRBNB" in subject


def test_cancellation_alert_subject_signals_cancellation():
    from app.ingestion.alerts import build_cancellation_alert
    booking = _make_booking()
    subject, _ = build_cancellation_alert(booking, dashboard_base_url="https://example.com")
    assert "cancel" in subject.lower()


def test_cancellation_alert_body_names_guest_platform_externalid_checkin():
    from app.ingestion.alerts import build_cancellation_alert
    booking = _make_booking(platform_value="vrbo")
    _, body = build_cancellation_alert(booking, dashboard_base_url="https://example.com")
    assert "Test Guest" in body
    assert "vrbo" in body.lower() or "VRBO" in body
    assert "HMTEST01" in body  # external_id
    assert "Aug 1" in body or "2026-08-01" in body  # check-in date


def test_cancellation_alert_body_lists_hoa_and_cleaner_manual_cleanups():
    from app.ingestion.alerts import build_cancellation_alert
    booking = _make_booking()
    _, body = build_cancellation_alert(booking, dashboard_base_url="https://example.com")
    assert "HOA" in body
    assert "cleaner" in body.lower()


def test_cancellation_alert_body_contains_dashboard_deep_link():
    from app.ingestion.alerts import build_cancellation_alert
    booking = _make_booking()
    _, body = build_cancellation_alert(booking, dashboard_base_url="https://example.com")
    assert f"https://example.com/bookings/{booking.id}" in body


def test_cancellation_alert_body_does_not_ask_owner_to_void_or_delete_code():
    """DocuSign void + Seam code deletion are AUTOMATIC — must not be listed as manual cleanups."""
    from app.ingestion.alerts import build_cancellation_alert
    booking = _make_booking()
    _, body = build_cancellation_alert(booking, dashboard_base_url="https://example.com")
    lowered = body.lower()
    assert "void" not in lowered
    assert "access code" not in lowered and "seam" not in lowered


# ---------------------------------------------------------------------------
# Operational alert builders (Step 19)
# ---------------------------------------------------------------------------


def test_docusign_webhook_parse_failure_alert_names_keys_and_warns_of_missed_form():
    from app.ingestion.alerts import build_docusign_webhook_parse_failure_alert

    subject, body = build_docusign_webhook_parse_failure_alert(
        payload_keys=["retryCount", "data"],
        dashboard_base_url="https://example.com",
    )
    assert "missed" in subject.lower()
    # Names the observed payload keys so drift is diagnosable from the email.
    assert "retryCount" in body and "data" in body
    # Warns that a signed form / HOA email may not have been processed.
    assert "signed" in body.lower()
    assert "hoa" in body.lower()
    assert "https://example.com/" in body


def test_docusign_webhook_parse_failure_alert_handles_no_keys():
    from app.ingestion.alerts import build_docusign_webhook_parse_failure_alert

    _, body = build_docusign_webhook_parse_failure_alert(
        payload_keys=[], dashboard_base_url="https://example.com"
    )
    assert "(none)" in body


def test_unparseable_email_alert_names_message_and_error():
    from app.ingestion.alerts import build_unparseable_email_alert

    subject, body = build_unparseable_email_alert(
        message_id="abc123",
        classified_as="airbnb_booking",
        error="no confirmation code found",
        dashboard_base_url="https://example.com",
    )
    assert "airbnb_booking" in subject
    assert "abc123" in body
    assert "no confirmation code found" in body
    # Tells the owner the booking was NOT captured and needs manual entry.
    assert "hand" in body.lower() or "manual" in body.lower()


# ---------------------------------------------------------------------------
# F1a (bug hunt 2026-07-22): unprocessable-cancellation alert builder
# ---------------------------------------------------------------------------


def test_cancellation_parse_failure_alert_warns_booking_still_active():
    """A cancellation we cannot apply leaves the booking ACTIVE — the alert must
    say so and put ALL cleanups (including the normally-automatic door code and
    e-sign void) on the owner."""
    from app.ingestion.alerts import build_cancellation_parse_failure_alert

    subject, body = build_cancellation_parse_failure_alert(
        message_id="abc123",
        classified_as="airbnb_cancellation",
        dashboard_base_url="https://example.com",
    )
    assert "cancel" in subject.lower()
    assert "abc123" in body
    assert "airbnb_cancellation" in body
    lowered = body.lower()
    assert "active" in lowered  # booking was NOT cancelled in the system
    assert "manual" in lowered or "by hand" in lowered
    assert "door" in lowered or "access code" in lowered  # code was NOT deleted
    assert "https://example.com/" in body


# ---------------------------------------------------------------------------
# F9 (bug hunt 2026-07-22): booking-alteration alert builder
# ---------------------------------------------------------------------------


def test_booking_alteration_alert_flags_dates_may_be_stale():
    """An alteration email is dead-lettered, not applied — the booking keeps
    its original dates, which still drive the door code, HOA window and
    cleaner row. The alert must say dates may now be wrong and require a
    manual check."""
    from app.ingestion.alerts import build_booking_alteration_alert

    subject, body = build_booking_alteration_alert(
        message_id="abc123",
        classified_as="airbnb_alteration",
        dashboard_base_url="https://example.com",
    )
    assert "abc123" in body
    assert "airbnb_alteration" in body
    lowered = body.lower()
    assert "date" in lowered
    assert "manual" in lowered or "by hand" in lowered
    assert "https://example.com/" in body


def test_stalled_automations_alert_lists_tasks_and_asks_for_action():
    from app.ingestion.alerts import build_stalled_automations_alert

    items = [
        {
            "task": "docusign_send",
            "state": "failed",
            "guest": "Stuck Guest",
            "check_in": "Aug 1, 2026",
            "attempts": 5,
            "last_error": "quota exceeded",
            "booking_url": "https://example.com/bookings/abc",
        },
        {
            "task": "access_code_create",
            "state": "in_progress",
            "guest": "Frozen Guest",
            "check_in": "Aug 3, 2026",
            "attempts": 1,
            "last_error": None,
            "booking_url": "https://example.com/bookings/def",
        },
    ]
    subject, body = build_stalled_automations_alert(
        items, dashboard_base_url="https://example.com"
    )
    assert "automation" in subject.lower()
    assert "Stuck Guest" in body and "Frozen Guest" in body
    assert "docusign_send" in body and "access_code_create" in body
    assert "quota exceeded" in body
    assert "https://example.com/bookings/abc" in body
    # IN_PROGRESS carries the risk-register warning: inspect before resetting.
    assert "inspect" in body.lower()


def test_access_code_problem_alert_names_guest_and_code_state():
    from app.ingestion.alerts import build_access_code_problem_alert

    items = [
        {
            "guest": "Door Guest",
            "check_in": "Aug 1, 2026",
            "status": "setting",
            "problem": "code not yet programmed on the device",
            "booking_url": "https://example.com/bookings/abc",
        }
    ]
    subject, body = build_access_code_problem_alert(
        items, dashboard_base_url="https://example.com"
    )
    assert "code" in subject.lower()
    assert "Door Guest" in body
    assert "setting" in body
    assert "https://example.com/bookings/abc" in body
    lowered = body.lower()
    # The owner's fallback action: check the lock / give the guest a code.
    assert "lock" in lowered
