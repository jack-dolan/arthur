"""Parse a VRBO booking confirmation email into structured data.

VRBO confirmation emails (direct or manual-forward wrapped) use a labeled
section format in the text/plain body:

    *Reservation ID*
    HA-TESTX1

    *Dates*
    *Jul 2 - Jul 5, 2026*, 3 nights

    *Traveler Name*
    Jane Doe

    *Traveler Phone*
    +1 (555) 123-4567

The year is always present in the *Dates* line, so no year inference is needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from email.message import Message

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Strips markdown-style bold markers (*text*) used by VRBO's text renderer.
_BOLD_RE = re.compile(r"\*([^*]+)\*")

# Matches the dates line: "*Jul 2 - Jul 5, 2026*, 3 nights"
# After bold-stripping: "Jul 2 - Jul 5, 2026, 3 nights"
# The first date may carry its own year ("Dec 30, 2026 - Jan 2, 2027" — the
# cross-year rendering, bug hunt F7); when it doesn't and the stay wraps the
# year boundary, the check-in belongs to the year BEFORE the trailing one.
_DATES_LINE_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})"
    r"(?:,\s*(\d{4}))?"
    r"\s*[-–]\s*"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})"
    r",\s*(\d{4})",
    re.IGNORECASE,
)

# Forward block separator — same as classifier.
_FORWARD_HEADER_RE = re.compile(r"-{5,}\s*Forwarded message\s*-{5,}", re.IGNORECASE)


class VrboParseError(ValueError):
    """Raised when a required field cannot be extracted from the email."""


@dataclass(frozen=True)
class VrboBookingData:
    reservation_id: str
    guest_first_name: str
    guest_last_name: str
    guest_phone: str
    check_in_date: date
    check_out_date: date


def _get_text_body(msg: Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            return part.get_content()
    return ""


def _effective_body(msg: Message) -> str:
    """Return the body of the inner (platform) email, skipping the forward wrapper."""
    body = _get_text_body(msg)
    match = _FORWARD_HEADER_RE.search(body)
    if not match:
        return body
    # Return everything after the forward separator (includes inner headers + body).
    return body[match.end():]


def _strip_bold(s: str) -> str:
    return _BOLD_RE.sub(r"\1", s)


def _labeled_value(lines: list[str], label_pattern: str) -> str | None:
    """Return the first non-empty line after the line matching *label_pattern*.

    The label line is normalised (bold markers stripped, whitespace trimmed, a
    single trailing colon removed) before matching, so both VRBO renderings are
    accepted: the markdown ``*Reservation ID*`` form used by the older fixtures
    and the real-email ``Reservation ID:`` form (trailing colon, value on the
    next indented line). Real VRBO confirmation emails use the colon form; the
    original parser only matched the markdown form and silently dropped real
    bookings (see the Step 20 follow-up regression).
    """
    label_re = re.compile(label_pattern, re.IGNORECASE)
    for i, line in enumerate(lines):
        candidate = _strip_bold(line).strip()
        if candidate.endswith(":"):
            candidate = candidate[:-1].strip()
        if label_re.search(candidate):
            for j in range(i + 1, min(i + 5, len(lines))):
                val = lines[j].strip()
                if val:
                    return _strip_bold(val)
    return None


def parse_vrbo_booking(msg: Message) -> VrboBookingData:
    """Parse a VRBO booking confirmation email.Message into VrboBookingData."""
    body = _effective_body(msg)
    lines = body.splitlines()

    reservation_id = _labeled_value(lines, r"^Reservation ID$")
    if not reservation_id:
        raise VrboParseError("Reservation ID not found in VRBO email body")

    traveler_name = _labeled_value(lines, r"^Traveler Name$")
    if not traveler_name:
        raise VrboParseError("Traveler Name not found in VRBO email body")
    name_parts = traveler_name.split()
    guest_first = name_parts[0]
    guest_last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    guest_phone = _labeled_value(lines, r"^Traveler Phone$")
    if not guest_phone:
        raise VrboParseError("Traveler Phone not found in VRBO email body")

    dates_raw = _labeled_value(lines, r"^Dates$")
    if not dates_raw:
        raise VrboParseError("Dates not found in VRBO email body")
    m = _DATES_LINE_RE.search(_strip_bold(dates_raw))
    if not m:
        raise VrboParseError(f"Cannot parse date range: {repr(dates_raw)}")
    in_month = _MONTHS[m.group(1).lower()]
    in_day = int(m.group(2))
    explicit_in_year = m.group(3)
    out_month = _MONTHS[m.group(4).lower()]
    out_day = int(m.group(5))
    out_year = int(m.group(6))

    if explicit_in_year is not None:
        in_year = int(explicit_in_year)
    elif in_month > out_month:
        # Single trailing year + wrapped months = a New Year's stay: the
        # check-in is in the year BEFORE the (checkout's) trailing year (F7).
        in_year = out_year - 1
    else:
        in_year = out_year

    return VrboBookingData(
        reservation_id=reservation_id,
        guest_first_name=guest_first,
        guest_last_name=guest_last,
        guest_phone=guest_phone,
        check_in_date=date(in_year, in_month, in_day),
        check_out_date=date(out_year, out_month, out_day),
    )
