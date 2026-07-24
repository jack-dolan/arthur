"""HOA email composer and sender.

Builds a MIMEMultipart email with a time-of-day greeting body and an attached
signed PDF, then sends it via the Gmail alerts service.

Time-of-day thresholds (D-10, US/Eastern):
    morning   = 00:00–11:59 ET
    afternoon = 12:00–16:59 ET
    evening   = 17:00–23:59 ET
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def _fmt_hoa_date(d) -> str:
    """Format a check-in date as M/D/YY (no zero padding, 2-digit year), e.g. 7/6/26."""
    return f"{d.month}/{d.day}/{d.strftime('%y')}"


def _time_of_day_greeting(now_et: datetime) -> str:
    """Return a time-of-day greeting string based on US/Eastern hour.

    Thresholds (D-10):
        hour < 12  → "Good morning"
        12 <= hour < 17 → "Good afternoon"
        hour >= 17 → "Good evening"

    *now_et* should be a datetime localised to US/Eastern (or any datetime
    whose ``.hour`` already reflects Eastern time).
    """
    hour = now_et.hour
    if hour < 12:
        return "Good morning"
    elif hour < 17:
        return "Good afternoon"
    else:
        return "Good evening"


def _build_hoa_mime(
    booking: Any,
    *,
    pdf_path: str,
    property_config: Any,
    greeting: str,
    owners_signature_name: str,
    from_address: str,
) -> MIMEMultipart:
    """Build the MIMEMultipart email for the HOA notification.

    Separated from the send step so the MIME construction is independently
    unit-testable (mirrors the ``build_new_booking_alert`` pattern in
    ``app/ingestion/alerts.py``).
    """
    arrival = _fmt_hoa_date(booking.check_in_date)

    msg = MIMEMultipart()
    msg["To"] = property_config.hoa.email
    # A personal display name on the From header (e.g. "Jack Dolan <...>") reads
    # as a real person rather than an anonymous automation, which helps the HOA's
    # spam filter treat it as legitimate mail.
    msg["From"] = (
        f"{owners_signature_name} <{from_address}>"
        if owners_signature_name and from_address
        else from_address
    )
    msg["Subject"] = f"Guest Registration Form - Arriving {arrival}"

    body = (
        f"{greeting},\n\n"
        f"Attached, please find the registration form for my guest, "
        f"arriving {arrival}.\n\n"
        f"Thank you,\n"
        f"{owners_signature_name}"
    )
    msg.attach(MIMEText(body, "plain"))

    with open(pdf_path, "rb") as fh:
        attachment = MIMEApplication(fh.read(), _subtype="pdf")
        attachment.add_header(
            "Content-Disposition",
            "attachment",
            filename=f"guest_form_{booking.id}.pdf",
        )
        msg.attach(attachment)

    return msg


def send_hoa_email(
    booking: Any,
    *,
    pdf_path: str,
    property_config: Any,
    alerts_service: Any,
    owners_signature_name: str = "",
    from_address: str = "",
) -> None:
    """Compose and send the HOA guest-arrival notification email.

    Parameters
    ----------
    booking:
        ``app.db.models.Booking`` instance (or duck-type).
    pdf_path:
        Absolute filesystem path to the signed PDF to attach.
    property_config:
        ``PropertyConfig`` instance; must expose ``hoa.email``.
    alerts_service:
        Authenticated Gmail API service resource (from ``get_alerts_service()``).
    owners_signature_name:
        Name used in the email sign-off and the From display name.  Callers should
        pass ``load_config().owners.signature_name``; defaults to empty string.
    from_address:
        Sender address (``From`` header).  Callers should pass
        ``load_config().email.alerts``; defaults to empty string.
    """
    now_et = datetime.now(ET)
    greeting = _time_of_day_greeting(now_et)

    msg = _build_hoa_mime(
        booking,
        pdf_path=pdf_path,
        property_config=property_config,
        greeting=greeting,
        owners_signature_name=owners_signature_name,
        from_address=from_address,
    )

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    alerts_service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()

    log.info("Sent HOA email for booking %s", booking.id)
