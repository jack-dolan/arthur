"""Build and send alert emails for the booking workflow.

New-booking alerts (step 2.6): sent immediately after a booking is persisted.
Reminder alerts (Phase 5): sent by the daily scheduler for missing phone,
missing email, and unsigned DocuSign form conditions at 7d and 4d thresholds.
"""
from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText
from typing import Any

from app.db.models import TaskType

log = logging.getLogger(__name__)


def build_new_booking_alert(booking: Any, *, dashboard_base_url: str) -> tuple[str, str]:
    """Return (subject, plain-text body) for a new-booking alert.

    *booking* is a ``app.db.models.Booking`` instance.
    """
    platform_label = booking.platform.value.upper()
    guest_name = f"{booking.guest_first_name} {booking.guest_last_name}".strip()
    check_in = booking.check_in_date.strftime("%b %-d, %Y")
    check_out = booking.check_out_date.strftime("%b %-d, %Y")
    detail_url = f"{dashboard_base_url.rstrip('/')}/bookings/{booking.id}"

    subject = f"New {platform_label} Booking — {guest_name} ({check_in})"

    missing: list[str] = []
    if not booking.guest_phone:
        missing.append(
            "  • Phone number — look up the booking page in the "
            f"{'Airbnb' if booking.platform.value == 'airbnb' else 'VRBO'} app"
        )
    if not booking.guest_email:
        missing.append(
            "  • Email address — "
            + (
                "ask the guest via Airbnb messaging and paste their reply into the dashboard"
                if booking.platform.value == "airbnb"
                else "look up the booking details page in the VRBO app"
            )
        )

    lines = [
        f"New booking confirmed on {platform_label}.",
        "",
        f"  Guest:      {guest_name}",
        f"  Check-in:   {check_in}",
        f"  Check-out:  {check_out}",
        "",
        f"  Dashboard:  {detail_url}",
    ]

    if missing:
        lines += ["", "Action needed — enter the following missing fields:", ""] + missing

    body = "\n".join(lines)
    return subject, body


