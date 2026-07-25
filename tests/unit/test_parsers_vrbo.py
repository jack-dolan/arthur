"""Unit tests for the VRBO booking confirmation parser.

The parser must return a VrboBookingData dataclass with:
  - reservation_id    (str, e.g. "HA-TESTX1")
  - guest_first_name  (str)
  - guest_last_name   (str)
  - guest_phone       (str — raw as it appears in the email)
  - check_in_date     (datetime.date)
  - check_out_date    (datetime.date)

VRBO emails (including manual forwards) include the year in the dates line,
so no year inference is needed.

Both fixture files are tested to confirm the parser is robust across two
real VRBO booking emails.
"""
from __future__ import annotations

import email as email_lib
from datetime import date
from email import policy

import pytest

from tests.conftest import recorded_email_message


def load_msg(filename: str):
    # Skips when the gitignored recording is absent — see tests/conftest.py.
    return recorded_email_message(filename)


# ---------------------------------------------------------------------------
# VrboBookingData shape
# ---------------------------------------------------------------------------

def test_vrbo_booking_data_has_expected_fields():
    from app.ingestion.parsers.vrbo import VrboBookingData
    obj = VrboBookingData(
        reservation_id="HA-TEST01",
        guest_first_name="Jane",
        guest_last_name="Doe",
        guest_phone="+15551234567",
        check_in_date=date(2026, 7, 2),
        check_out_date=date(2026, 7, 5),
    )
    assert obj.reservation_id == "HA-TEST01"
    assert obj.guest_first_name == "Jane"
    assert obj.guest_last_name == "Doe"
    assert obj.guest_phone == "+15551234567"
    assert obj.check_in_date == date(2026, 7, 2)
    assert obj.check_out_date == date(2026, 7, 5)


# ---------------------------------------------------------------------------
# Smoke tests against real fixtures (structural checks only — no PII values)
# ---------------------------------------------------------------------------

def test_parse_vrbo_fixture_1_has_expected_shape():
    from app.ingestion.parsers.vrbo import parse_vrbo_booking
    result = parse_vrbo_booking(load_msg("vrbo-booking-1.eml"))
    assert result.reservation_id and result.reservation_id.startswith("HA-")
    assert result.guest_first_name
    assert result.guest_last_name
    assert result.guest_phone
    assert result.check_in_date < result.check_out_date


def test_parse_vrbo_fixture_2_has_expected_shape():
    from app.ingestion.parsers.vrbo import parse_vrbo_booking
    result = parse_vrbo_booking(load_msg("vrbo-booking-2.eml"))
    assert result.reservation_id and result.reservation_id.startswith("HA-")
    assert result.guest_first_name
    assert result.guest_last_name
    assert result.guest_phone
    assert result.check_in_date < result.check_out_date


def _make_vrbo_real_format_msg() -> object:
    """Build a VRBO booking confirmation in the REAL email layout.

    Real VRBO confirmation emails label each field on its own line with a
    trailing colon and put the value on the next, indented line — e.g.::

        Reservation ID:
                HA-RLFMT1

    This differs from the ``*Reservation ID*`` markdown form the earlier
    fixtures/synthetic tests used. Built inline (not from a fixture file) so the
    regression is committed and runs on a fresh checkout — the real ``.eml``
    fixtures dir is gitignored (contains PII). Sanitized values only.
    """
    body = (
        "Your booking is confirmed\r\n\r\n"
        "Property:\r\n        #1234567\r\n"
        "Unit:\r\n        unit_7654321\r\n"
        "Reservation ID:\r\n        HA-RLFMT1\r\n"
        "Dates:\r\n        Jul 13 - Jul 16, 2026, 3 nights\r\n"
        "Guests:\r\n        4 adults, 3 children\r\n"
        "Traveler Name:\r\n        Jordan Rivera\r\n"
        "Traveler Phone:\r\n        +15557654321\r\n"
        "Payment Method:\r\n        Visa\r\n"
    )
    raw = (
        "From: Jordan Rivera <sender@messages.homeaway.com>\r\n"
        "Subject: Instant Booking from Jordan Rivera: Jul 13 - Jul 16, 2026 - Vrbo #1234567\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n" + body
    )
    return email_lib.message_from_string(raw, policy=policy.default)


def test_parse_vrbo_real_colon_label_format():
    """Regression: real VRBO emails label fields with a trailing colon and put
    the value on the next indented line (``Reservation ID:`` / ``<indent>HA-...``),
    NOT the ``*Reservation ID*`` markdown form the older fixtures used. A real
    booking in this format was silently dropped in production (parser raised
    "Reservation ID not found") and fired an owner alert — see Step 20 follow-up.
    """
    from app.ingestion.parsers.vrbo import parse_vrbo_booking
    result = parse_vrbo_booking(_make_vrbo_real_format_msg())
    assert result.reservation_id == "HA-RLFMT1"
    assert result.guest_first_name == "Jordan"
    assert result.guest_last_name == "Rivera"
    assert result.guest_phone == "+15557654321"
    assert result.check_in_date == date(2026, 7, 13)
    assert result.check_out_date == date(2026, 7, 16)


# ---------------------------------------------------------------------------
# Precise-value tests using synthetic messages (fake data)
# ---------------------------------------------------------------------------

