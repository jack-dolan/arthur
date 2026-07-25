"""Unit tests for the Airbnb booking confirmation parser.

The parser must return an AirbnbBookingData dataclass with:
  - confirmation_code  (str, e.g. "HMTEST0001")
  - guest_first_name   (str)
  - guest_last_name    (str)
  - check_in_date      (datetime.date)
  - check_out_date     (datetime.date)

It must handle:
  - Direct emails from automated@airbnb.com
  - Manual-forward wrappers (body starts with forwarded-message block)
  - Year inference: "Wed, Jul 15" has no year; derive from the message Date header
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
# AirbnbBookingData shape
# ---------------------------------------------------------------------------

def test_airbnb_booking_data_has_expected_fields():
    from app.ingestion.parsers.airbnb import AirbnbBookingData
    obj = AirbnbBookingData(
        confirmation_code="HMTEST01",
        guest_first_name="Jane",
        guest_last_name="Doe",
        check_in_date=date(2026, 7, 1),
        check_out_date=date(2026, 7, 5),
    )
    assert obj.confirmation_code == "HMTEST01"
    assert obj.guest_first_name == "Jane"
    assert obj.guest_last_name == "Doe"
    assert obj.check_in_date == date(2026, 7, 1)
    assert obj.check_out_date == date(2026, 7, 5)


# ---------------------------------------------------------------------------
# Smoke tests against real fixture (structural checks only — no PII values)
# ---------------------------------------------------------------------------

def test_parse_airbnb_fixture_confirmation_code_is_present():
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    result = parse_airbnb_booking(load_msg("airbnbbooking-1.eml"))
    assert result.confirmation_code and len(result.confirmation_code) >= 8


def test_parse_airbnb_fixture_guest_name_is_present():
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    result = parse_airbnb_booking(load_msg("airbnbbooking-1.eml"))
    assert result.guest_first_name
    assert result.guest_last_name


def test_parse_airbnb_fixture_check_in_date_is_valid():
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    result = parse_airbnb_booking(load_msg("airbnbbooking-1.eml"))
    assert result.check_in_date.year >= 2026
    assert 1 <= result.check_in_date.month <= 12


def test_parse_airbnb_fixture_check_out_after_check_in():
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    result = parse_airbnb_booking(load_msg("airbnbbooking-1.eml"))
    assert result.check_out_date > result.check_in_date


# ---------------------------------------------------------------------------
# Real-format regression (inline — runs on a fresh checkout)
# ---------------------------------------------------------------------------

def _make_airbnb_real_format_msg():
    """Reproduce the layout of a REAL machine-sent Airbnb confirmation.

    Transcribed (values sanitized) from the live email the poller ingested on
    2026-07-08, From ``automated@airbnb.com``.
    Reproduces the parts the parser depends on, plus the surrounding noise it
    must ignore: tracking URLs, the bracketed link lines, the wide-indented
    guest-info column, and the ``4:00 PM / 11:00 AM`` line that follows the
    dates. Note ``Check-in     Checkout`` is padded to the header width and the
    date columns are separated by exactly THREE spaces regardless of whether the
    day number is one or two digits.
    """
    body = (
        "%opentrack%\n"
        "\n"
        "https://www.airbnb.com/?c=.pi80.pkTEST&euid=00000000-0000-0000-0000-000000000000\n"
        "\n"
        "NEW BOOKING CONFIRMED! ALEX ARRIVES NOV 5.\n"
        "\n"
        "Send a message to confirm check-in details or welcome\n"
        "Alex.\n"
        "\n"
        "https://www.airbnb.com/hosting/reservations/details/HMFAKE1234?isPending=true   Alex River"
        "stone\n"
        "                                                                               \n"
        "                                                                               Identity ve"
        "rified · 8 reviews\n"
        "                                                                               \n"
        "                                                                               Somewhere, "
        "NJ\n"
        "\n"
        "COZY TEST CHALET|HOT TUB|SAUNA\n"
        "\n"
        "Entire home/apt\n"
        "\n"
        "Check-in     Checkout\n"
        "             \n"
        "Thu, Nov 5   Sun, Nov 8\n"
        "             \n"
        "4:00 PM      11:00 AM\n"
        "\n"
        "GUESTS\n"
        "\n"
        "3 adults\n"
        "\n"
        "CONFIRMATION CODE\n"
        "HMFAKE1234\n"
        "\n"
        "GUEST PAID\n"
        "\n"
        "$241.67 x 3 nights   $725.00\n"
    )
    raw = (
        "From: Airbnb <automated@airbnb.com>\r\n"
        "Date: Wed, 08 Jul 2026 01:56:31 +0000\r\n"
        "Subject: Reservation confirmed - Alex Riverstone arrives Nov 5\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n" + body
    )
    return email_lib.message_from_string(raw, policy=policy.default)


def test_parse_airbnb_real_format():
    """Regression: pin the REAL machine-sent Airbnb layout.

    The VRBO parser silently dropped a real booking because its committed
    fixture was a *human-forwarded* copy (``From: <a personal gmail>``) whose
    mail client re-rendered the body into a ``*Reservation ID*`` markdown form,
    while real machine-sent VRBO mail uses ``Reservation ID:`` (Step 20
    follow-up). The Airbnb parser never had that flaw — ``airbnbbooking-1.eml``
    is a genuine DKIM-signed ``automated.airbnb.com`` message — but that fixture
    is **gitignored** (PII), so without this test nothing in a fresh checkout
    pins the real format. Verified against the live email: it parses to exactly
    these five fields.

    Covers a SINGLE-digit check-in day (``Thu, Nov 5``), which the fixture
    (``Wed, Jul 15``) and the synthetic tests did not exercise — the column
    separator stays at three spaces either way, so ``\\s{3,}`` splitting holds.
    """
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    result = parse_airbnb_booking(_make_airbnb_real_format_msg())
    assert result.confirmation_code == "HMFAKE1234"
    assert result.guest_first_name == "Alex"
    assert result.guest_last_name == "Riverstone"
    assert result.check_in_date == date(2026, 11, 5)
    assert result.check_out_date == date(2026, 11, 8)


def test_parse_airbnb_real_format_classifies_as_airbnb_booking():
    """The real-format message must also survive classification (poller entry point)."""
    from app.ingestion.classifier import EmailType, classify
    assert classify(_make_airbnb_real_format_msg()) == EmailType.AIRBNB_BOOKING


# ---------------------------------------------------------------------------
# Precise-value tests using synthetic messages (fake data)
# ---------------------------------------------------------------------------

def _make_airbnb_booking_msg_with_code(
    date_header: str,
    check_in_line: str,
    check_out_token: str,
    name: str = "Test Guest",
    code: str = "HMTEST0001",
) -> object:
    """Build a minimal Airbnb-style booking confirmation for precise-value testing."""
    subject = (
        f"Reservation confirmed - {name} arrives "
        f"{check_in_line.split(',')[1].strip() if ',' in check_in_line else check_in_line}"
    )
    body = (
        "NEW BOOKING CONFIRMED!\n\n"
        "Check-in      Checkout\n"
        "              \n"
        f"{check_in_line}   {check_out_token}\n"
        "\n"
        "CONFIRMATION CODE\n"
        f"{code}\n"
    )
    raw = (
        f"From: Airbnb <automated@airbnb.com>\r\n"
        f"Date: {date_header}\r\n"
        f"Subject: {subject}\r\n"
        f"Content-Type: text/plain\r\n"
        f"\r\n"
        f"{body}"
    )
    return email_lib.message_from_string(raw, policy=policy.default)


def test_parse_synthetic_airbnb_confirmation_code():
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    msg = _make_airbnb_booking_msg_with_code(
        date_header="Mon, 18 May 2026 23:00:00 +0000",
        check_in_line="Wed, Jul 15",
        check_out_token="Sun, Jul 19",
        code="HMTEST0001",
    )
    assert parse_airbnb_booking(msg).confirmation_code == "HMTEST0001"


def test_parse_synthetic_airbnb_guest_single_word_last_name():
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    msg = _make_airbnb_booking_msg_with_code(
        date_header="Mon, 18 May 2026 23:00:00 +0000",
        check_in_line="Wed, Jul 15",
        check_out_token="Sun, Jul 19",
        name="Jane Doe",
    )
    result = parse_airbnb_booking(msg)
    assert result.guest_first_name == "Jane"
    assert result.guest_last_name == "Doe"


def test_parse_synthetic_airbnb_guest_compound_last_name():
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    msg = _make_airbnb_booking_msg_with_code(
        date_header="Mon, 18 May 2026 23:00:00 +0000",
        check_in_line="Wed, Jul 15",
        check_out_token="Sun, Jul 19",
        name="Jane Van Der Berg",
    )
    result = parse_airbnb_booking(msg)
    assert result.guest_first_name == "Jane"
    assert result.guest_last_name == "Van Der Berg"


# ---------------------------------------------------------------------------
# Year inference edge cases (synthetic messages)
# ---------------------------------------------------------------------------

def test_year_inference_same_year():
    """Check-in month (Jul) is after message month (May) — same year."""
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    msg = _make_airbnb_booking_msg_with_code(
        date_header="Mon, 18 May 2026 23:00:00 +0000",
        check_in_line="Wed, Jul 15",
        check_out_token="Sun, Jul 19",
    )
    result = parse_airbnb_booking(msg)
    assert result.check_in_date == date(2026, 7, 15)
    assert result.check_out_date == date(2026, 7, 19)


def test_year_inference_wraps_to_next_year():
    """Check-in month (Jan) is before message month (Dec) — next year."""
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    msg = _make_airbnb_booking_msg_with_code(
        date_header="Mon, 15 Dec 2026 10:00:00 +0000",
        check_in_line="Fri, Jan 2",
        check_out_token="Mon, Jan 5",
    )
    result = parse_airbnb_booking(msg)
    assert result.check_in_date == date(2027, 1, 2)
    assert result.check_out_date == date(2027, 1, 5)


def test_year_inference_same_month_uses_same_year():
    """Check-in in same month as message — same year."""
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    msg = _make_airbnb_booking_msg_with_code(
        date_header="Mon, 1 Jun 2026 10:00:00 +0000",
        check_in_line="Fri, Jun 20",
        check_out_token="Mon, Jun 23",
    )
    result = parse_airbnb_booking(msg)
    assert result.check_in_date == date(2026, 6, 20)


# ---------------------------------------------------------------------------
# Raises on unparseable input
# ---------------------------------------------------------------------------

def test_parse_raises_on_missing_confirmation_code():
    from app.ingestion.parsers.airbnb import AirbnbParseError, parse_airbnb_booking
    raw = (
        "From: Airbnb <automated@airbnb.com>\r\n"
        "Date: Mon, 18 May 2026 23:00:00 +0000\r\n"
        "Subject: Reservation confirmed - Test Guest arrives Jul 15\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "Check-in      Checkout\n"
        "Wed, Jul 15   Sun, Jul 19\n"
        # no CONFIRMATION CODE section
    )
    msg = email_lib.message_from_string(raw, policy=policy.default)
    with pytest.raises(AirbnbParseError):
        parse_airbnb_booking(msg)


def test_year_inference_same_month_earlier_day_rolls_to_next_year():
    """F6 (bug hunt 2026-07-22): an email received Jul 22 for a check-in
    'Jul 10' can only mean NEXT July (Airbnb books up to ~12 months out).
    Month-granular inference dated it 12 days in the PAST, which silently
    disabled reminders, mis-sorted the cleaner sheet, and produced a
    year-wrong access-code window."""
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    msg = _make_airbnb_booking_msg_with_code(
        date_header="Wed, 22 Jul 2026 15:00:00 +0000",
        check_in_line="Fri, Jul 10",
        check_out_token="Sun, Jul 12",
    )
    result = parse_airbnb_booking(msg)
    assert result.check_in_date == date(2027, 7, 10)
    assert result.check_out_date == date(2027, 7, 12)


def test_year_inference_same_day_as_message_stays_same_year():
    """A same-day check-in (booked the day of arrival) stays in this year."""
    from app.ingestion.parsers.airbnb import parse_airbnb_booking
    msg = _make_airbnb_booking_msg_with_code(
        date_header="Wed, 22 Jul 2026 09:00:00 +0000",
        check_in_line="Wed, Jul 22",
        check_out_token="Fri, Jul 24",
    )
    result = parse_airbnb_booking(msg)
    assert result.check_in_date == date(2026, 7, 22)
