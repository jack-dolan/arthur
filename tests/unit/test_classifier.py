"""Unit tests for the email classifier.

Fixtures cover every known email shape the booking feed inbox can receive:
- Airbnb booking confirmation (direct from Airbnb)
- Airbnb cancellation (manual forward — wrapping stripped before classification)
- VRBO booking confirmation (manual forward)
- VRBO cancellation (manual forward)
- Something unrecognised → EmailType.OTHER
"""
from __future__ import annotations

import email as email_lib
from email import policy

from tests.conftest import recorded_email_message


def load_msg(filename: str):
    # Skips when the gitignored recording is absent — see tests/conftest.py.
    return recorded_email_message(filename)


# ---------------------------------------------------------------------------
# EmailType enum
# ---------------------------------------------------------------------------

def test_email_type_enum_has_all_expected_values():
    from app.ingestion.classifier import EmailType
    assert {e.value for e in EmailType} == {
        "airbnb_booking",
        "airbnb_cancellation",
        "airbnb_alteration",
        "vrbo_booking",
        "vrbo_cancellation",
        "vrbo_alteration",
        "other",
    }


# ---------------------------------------------------------------------------
# classify() on real fixture files
# ---------------------------------------------------------------------------

def test_classify_airbnb_booking_direct():
    from app.ingestion.classifier import EmailType, classify
    result = classify(load_msg("airbnbbooking-1.eml"))
    assert result == EmailType.AIRBNB_BOOKING


def test_classify_airbnb_cancellation_forwarded():
    from app.ingestion.classifier import EmailType, classify
    result = classify(load_msg("airbnb-cancellation-1.eml"))
    assert result == EmailType.AIRBNB_CANCELLATION


def test_classify_vrbo_booking_forwarded_1():
    from app.ingestion.classifier import EmailType, classify
    result = classify(load_msg("vrbo-booking-1.eml"))
    assert result == EmailType.VRBO_BOOKING


def test_classify_vrbo_booking_forwarded_2():
    from app.ingestion.classifier import EmailType, classify
    result = classify(load_msg("vrbo-booking-2.eml"))
    assert result == EmailType.VRBO_BOOKING


def test_classify_vrbo_cancellation_forwarded():
    from app.ingestion.classifier import EmailType, classify
    result = classify(load_msg("vrbo-cancellation-1.eml"))
    assert result == EmailType.VRBO_CANCELLATION


# ---------------------------------------------------------------------------
# classify() on synthetic messages (edge cases / other)
# ---------------------------------------------------------------------------

def _make_msg(from_: str, subject: str, body: str = "Hello") -> object:
    """Build a minimal email.Message for classifier testing."""
    raw = (
        f"From: {from_}\r\n"
        f"Subject: {subject}\r\n"
        f"Content-Type: text/plain\r\n"
        f"\r\n"
        f"{body}"
    )
    return email_lib.message_from_string(raw, policy=policy.default)


def test_classify_returns_other_for_unrecognised_sender():
    from app.ingestion.classifier import EmailType, classify
    msg = _make_msg("noreply@example.com", "Something random")
    assert classify(msg) == EmailType.OTHER


def test_classify_returns_other_for_airbnb_sender_unknown_subject():
    from app.ingestion.classifier import EmailType, classify
    msg = _make_msg("automated@airbnb.com", "Your weekly summary")
    assert classify(msg) == EmailType.OTHER


def test_classify_vrbo_booking_direct_style():
    """Auto-forwarded VRBO booking looks like a direct message from homeaway.com sender."""
    from app.ingestion.classifier import EmailType, classify
    msg = _make_msg(
        "Test Guest <sender@messages.homeaway.com>",
        "Instant Booking from Test Guest: Jul 2 - Jul 5, 2026 - Vrbo #12345678",
    )
    assert classify(msg) == EmailType.VRBO_BOOKING


def test_classify_vrbo_cancellation_direct_style():
    """Auto-forwarded VRBO cancellation from vrbo@partners.expediagroup.com."""
    from app.ingestion.classifier import EmailType, classify
    msg = _make_msg(
        "Vrbo <vrbo@partners.expediagroup.com>",
        "Your reservation HA-TEST01 was canceled at Property 12345678",
    )
    assert classify(msg) == EmailType.VRBO_CANCELLATION


# ---------------------------------------------------------------------------
# F9 (bug hunt 2026-07-22): booking-alteration emails — no real sample yet, so
# this is a deliberately loose heuristic that dead-letters LOUDLY (owner
# alert) rather than falling into the silent OTHER path. Revisit precision
# once a real "reservation changed" email is on hand.
# ---------------------------------------------------------------------------


def test_classify_airbnb_alteration_heuristic():
    from app.ingestion.classifier import EmailType, classify
    msg = _make_msg(
        "Airbnb <automated@airbnb.com>",
        "Reservation updated: Test Guest's trip dates changed",
    )
    assert classify(msg) == EmailType.AIRBNB_ALTERATION


def test_classify_vrbo_alteration_heuristic():
    from app.ingestion.classifier import EmailType, classify
    msg = _make_msg(
        "Vrbo <vrbo@partners.expediagroup.com>",
        "Your reservation HA-TEST01 has been modified",
    )
    assert classify(msg) == EmailType.VRBO_ALTERATION


def test_classify_airbnb_booking_confirmation_not_shadowed_by_alteration_heuristic():
    """'confirmed' emails must never fall into the alteration bucket even
    though guests read as having 'new' details."""
    from app.ingestion.classifier import EmailType, classify
    result = classify(load_msg("airbnbbooking-1.eml"))
    assert result == EmailType.AIRBNB_BOOKING


def test_classify_airbnb_booking_via_manual_forward_structure():
    """Manual forward wrapper: outer From is a Gmail user, inner content is Airbnb booking."""
    from app.ingestion.classifier import EmailType, classify
    inner_body = (
        "---------- Forwarded message ---------\r\n"
        "From: Airbnb <automated@airbnb.com>\r\n"
        "Date: Mon, 18 May 2026 16:21:02 +0000\r\n"
        "Subject: Reservation confirmed - Test Guest arrives Jun 10\r\n"
        "To: cohost@example.com\r\n"
        "\r\n"
        "NEW BOOKING CONFIRMED! TEST GUEST ARRIVES JUN 10.\r\n"
    )
    msg = _make_msg(
        "Co Host <cohost@example.com>",
        "Fwd: Reservation confirmed - Test Guest arrives Jun 10",
        inner_body,
    )
    assert classify(msg) == EmailType.AIRBNB_BOOKING