def _make_vrbo_booking_msg(
    reservation_id: str = "HA-TESTX1",
    name: str = "Jane Doe",
    phone: str = "+15551234567",
    dates_line: str = "Jul 2 - Jul 5, 2026",
    nights: str = "3 nights",
) -> object:
    """Build a minimal VRBO-style direct booking confirmation for precise-value testing."""
    body = (
        f"*Reservation ID*\r\n{reservation_id}\r\n\r\n"
        f"*Dates*\r\n*{dates_line}*, {nights}\r\n\r\n"
        f"*Traveler Name*\r\n{name}\r\n\r\n"
        f"*Traveler Phone*\r\n{phone}\r\n"
    )
    raw = (
        "From: vrbo@partners.expediagroup.com\r\n"
        "Subject: Instant Booking\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n" + body
    )
    return email_lib.message_from_string(raw, policy=policy.default)


def test_parse_synthetic_vrbo_reservation_id():
    from app.ingestion.parsers.vrbo import parse_vrbo_booking
    msg = _make_vrbo_booking_msg(reservation_id="HA-TESTX1")
    assert parse_vrbo_booking(msg).reservation_id == "HA-TESTX1"


def test_parse_synthetic_vrbo_guest_name():
    from app.ingestion.parsers.vrbo import parse_vrbo_booking
    msg = _make_vrbo_booking_msg(name="Jane Doe")
    result = parse_vrbo_booking(msg)
    assert result.guest_first_name == "Jane"
    assert result.guest_last_name == "Doe"


def test_parse_synthetic_vrbo_guest_compound_last_name():
    from app.ingestion.parsers.vrbo import parse_vrbo_booking
    msg = _make_vrbo_booking_msg(name="Jane Van Der Berg")
    result = parse_vrbo_booking(msg)
    assert result.guest_first_name == "Jane"
    assert result.guest_last_name == "Van Der Berg"


def test_parse_synthetic_vrbo_phone():
    from app.ingestion.parsers.vrbo import parse_vrbo_booking
    msg = _make_vrbo_booking_msg(phone="+15559876543")
    assert parse_vrbo_booking(msg).guest_phone == "+15559876543"


def test_parse_synthetic_vrbo_dates():
    from app.ingestion.parsers.vrbo import parse_vrbo_booking
    msg = _make_vrbo_booking_msg(dates_line="Aug 10 - Aug 14, 2026", nights="4 nights")
    result = parse_vrbo_booking(msg)
    assert result.check_in_date == date(2026, 8, 10)
    assert result.check_out_date == date(2026, 8, 14)


def test_parse_synthetic_vrbo_dates_cross_month():
    from app.ingestion.parsers.vrbo import parse_vrbo_booking
    msg = _make_vrbo_booking_msg(dates_line="Mar 31 - Apr 2, 2026", nights="2 nights")
    result = parse_vrbo_booking(msg)
    assert result.check_in_date == date(2026, 3, 31)
    assert result.check_out_date == date(2026, 4, 2)


# ---------------------------------------------------------------------------
# Raises on unparseable input
# ---------------------------------------------------------------------------

def test_parse_raises_on_missing_reservation_id():
    from app.ingestion.parsers.vrbo import VrboParseError, parse_vrbo_booking
    body = (
        "---------- Forwarded message ---------\r\n"
        "From: Test Guest <sender@messages.homeaway.com>\r\n"
        "Subject: Instant Booking from Test Guest: Jul 2 - Jul 5, 2026\r\n"
        "\r\n"
        "*Traveler Name*\r\nTest Guest\r\n\r\n"
        "*Traveler Phone*\r\n+15551234567\r\n\r\n"
        "*Dates*\r\n*Jul 2 - Jul 5, 2026*, 3 nights\r\n"
        # no *Reservation ID* block
    )
    raw = (
        "From: forwarder@example.com\r\n"
        "Subject: Fwd: Instant Booking...\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n" + body
    )
    msg = email_lib.message_from_string(raw, policy=policy.default)
    with pytest.raises(VrboParseError):
        parse_vrbo_booking(msg)


# ---------------------------------------------------------------------------
# F7 (bug hunt 2026-07-22): cross-year stays
# ---------------------------------------------------------------------------


def _vrbo_msg_with_dates(dates_line: str):
    import email as email_lib
    from email import policy

    body = (
        "*Reservation ID*\n"
        "HA-XYEAR1\n"
        "\n"
        "*Dates*\n"
        f"*{dates_line}*, 3 nights\n"
        "\n"
        "*Traveler Name*\n"
        "New Year Guest\n"
        "\n"
        "*Traveler Phone*\n"
        "+1 (555) 123-4567\n"
    )
    raw = (
        "From: Vrbo <vrbo@partners.expediagroup.com>\r\n"
        "Subject: Instant Booking from New Year Guest\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n" + body
    )
    return email_lib.message_from_string(raw, policy=policy.default)


def test_vrbo_cross_year_stay_checkin_gets_previous_year():
    """'Dec 30 - Jan 2, 2027' is a New Year's stay: check-in Dec 30 *2026*.
    The single trailing year used to be applied to BOTH dates, producing a
    check-in a year in the future and check_out < check_in."""
    from datetime import date

    from app.ingestion.parsers.vrbo import parse_vrbo_booking

    result = parse_vrbo_booking(_vrbo_msg_with_dates("Dec 30 - Jan 2, 2027"))
    assert result.check_in_date == date(2026, 12, 30)
    assert result.check_out_date == date(2027, 1, 2)


def test_vrbo_both_years_rendering_parses():
    """The explicit two-year rendering must parse too (it previously failed
    to match the regex entirely)."""
    from datetime import date

    from app.ingestion.parsers.vrbo import parse_vrbo_booking

    result = parse_vrbo_booking(_vrbo_msg_with_dates("Dec 30, 2026 - Jan 2, 2027"))
    assert result.check_in_date == date(2026, 12, 30)
    assert result.check_out_date == date(2027, 1, 2)