def send_new_booking_alert(
    booking: Any,
    *,
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the new-booking alert via the alerts Gmail account."""
    subject, body = build_new_booking_alert(booking, dashboard_base_url=dashboard_base_url)

    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info("Sent new-booking alert for booking %s", booking.id)


# ---------------------------------------------------------------------------
# Cancellation alert (Step 3)
# ---------------------------------------------------------------------------


def build_cancellation_alert(booking: Any, *, dashboard_base_url: str) -> tuple[str, str]:
    """Return (subject, plain-text body) for a booking-cancellation alert.

    On cancellation the DocuSign envelope is voided and the Seam access code is
    deleted automatically, but the HOA notification and the cleaner-schedule row
    must be cleaned up *by hand* (domain rule: those two are ALERT-ONLY). This
    email names the guest, platform + external id, and check-in date, then spells
    out the two manual cleanups required, with a dashboard deep link.

    *booking* is a ``app.db.models.Booking`` instance.
    """
    platform_label = booking.platform.value.upper()
    guest_name = f"{booking.guest_first_name} {booking.guest_last_name}".strip()
    check_in = booking.check_in_date.strftime("%b %-d, %Y")
    detail_url = f"{dashboard_base_url.rstrip('/')}/bookings/{booking.id}"

    subject = (
        f"Cancelled {platform_label} Booking — {guest_name} ({check_in}) "
        "— manual cleanup needed"
    )

    lines = [
        f"This {platform_label} booking has been cancelled.",
        "",
        f"  Guest:        {guest_name}",
        f"  Platform:     {platform_label} ({booking.external_id})",
        f"  Check-in:     {check_in}",
        "",
        f"  Dashboard:    {detail_url}",
        "",
        "The e-sign form and the door lock code (if any) have been cancelled "
        "automatically — no action needed there.",
        "",
        "Action needed — two manual cleanups are required:",
        "",
        "  • HOA notification — if the HOA was already notified about this "
        "guest's arrival, let them know the stay is cancelled.",
        "  • Cleaner schedule — remove this booking's row from the cleaner "
        "schedule sheet so no clean is scheduled.",
    ]

    body = "\n".join(lines)
    return subject, body


# ---------------------------------------------------------------------------
# Operational alerts (Step 19)
# ---------------------------------------------------------------------------


def build_docusign_webhook_parse_failure_alert(
    *, payload_keys: list[str], dashboard_base_url: str
) -> tuple[str, str]:
    """Return (subject, body) for a DocuSign webhook parse-failure alert (Risk 4).

    Fires when a DocuSign Connect webhook arrives with a VALID HMAC signature but
    the envelope id and/or status could not be extracted — i.e. the payload shape
    differs from what the parser expects. The event is acknowledged (HTTP 200 so
    Connect does not retry-storm) but NOT acted on, so a signed guest form may not
    have been recorded and the HOA email may not have gone out. This alert makes
    that visible instead of a silent drop.
    """
    subject = (
        "DocuSign webhook could not be processed — a signed form may have been missed"
    )
    keys_display = ", ".join(payload_keys) if payload_keys else "(none)"
    lines = [
        "A DocuSign Connect webhook arrived with a VALID signature, but the "
        "envelope id and/or status could not be extracted from its payload.",
        "",
        "This usually means the Connect payload shape differs from what the "
        "automation expects (for example after a Connect configuration change). "
        "The event was acknowledged (HTTP 200) but NOT acted on — so a guest form "
        "that was just signed may not have been recorded, and its HOA email may "
        "not have been sent.",
        "",
        f"  Top-level payload keys: {keys_display}",
        "",
        "Action needed:",
        "  • Check the booking whose guest just signed. If its form is not marked "
        "signed in the dashboard, complete the HOA step manually.",
        "  • Capture the Connect payload shape so the webhook parser can be updated.",
        "",
        f"  Dashboard:  {dashboard_base_url.rstrip('/')}/",
    ]
    return subject, "\n".join(lines)


def send_docusign_webhook_parse_failure_alert(
    *,
    payload_keys: list[str],
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the DocuSign webhook parse-failure alert via the alerts Gmail account."""
    subject, body = build_docusign_webhook_parse_failure_alert(
        payload_keys=payload_keys, dashboard_base_url=dashboard_base_url
    )
    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.warning(
        "Sent DocuSign webhook parse-failure alert (top-level payload keys: %s)",
        payload_keys,
    )


def build_unparseable_email_alert(
    *, message_id: str, classified_as: str, error: str, dashboard_base_url: str
) -> tuple[str, str]:
    """Return (subject, body) for an unparseable booking-email alert (R3b).

    Fires when an incoming email classified as a booking (Airbnb/VRBO) could not
    be parsed — most likely because the platform changed its email format. Left
    unhandled this is a *silent dropped booking*, so the owner is alerted and the
    message is dead-lettered (recorded in ``processed_messages``) rather than
    re-fetched every poll forever.
    """
    subject = f"Booking email could not be read ({classified_as}) — check manually"
    lines = [
        f"An incoming email classified as a {classified_as} could not be parsed.",
        "",
        "This most likely means the platform changed its confirmation-email "
        "format. The message has been recorded so it is NOT re-processed every "
        "poll, but that means the booking was NOT captured automatically.",
        "",
        f"  Gmail message id: {message_id}",
        f"  Parser error:     {error}",
        "",
        "Action needed — open the original email in the booking-feed inbox and "
        "enter the booking by hand, then report the new email format so the "
        "parser can be updated.",
        "",
        f"  Dashboard:  {dashboard_base_url.rstrip('/')}/",
    ]
    return subject, "\n".join(lines)


def send_unparseable_email_alert(
    *,
    message_id: str,
    classified_as: str,
    error: str,
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the unparseable-booking-email alert via the alerts Gmail account."""
    subject, body = build_unparseable_email_alert(
        message_id=message_id,
        classified_as=classified_as,
        error=error,
        dashboard_base_url=dashboard_base_url,
    )
    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.warning(
        "Sent unparseable-email alert for message %s (classified_as=%s)",
        message_id,
        classified_as,
    )


def build_cancellation_parse_failure_alert(
    *, message_id: str, classified_as: str, dashboard_base_url: str
) -> tuple[str, str]:
    """Return (subject, body) for an unprocessable-cancellation alert (F1a).

    Fires when an email classified as a cancellation carries no extractable
    reservation ID, so the automation cannot apply it. Unlike an unknown-booking
    cancellation (which self-heals when the booking arrives, R3a), this can
    never succeed by retrying — and the stakes are high: the booking stays
    ACTIVE in the system, so the door code is NOT deleted, the e-sign envelope
    is NOT voided, and the HOA/cleaner cleanups are never flagged. Every cleanup
    is now the owner's job, so this must be loud.
    """
    subject = (
        f"Cancellation email could not be processed ({classified_as}) — "
        "manual cleanup needed"
    )
    lines = [
        f"An incoming email classified as a {classified_as} did not contain a "
        "readable reservation ID, so the cancellation could NOT be applied.",
        "",
        "The booking is still marked ACTIVE in the system. None of the usual "
        "automatic cleanups have run.",
        "",
        f"  Gmail message id: {message_id}",
        "",
        "Action needed — handle ALL of the following manually:",
        "  • Confirm the cancellation in the Airbnb/VRBO app and identify which "
        "booking it is.",
        "  • Delete the guest's door access code if one was created.",
        "  • Void the e-sign form if it was sent and is unsigned.",
        "  • If the HOA was already notified, let them know the stay is cancelled.",
        "  • Remove the booking's row from the cleaner schedule sheet.",
        "",
        "Then report the email's subject line so the cancellation parser can be "
        "updated.",
        "",
        f"  Dashboard:  {dashboard_base_url.rstrip('/')}/",
    ]
    return subject, "\n".join(lines)


def send_cancellation_parse_failure_alert(
    *,
    message_id: str,
    classified_as: str,
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the unprocessable-cancellation alert via the alerts Gmail account."""
    subject, body = build_cancellation_parse_failure_alert(
        message_id=message_id,
        classified_as=classified_as,
        dashboard_base_url=dashboard_base_url,
    )
    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.warning(
        "Sent unprocessable-cancellation alert for message %s (classified_as=%s)",
        message_id,
        classified_as,
    )


def build_booking_alteration_alert(
    *, message_id: str, classified_as: str, dashboard_base_url: str
) -> tuple[str, str]:
    """Return (subject, body) for a booking-alteration email (F9).

    The classifier only recognizes "reservation confirmed" and cancellation
    subjects; an alteration (date change, guest count change, etc.) is
    dead-lettered rather than applied — the booking keeps its ORIGINAL dates,
    which continue to drive the door code window, the HOA window, the
    cleaner-sheet row and the reminder thresholds. There is no automatic fix
    here (no dashboard affordance to re-date a booking yet), so this must be
    loud and the owner must check the platform directly.
    """
    subject = f"Booking may have changed ({classified_as}) — check dates by hand"
    lines = [
        f"An incoming email classified as a {classified_as} — a possible "
        "date/detail change — was NOT applied automatically.",
        "",
        "The booking in this system still has its ORIGINAL dates. If the "
        "platform changed them, the door code window, HOA window, cleaner "
        "schedule row and reminders are all now based on stale dates.",
        "",
        f"  Gmail message id: {message_id}",
        "",
        "Action needed:",
        "  • Open the reservation in the Airbnb/VRBO app and check whether "
        "the dates or guest count actually changed.",
        "  • If they did, update the booking by hand (there is no automatic "
        "re-date path yet) and double-check the door code, HOA email, and "
        "cleaner sheet row.",
        "",
        f"  Dashboard:  {dashboard_base_url.rstrip('/')}/",
    ]
    return subject, "\n".join(lines)


def send_booking_alteration_alert(
    *,
    message_id: str,
    classified_as: str,
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the booking-alteration alert via the alerts Gmail account."""
    subject, body = build_booking_alteration_alert(
        message_id=message_id,
        classified_as=classified_as,
        dashboard_base_url=dashboard_base_url,
    )
    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.warning(
        "Sent booking-alteration alert for message %s (classified_as=%s)",
        message_id,
        classified_as,
    )


def build_docusign_keepalive_failure_alert(
    *, error: str, dashboard_base_url: str
) -> tuple[str, str]:
    """Return (subject, body) for a failed DocuSign token keep-alive (F4).

    The weekly keep-alive is what stops the 30-day refresh-token expiry. A
    failure is the early warning before every DocuSign call starts 401ing —
    it must reach the owner, not just a log nobody watches.
    """
    subject = "DocuSign token keep-alive failed — action may be needed"
    lines = [
        "The weekly DocuSign refresh-token keep-alive failed.",
        "",
        f"  Error: {error}",
        "",
        "If this keeps failing, the token will expire within 30 days of its "
        "last successful refresh and every DocuSign action (guest form sends, "
        "signed-form downloads) will start failing.",
        "",
        "Action needed if it recurs: re-mint the token with "
        "scripts/manual/get_docusign_refresh_token.py production on the VPS, "
        "then restart the app container.",
        "",
        f"  Dashboard:  {dashboard_base_url.rstrip('/')}/",
    ]
    return subject, "\n".join(lines)


def send_docusign_keepalive_failure_alert(
    *,
    error: str,
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the keep-alive-failure alert via the alerts Gmail account."""
    subject, body = build_docusign_keepalive_failure_alert(
        error=error, dashboard_base_url=dashboard_base_url
    )
    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.warning("Sent DocuSign keep-alive failure alert (%s)", error)


def build_stalled_automations_alert(
    items: list[dict], *, dashboard_base_url: str
) -> tuple[str, str]:
    """Return (subject, body) for the daily stalled-automations digest (F17).

    *items* rows carry: task, state, guest, check_in, attempts, last_error,
    booking_url. Two kinds of entries:
      - FAILED at the retry cap — automatic retries are exhausted; the owner
        must fix the underlying cause or do the step by hand.
      - IN_PROGRESS for >24h — the risk register's crash-window edge; the
        external side effect may or may not have happened, so it must be
        INSPECTED, never blindly reset.
    """
    subject = (
        f"{len(items)} automation task(s) need attention — retries exhausted or stuck"
    )
    lines = [
        "The following automation tasks are stalled and will NOT be retried "
        "automatically:",
        "",
    ]
    for item in items:
        lines.append(
            f"  • {item['task']} — {item['state']} — {item['guest']} "
            f"(check-in {item['check_in']}, attempts {item['attempts']})"
        )
        if item.get("last_error"):
            lines.append(f"      last error: {item['last_error']}")
        lines.append(f"      {item['booking_url']}")
    lines += [
        "",
        "For FAILED tasks: fix the underlying cause (or do the step by hand), "
        "then re-submit the booking's contact form to re-dispatch.",
        "For IN_PROGRESS tasks: inspect the external system FIRST (was the "
        "envelope/door code actually created?) — never assume it didn't run.",
        "",
        f"  Dashboard:  {dashboard_base_url.rstrip('/')}/",
    ]
    return subject, "\n".join(lines)


def send_stalled_automations_alert(
    items: list[dict],
    *,
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the stalled-automations digest via the alerts Gmail account."""
    subject, body = build_stalled_automations_alert(
        items, dashboard_base_url=dashboard_base_url
    )
    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.warning("Sent stalled-automations alert (%d task(s))", len(items))


def build_access_code_problem_alert(
    items: list[dict], *, dashboard_base_url: str
) -> tuple[str, str]:
    """Return (subject, body) for the access-code verification alert (F10).

    Seam provisions codes on the lock ASYNCHRONOUSLY — a successful create
    call does not mean the code reached the device. Each *items* row carries:
    guest, check_in, status, problem, booking_url.
    """
    subject = (
        f"Door access code problem for {len(items)} upcoming booking(s) — check the lock"
    )
    lines = [
        "The daily verification found door access codes that may NOT be working "
        "on the lock:",
        "",
    ]
    for item in items:
        lines.append(
            f"  • {item['guest']} (check-in {item['check_in']}) — "
            f"Seam status: {item['status']}"
        )
        lines.append(f"      problem: {item['problem']}")
        lines.append(f"      {item['booking_url']}")
    lines += [
        "",
        "Action needed: check the lock's Wi-Fi/power and the Seam dashboard. "
        "If the code cannot be fixed before check-in, set a code on the lock "
        "manually (or send the guest a keypad code another way).",
        "",
        f"  Dashboard:  {dashboard_base_url.rstrip('/')}/",
    ]
    return subject, "\n".join(lines)


def send_access_code_problem_alert(
    items: list[dict],
    *,
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the access-code verification alert via the alerts Gmail account."""
    subject, body = build_access_code_problem_alert(
        items, dashboard_base_url=dashboard_base_url
    )
    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.warning("Sent access-code problem alert (%d booking(s))", len(items))


# ---------------------------------------------------------------------------
# Reminder alerts (Phase 5)
# ---------------------------------------------------------------------------

_DOCUSIGN_UNSIGNED_TASK_TYPES = {
    TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D,
    TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D,
}

_PHONE_TASK_TYPES = {
    TaskType.OWNER_ALERT_MISSING_PHONE_7D,
    TaskType.OWNER_ALERT_MISSING_PHONE_4D,
}

_EMAIL_TASK_TYPES = {
    TaskType.OWNER_ALERT_MISSING_EMAIL_7D,
    TaskType.OWNER_ALERT_MISSING_EMAIL_4D,
}

_ALL_REMINDER_TASK_TYPES = (
    _PHONE_TASK_TYPES | _EMAIL_TASK_TYPES | _DOCUSIGN_UNSIGNED_TASK_TYPES
)


def build_reminder_alert(
    task_type: TaskType,
    booking: Any,
    *,
    threshold_days: int,
    dashboard_base_url: str,
) -> tuple[str, str]:
    """Return (subject, plain-text body) for a reminder alert email.

    Handles all 6 reminder task types:
      - OWNER_ALERT_MISSING_PHONE_7D / _4D (D-09, D-10)
      - OWNER_ALERT_MISSING_EMAIL_7D / _4D (D-09, D-10)
      - OWNER_ALERT_DOCUSIGN_UNSIGNED_7D / _4D (D-10, D-12)

    Subject framing per D-10:
      7d: "Action needed — enter missing {field} for {guest} (check-in {date})"
      4d: "Urgent — {field} needed before check-in ({guest}, {date})"
    """
    if task_type not in _ALL_REMINDER_TASK_TYPES:
        raise ValueError(
            f"build_reminder_alert called with non-reminder task type {task_type!r}"
        )

    guest_name = f"{booking.guest_first_name} {booking.guest_last_name}".strip()
    check_in = booking.check_in_date.strftime("%b %-d, %Y")
    detail_url = f"{dashboard_base_url.rstrip('/')}/bookings/{booking.id}"
    is_airbnb = booking.platform.value == "airbnb"

    # Determine field label from task type
    if task_type in _PHONE_TASK_TYPES:
        field = "phone number"
    elif task_type in _EMAIL_TASK_TYPES:
        field = "email address"
    else:
        field = "signed form"

    # Subject framing (D-10)
    if threshold_days <= 4:
        subject = f"Urgent — {field} needed before check-in ({guest_name}, {check_in})"
    else:
        subject = f"Action needed — enter missing {field} for {guest_name} (check-in {check_in})"

    # Build body
    if task_type in _PHONE_TASK_TYPES:
        # Phone reminder body — include platform routing copy (D-09)
        if is_airbnb:
            routing = (
                "  • Phone number — look up the booking page in the Airbnb app"
            )
        else:
            routing = (
                "  • Phone number — look up the booking page in the VRBO app"
            )
        if threshold_days <= 4:
            urgency_note = (
                "This is urgent — the door access code cannot be created without the "
                "guest's phone number and check-in is only a few days away."
            )
        else:
            urgency_note = (
                "The door access code requires the guest's phone number. "
                "Please enter it as soon as possible."
            )
        lines = [
            "Reminder: the guest's phone number is still missing for the following booking.",
            "",
            f"  Guest:      {guest_name}",
            f"  Check-in:   {check_in}",
            "",
            f"  Dashboard:  {detail_url}",
            "",
            urgency_note,
            "",
            "How to find the phone number:",
            routing,
            "",
            "Enter the phone number on the booking's dashboard page to unblock "
            "the access code setup.",
        ]

    elif task_type in _EMAIL_TASK_TYPES:
        # Email reminder body — include platform routing copy (D-09)
        if is_airbnb:
            routing = (
                "  • Email address — "
                "ask the guest via Airbnb messaging and paste their reply into the dashboard"
            )
        else:
            routing = (
                "  • Email address — "
                "look up the booking details page in the VRBO app"
            )
        if threshold_days <= 4:
            urgency_note = (
                "This is urgent — the DocuSign form cannot be sent without the guest's "
                "email address and check-in is only a few days away."
            )
        else:
            urgency_note = (
                "The DocuSign guest form requires the guest's email address. "
                "Please enter it as soon as possible."
            )
        lines = [
            "Reminder: the guest's email address is still missing for the following booking.",
            "",
            f"  Guest:      {guest_name}",
            f"  Check-in:   {check_in}",
            "",
            f"  Dashboard:  {detail_url}",
            "",
            urgency_note,
            "",
            "How to find the email address:",
            routing,
            "",
            "Enter the email address on the booking's dashboard page to unblock the DocuSign send.",
        ]

    else:
        # DocuSign unsigned reminder body (D-12)
        if threshold_days <= 4:
            urgency_note = (
                "This is urgent — the signed form is needed for the HOA notification "
                "and check-in is only a few days away."
            )
        else:
            urgency_note = (
                "The HOA notification requires the signed form. "
                "Please follow up if needed."
            )
        lines = [
            "Reminder: the DocuSign guest form for this booking has not been signed yet.",
            "",
            f"  Guest:      {guest_name}",
            f"  Check-in:   {check_in}",
            "",
            f"  Dashboard:  {detail_url}",
            "",
            urgency_note,
            "",
            (
                "Note: DocuSign's built-in 7-day auto-reminder is configured on this envelope "
                "and will automatically notify the guest by email. You do not need to contact "
                "the guest yourself — reaching out via Airbnb or VRBO messaging is optional "
                "and may result in double-notifying the guest."
            ),
        ]

    body = "\n".join(lines)
    return subject, body


def send_reminder_alert(
    task_type: TaskType,
    booking: Any,
    *,
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
    threshold_days: int,
) -> None:
    """Send a reminder alert email via the alerts Gmail account."""
    subject, body = build_reminder_alert(
        task_type,
        booking,
        threshold_days=threshold_days,
        dashboard_base_url=dashboard_base_url,
    )

    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info(
        "Sent reminder alert %s for booking %s (threshold=%dd)",
        task_type.value,
        booking.id,
        threshold_days,
    )


# ---------------------------------------------------------------------------
# Credential sentinel (sustainability audit 2026-07-23, item 3)
# ---------------------------------------------------------------------------

def build_credential_sentinel_alert(
    *, failures: list[dict], dashboard_base_url: str
) -> tuple[str, str]:
    """Return (subject, body) for the daily credential-sentinel failure alert.

    *failures* rows carry: credential (the check name), error (str). Note this
    email may not arrive if the alerts token itself is what died — the external
    heartbeat monitor (which goes silent on any sentinel failure) is the
    primary alarm; this email is the detail sheet.
    """
    subject = f"{len(failures)} credential check(s) FAILED — integrations at risk"
    lines = [
        "The daily credential sentinel could not verify these credentials:",
        "",
    ]
    for f in failures:
        lines.append(f"  • {f['credential']}: {f['error']}")
    lines += [
        "",
        "Until fixed, the affected integration will fail on its next real use "
        "(booking ingestion, alert email, cleaner sheet, door code, or "
        "dashboard login).",
        "",
        "To repair: open a Claude Code session in the repo and use the "
        "recovering-credentials skill (it has per-integration re-mint steps).",
        "",
        f"  Dashboard:  {dashboard_base_url.rstrip('/')}/",
    ]
    return subject, "\n".join(lines)


def send_credential_sentinel_alert(
    *,
    failures: list[dict],
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the credential-sentinel failure alert via the alerts Gmail account."""
    subject, body = build_credential_sentinel_alert(
        failures=failures, dashboard_base_url=dashboard_base_url
    )
    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.warning("Sent credential sentinel alert (%d failures)", len(failures))


# ---------------------------------------------------------------------------
# Classifier-drift weekly digest (sustainability audit 2026-07-23, item 4)
# ---------------------------------------------------------------------------

def build_classifier_drift_digest(
    *, items: list[dict], dashboard_base_url: str
) -> tuple[str, str]:
    """Return (subject, body) for the weekly classifier-drift review digest.

    *items* rows carry: date, sender, subject — OTHER dead-letters from
    Airbnb/VRBO domains in the past week. Most will be legitimate noise
    (receipts, guest messages, promos); the digest exists because a REWORDED
    booking-confirmation subject would land here silently and the guest would
    arrive with no door code. A human eyeballing subjects catches that.
    """
    subject = (
        f"Weekly review: {len(items)} platform email(s) were not ingested"
    )
    lines = [
        "These emails came from Airbnb/VRBO domains but did not match any "
        "known booking, cancellation, or alteration subject, so they were "
        "recorded as noise (no alert, nothing ingested):",
        "",
    ]
    for item in items:
        lines.append(f"  • {item['date']}  {item['sender']}")
        lines.append(f"      {item['subject']}")
    lines += [
        "",
        "If every subject above is obviously noise (receipts, guest messages, "
        "marketing), delete this email — nothing to do.",
        "",
        "If any looks like a REAL booking/cancellation the platform has "
        "reworded: enter the booking manually via the dashboard now, and "
        "open a Claude Code session with the email as a sample so the "
        "classifier can be fixed (the VRBO format break of 2026-07-05 is the "
        "precedent).",
        "",
        f"  Dashboard:  {dashboard_base_url.rstrip('/')}/",
    ]
    return subject, "\n".join(lines)


def send_classifier_drift_digest(
    *,
    items: list[dict],
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the weekly classifier-drift digest via the alerts Gmail account."""
    subject, body = build_classifier_drift_digest(
        items=items, dashboard_base_url=dashboard_base_url
    )
    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info("Sent classifier-drift digest (%d suspect emails)", len(items))


# ---------------------------------------------------------------------------
# Monthly systems-normal report (sustainability audit 2026-07-23, item 3)
# ---------------------------------------------------------------------------

def build_monthly_status_report(
    *, stats: dict, dashboard_base_url: str
) -> tuple[str, str]:
    """Return (subject, body) for the monthly positive-confirmation email.

    This email is deliberately sent every month even when everything is fine:
    it proves the alerts Gmail token and the send path end-to-end, and its
    absence (noticed by the owner, or by the external monitor's heartbeat
    checks) is itself the alarm.
    """
    attention = stats.get("failed_tasks", 0) + stats.get("stuck_in_progress", 0)
    health = "systems normal" if attention == 0 else "needs attention"
    subject = f"Rental automation monthly status: {health}"

    token_age = stats.get("docusign_token_store_age_days")
    token_line = (
        f"{token_age} day(s) since last rotation"
        if token_age is not None
        else "no rotation persisted yet (running on the .env token)"
    )
    lines = [
        "Monthly status report from the rental automation service.",
        "",
        f"  Active bookings:                {stats.get('active_bookings', 0)}",
        f"  Completed in last 30 days:      {stats.get('completed_last_30d', 0)}",
        f"  FAILED tasks:                   {stats.get('failed_tasks', 0)}",
        f"  Stuck IN_PROGRESS tasks:        {stats.get('stuck_in_progress', 0)}",
        f"  Dead letters, 30d (noise):      {stats.get('dead_letters_other_30d', 0)}",
        f"  Dead letters, 30d (errors):     {stats.get('dead_letters_error_30d', 0)}",
        f"  DocuSign token store:           {token_line}",
        "",
        "If the FAILED / stuck counts are non-zero, check the dashboard and "
        "the stalled-automations digests.",
        "",
        "This email arrives on the 1st of every month. Its ABSENCE means the "
        "app or its alert channel is down — check the uptime monitor and the "
        "heartbeat dashboard.",
        "",
        f"  Dashboard:  {dashboard_base_url.rstrip('/')}/",
    ]
    return subject, "\n".join(lines)


def send_monthly_status_report(
    *,
    stats: dict,
    alerts_service: Any,
    alerts_address: str,
    dashboard_base_url: str,
) -> None:
    """Send the monthly status report via the alerts Gmail account."""
    subject, body = build_monthly_status_report(
        stats=stats, dashboard_base_url=dashboard_base_url
    )
    mime = MIMEText(body, "plain")
    mime["To"] = alerts_address
    mime["From"] = alerts_address
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info("Sent monthly status report")
