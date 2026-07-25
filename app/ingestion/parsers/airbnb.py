"""Parse an Airbnb booking confirmation email into structured data.

Airbnb emails (both direct and auto-forwarded) have a consistent text/plain
layout.  Key sections in the body:

    Check-in      Checkout
                  (blank / whitespace line)
    Wed, Jul 15   Sun, Jul 19

    CONFIRMATION CODE
    HMTEST0001

The guest name is extracted from the Subject line:
    "Reservation confirmed - {FULL NAME} arrives {MON} {DAY}"

Dates in Airbnb emails omit the year.  We infer it from the message's Date
header: if the check-in month is >= the message month we stay in the same
year; if it is earlier we roll forward to the next year.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from email.message import Message
from email.utils import parsedate_to_datetime

# Abbreviated month names as they appear in Airbnb text bodies.
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "Wed, Jul 15" — day-of-week is optional in synthetic messages.
_DATE_TOKEN_RE = re.compile(
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*)?"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})",
    re.IGNORECASE,
)

# Subject: "Reservation confirmed - Jane Smith arrives Jul 15"
_SUBJECT_NAME_RE = re.compile(
    r"Reservation confirmed\s*-\s*(.+?)\s+arrives\s+",
    re.IGNORECASE,
)


class AirbnbParseError(ValueError):
    """Raised when a required field cannot be extracted from the email."""


@dataclass(frozen=True)
class AirbnbBookingData:
    confirmation_code: str
    guest_first_name: str
    guest_last_name: str
    check_in_date: date
    check_out_date: date


def _get_text_body(msg: Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            return part.get_content()
    return ""


def _infer_year(month: int, day: int, ref_date: date) -> int:
    """Return the year for a booking date given the message's reference date.

    Day-granular (bug hunt F6): a (month, day) on or after the email's own
    (month, day) is this year; anything earlier can only mean next year —
    Airbnb accepts bookings up to ~12 months out, so an email received Jul 22
    for a check-in "Jul 10" is next July, not 12 days in the past. (A stay
    booked a full year ahead of a LATER month/day remains ambiguous by nature;
    the poller's date sanity guard backstops that.)
    """
    if (month, day) >= (ref_date.month, ref_date.day):
        return ref_date.year
    return ref_date.year + 1


def _parse_date_token(token: str, ref_date: date) -> date:
    m = _DATE_TOKEN_RE.search(token)
    if not m:
        raise AirbnbParseError(f"Cannot parse date token: {repr(token)}")
    month = _MONTHS[m.group(1).lower()]
    day = int(m.group(2))
    year = _infer_year(month, day, ref_date)
    return date(year, month, day)


def parse_airbnb_booking(msg: Message) -> AirbnbBookingData:
    """Parse a Airbnb booking confirmation email.Message into AirbnbBookingData."""
    # --- reference date for year inference ---
    date_header = msg.get("Date", "")
    try:
        ref_date = parsedate_to_datetime(date_header).date()
    except Exception:
        from datetime import date as dt_date
        ref_date = dt_date.today()

    # --- guest name from Subject ---
    subject = msg.get("Subject", "")
    name_match = _SUBJECT_NAME_RE.search(subject)
    if not name_match:
        raise AirbnbParseError(f"Cannot extract guest name from subject: {repr(subject)}")
    full_name = name_match.group(1).strip()
    name_parts = full_name.split()
    guest_first = name_parts[0]
    guest_last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    # --- body parsing ---
    body = _get_text_body(msg)
    lines = [ln.strip() for ln in body.splitlines()]

    # Find "Check-in      Checkout" section then parse the date line below it.
    check_in_date: date | None = None
    check_out_date: date | None = None
    confirmation_code: str | None = None

    for i, line in enumerate(lines):
        # Check-in / checkout dates
        if re.match(r"check-?in\s+check-?out", line, re.IGNORECASE):
            # The date line is the next non-empty, non-whitespace-only line
            for j in range(i + 1, min(i + 5, len(lines))):
                candidate = lines[j]
                if candidate and not candidate.isspace():
                    # Line format: "Wed, Jul 15   Sun, Jul 19"
                    # Split on 3+ spaces (the column separator in Airbnb's text layout)
                    parts = re.split(r"\s{3,}", candidate)
                    if len(parts) >= 2:
                        check_in_date = _parse_date_token(parts[0], ref_date)
                        check_out_date = _parse_date_token(parts[1], ref_date)
                    break

        # Confirmation code
        if line == "CONFIRMATION CODE" and i + 1 < len(lines):
            for j in range(i + 1, min(i + 4, len(lines))):
                code_candidate = lines[j].strip()
                if code_candidate:
                    confirmation_code = code_candidate
                    break

    if confirmation_code is None:
        raise AirbnbParseError("CONFIRMATION CODE section not found in email body")
    if check_in_date is None or check_out_date is None:
        raise AirbnbParseError("Check-in / checkout dates not found in email body")

    return AirbnbBookingData(
        confirmation_code=confirmation_code,
        guest_first_name=guest_first,
        guest_last_name=guest_last,
        check_in_date=check_in_date,
        check_out_date=check_out_date,
    )
