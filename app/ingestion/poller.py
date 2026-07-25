"""Gmail booking-feed poller.

Polls the booking feed inbox every 5 minutes via the Gmail API and processes
any message not yet recorded in the database.

Public functions used by the scheduler and tests:
  fetch_unprocessed_message_ids(service, already_seen) -> list[str]
  fetch_raw_message(service, msg_id) -> dict
  raw_payload_to_message(payload) -> email.Message
  process_message(msg_id, payload) -> None  (async)
  persist_booking(msg_id, msg, parsed, platform) -> None  (async)
  handle_cancellation(msg_id, msg, platform) -> None  (async, stub for 2.7)

The APScheduler job (poll_booking_feed) ties these together and is registered
in app.main via the FastAPI lifespan.
"""
from __future__ import annotations

import asyncio
import base64
import email as email_lib
import logging
from datetime import UTC, datetime
from email import policy
from email.message import Message
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Booking,
    BookingStatus,
    BookingTask,
    DataPoint,
    DataPointSource,
    Platform,
    ProcessedMessage,
    TaskState,
    TaskType,
)
from app.db.session import AsyncSessionLocal
from app.ingestion.alerts import (
    send_booking_alteration_alert,
    send_cancellation_parse_failure_alert,
    send_unparseable_email_alert,
)
from app.ingestion.classifier import EmailType, _effective_sender_and_subject, classify
from app.ingestion.parsers.airbnb import AirbnbBookingData, parse_airbnb_booking
from app.ingestion.parsers.vrbo import VrboBookingData, parse_vrbo_booking
from app.integrations.gmail.oauth import get_alerts_service
from app.monitoring import ping_heartbeat_async

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gmail API helpers
# ---------------------------------------------------------------------------

def fetch_unprocessed_message_ids(
    service: Any, already_seen: set[str], *, max_messages: int = 500
) -> list[str]:
    """Return message IDs from INBOX that are not in *already_seen*.

    Paginates (F16, bug hunt 2026-07-22): a single default page holds 100 ids
    newest-first, so any unseen backlog beyond it — e.g. after a multi-day
    outage on a noisy inbox — was silently orphaned (new mail keeps it pushed
    past page 1 forever). Capped at *max_messages* raw ids per poll to bound
    the work; the cap is logged when hit.
    """
    ids: list[str] = []
    page_token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"userId": "me", "labelIds": ["INBOX"]}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        ids.extend(m["id"] for m in result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
        if len(ids) >= max_messages:
            log.warning(
                "fetch_unprocessed_message_ids: hit the %d-message page cap with "
                "more pages remaining; the rest will be picked up next poll",
                max_messages,
            )
            break
    return [i for i in ids if i not in already_seen]


def fetch_raw_message(service: Any, msg_id: str) -> dict:
    """Fetch a single message in raw (RFC 2822) format."""
    return service.users().messages().get(userId="me", id=msg_id, format="raw").execute()


def raw_payload_to_message(payload: dict) -> Message:
    """Decode a Gmail API raw payload dict into an email.Message."""
    raw_bytes = base64.urlsafe_b64decode(payload["raw"] + "==")
    return email_lib.message_from_bytes(raw_bytes, policy=policy.default)


# ---------------------------------------------------------------------------
# Per-message processing
# ---------------------------------------------------------------------------

async def process_message(msg_id: str, payload: dict) -> None:
    """Classify and dispatch a single Gmail message."""
    # F1d (bug hunt 2026-07-22): an exception while decoding or classifying used
    # to escape to the poll loop's catch-all, so the message was never recorded
    # and was re-fetched every 5 minutes forever. Treat it as terminal: dead-
    # letter + owner alert (it might have been a booking we can't read).
    try:
        msg = raw_payload_to_message(payload)
        email_type = classify(msg)
    except Exception as exc:  # noqa: BLE001 — any classify failure is terminal
        log.error("Failed to decode/classify message %s: %s", msg_id, exc)
        await _record_processed_message(msg_id, "classify_error", error=str(exc))
        try:
            from app.config import load_config
            from app.settings import settings

            config = load_config()
            await asyncio.to_thread(  # F11: sync Gmail send off the loop
                send_unparseable_email_alert,
                message_id=msg_id,
                classified_as="unclassifiable (decode/classify failed)",
                error=str(exc),
                alerts_service=get_alerts_service(),
                alerts_address=config.email.alerts,
                dashboard_base_url=f"https://{settings.domain}",
            )
        except Exception as alert_exc:  # noqa: BLE001 — alert must be non-fatal
            log.error(
                "Failed to send classify-failure alert for %s: %s", msg_id, alert_exc
            )
        return

    if email_type == EmailType.AIRBNB_BOOKING:
        try:
            parsed = parse_airbnb_booking(msg)
        except Exception as exc:  # noqa: BLE001 — any parse failure is terminal
            await _handle_parse_failure(msg_id, email_type, exc)
            return
        date_error = _implausible_dates(parsed, msg)
        if date_error:
            await _handle_parse_failure(msg_id, email_type, ValueError(date_error))
            return
        await persist_booking(msg_id, msg, parsed, Platform.AIRBNB)

    elif email_type == EmailType.VRBO_BOOKING:
        try:
            parsed = parse_vrbo_booking(msg)
        except Exception as exc:  # noqa: BLE001 — any parse failure is terminal
            await _handle_parse_failure(msg_id, email_type, exc)
            return
        date_error = _implausible_dates(parsed, msg)
        if date_error:
            await _handle_parse_failure(msg_id, email_type, ValueError(date_error))
            return
        await persist_booking(msg_id, msg, parsed, Platform.VRBO)

    elif email_type in (EmailType.AIRBNB_CANCELLATION, EmailType.VRBO_CANCELLATION):
        platform = (
            Platform.AIRBNB
            if email_type == EmailType.AIRBNB_CANCELLATION
            else Platform.VRBO
        )
        # NB: a cancellation whose booking does not exist yet is deliberately NOT
        # dead-lettered here — that case self-heals once the booking arrives, so
        # it must keep being retried (R3a). But a cancellation whose external id
        # cannot be EXTRACTED can never succeed by retrying — that case is
        # terminal and must be dead-lettered + alerted (F1a), or it replays
        # every poll forever while the booking silently stays ACTIVE.
        from app.ingestion.cancellation import parse_cancellation_external_id

        if parse_cancellation_external_id(msg, platform) is None:
            await _handle_cancellation_parse_failure(msg_id, email_type)
            return
        await handle_cancellation(msg_id, msg, platform)

    elif email_type in (EmailType.AIRBNB_ALTERATION, EmailType.VRBO_ALTERATION):
        await _handle_alteration(msg_id, email_type)

    else:
        # EmailType.OTHER — not a booking or cancellation. Dead-letter it so the
        # poller does not re-fetch it every 5 minutes forever (R3b). No owner
        # alert: OTHER is expected inbox noise, not a dropped booking — but the
        # sender/subject are recorded so the weekly classifier-drift digest can
        # surface platform-domain OTHER mail for human review (a reworded
        # booking subject would land here silently otherwise).
        log.info("Message %s classified as OTHER; recording as processed", msg_id)
        sender, subject = _effective_sender_and_subject(msg)
        await _record_processed_message(msg_id, "other", sender=sender, subject=subject)


def _implausible_dates(
    parsed: "AirbnbBookingData | VrboBookingData", msg: Message
) -> str | None:
    """Return an error string when parsed booking dates cannot be real (F6/F7).

    The invariant is anchored to the email's OWN Date header (not today): a
    confirmation can never announce a check-in before it was sent, nor beyond
    the platforms' ~1-year booking horizon — so a violation means a parser bug
    (e.g. year mis-inference) or platform format drift, never a legitimate
    booking. Persisting it would silently drive a wrong lock-code window, a
    mis-sorted cleaner row and dead reminders, so such messages are routed
    through the parse-failure path (dead-letter + owner alert) and entered by
    hand. Anchoring to the header also keeps backfilling old emails legitimate.

    Slack: 3 days before the email date (timezone edges around a same-day
    booking), 2 years after (beyond any platform's booking horizon).
    """
    from datetime import timedelta
    from email.utils import parsedate_to_datetime

    from app.integrations.hoa.window import today_et

    try:
        ref = parsedate_to_datetime(msg.get("Date", "")).date()
    except Exception:  # noqa: BLE001 — missing/garbled header: fall back to today
        ref = today_et()

    if parsed.check_out_date <= parsed.check_in_date:
        return (
            f"check-out {parsed.check_out_date} is not after "
            f"check-in {parsed.check_in_date}"
        )
    if parsed.check_in_date < ref - timedelta(days=3):
        return (
            f"check-in {parsed.check_in_date} is before the email's own "
            f"date ({ref})"
        )
    if parsed.check_in_date > ref + timedelta(days=730):
        return (
            f"check-in {parsed.check_in_date} is over 2 years after the "
            f"email's date ({ref})"
        )
    return None


async def _record_processed_message(
    msg_id: str,
    disposition: str,
    *,
    classified_as: str | None = None,
    error: str | None = None,
    sender: str | None = None,
    subject: str | None = None,
) -> None:
    """Dead-letter a message so it is not re-polled forever (R3b).

    Idempotent: the UNIQUE constraint on ``message_id`` backstops a concurrent or
    repeated record (the row is simply left as-is).
    """
    async with AsyncSessionLocal() as session:
        session.add(
            ProcessedMessage(
                message_id=msg_id,
                disposition=disposition,
                classified_as=classified_as,
                error=error,
                sender=sender,
                subject=subject,
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            log.debug("Message %s already recorded as processed", msg_id)


async def _handle_parse_failure(
    msg_id: str, email_type: EmailType, exc: Exception
) -> None:
    """Handle a booking email that classified as a booking but would not parse.

    A booking email we cannot parse is a *silently dropped booking* (most likely
    the platform changed its email format). We dead-letter it (so it is not
    re-fetched every poll) AND alert the owner so the drop is visible and can be
    entered by hand (R3b).
    """
    log.error("Failed to parse %s message %s: %s", email_type.value, msg_id, exc)
    await _record_processed_message(
        msg_id, "parse_error", classified_as=email_type.value, error=str(exc)
    )
    try:
        from app.config import load_config
        from app.settings import settings

        config = load_config()
        await asyncio.to_thread(  # F11: sync Gmail send off the loop
            send_unparseable_email_alert,
            message_id=msg_id,
            classified_as=email_type.value,
            error=str(exc),
            alerts_service=get_alerts_service(),
            alerts_address=config.email.alerts,
            dashboard_base_url=f"https://{settings.domain}",
        )
    except Exception as alert_exc:  # noqa: BLE001 — alert failure must be non-fatal
        log.error(
            "Failed to send unparseable-email alert for %s: %s", msg_id, alert_exc
        )


async def _handle_alteration(msg_id: str, email_type: EmailType) -> None:
    """Handle a booking-alteration email (date/detail change) — F9.

    Not applied automatically (no real sample was available to write a
    precise re-date rule against, and there is no dashboard affordance to
    re-date a booking yet). Dead-letter so it is not re-fetched every poll,
    and alert the owner loudly: the booking keeps its original dates, which
    keep driving the door code window, HOA window, cleaner row and reminders.
    """
    log.warning(
        "Message %s classified as %s (possible booking alteration); "
        "dead-lettering and alerting owner",
        msg_id,
        email_type.value,
    )
    await _record_processed_message(
        msg_id, "alteration", classified_as=email_type.value
    )
    try:
        from app.config import load_config
        from app.settings import settings

        config = load_config()
        await asyncio.to_thread(  # F11: sync Gmail send off the loop
            send_booking_alteration_alert,
            message_id=msg_id,
            classified_as=email_type.value,
            alerts_service=get_alerts_service(),
            alerts_address=config.email.alerts,
            dashboard_base_url=f"https://{settings.domain}",
        )
    except Exception as alert_exc:  # noqa: BLE001 — alert failure must be non-fatal
        log.error(
            "Failed to send booking-alteration alert for %s: %s", msg_id, alert_exc
        )


async def _handle_cancellation_parse_failure(msg_id: str, email_type: EmailType) -> None:
    """Handle a cancellation email whose reservation id cannot be extracted (F1a).

    Unlike the unknown-booking case (R3a, retried on purpose), an unextractable
    id is permanent: dead-letter the message so it is not re-fetched forever,
    and alert the owner loudly — the booking stays ACTIVE in the system, so the
    door code, envelope, HOA and cleaner cleanups are all manual now.
    """
    log.error(
        "Cancellation email %s (%s) has no extractable reservation id; "
        "dead-lettering and alerting owner",
        msg_id,
        email_type.value,
    )
    await _record_processed_message(
        msg_id,
        "cancellation_parse_error",
        classified_as=email_type.value,
        error="no extractable reservation id in subject",
    )
    try:
        from app.config import load_config
        from app.settings import settings

        config = load_config()
        await asyncio.to_thread(  # F11: sync Gmail send off the loop
            send_cancellation_parse_failure_alert,
            message_id=msg_id,
            classified_as=email_type.value,
            alerts_service=get_alerts_service(),
            alerts_address=config.email.alerts,
            dashboard_base_url=f"https://{settings.domain}",
        )
    except Exception as alert_exc:  # noqa: BLE001 — alert failure must be non-fatal
        log.error(
            "Failed to send unprocessable-cancellation alert for %s: %s",
            msg_id,
            alert_exc,
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _initial_tasks(platform: Platform, has_phone: bool, has_email: bool) -> list[BookingTask]:
    """Return the BookingTask rows to create alongside a new booking."""
    waiting = TaskState.WAITING
    pending = TaskState.PENDING
    skipped = TaskState.SKIPPED

    tasks = [
        BookingTask(task_type=TaskType.OWNER_ALERT_NEW_BOOKING, state=pending),
        BookingTask(task_type=TaskType.CLEANER_SHEET_ADD, state=pending),
        BookingTask(
            task_type=TaskType.DOCUSIGN_SEND,
            state=pending if has_email else waiting,
        ),
        BookingTask(task_type=TaskType.HOA_EMAIL, state=waiting),
        BookingTask(
            task_type=TaskType.ACCESS_CODE_CREATE,
            state=pending if has_phone else waiting,
        ),
        BookingTask(
            task_type=TaskType.OWNER_ALERT_MISSING_PHONE_7D,
            state=skipped if has_phone else pending,
        ),
        BookingTask(
            task_type=TaskType.OWNER_ALERT_MISSING_PHONE_4D,
            state=skipped if has_phone else pending,
        ),
        BookingTask(
            task_type=TaskType.OWNER_ALERT_MISSING_EMAIL_7D,
            state=skipped if has_email else pending,
        ),
        BookingTask(
            task_type=TaskType.OWNER_ALERT_MISSING_EMAIL_4D,
            state=skipped if has_email else pending,
        ),
        BookingTask(task_type=TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D, state=pending),
        BookingTask(task_type=TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D, state=pending),
    ]
    return tasks


def _booking_data_points(parsed: AirbnbBookingData | VrboBookingData) -> list[DataPoint]:
    """Return DataPoint provenance rows for all fields extracted from the email."""
    src = DataPointSource.EMAIL_PARSE
    fields: list[tuple[str, str]] = [
        ("guest_first_name", parsed.guest_first_name),
        ("guest_last_name", parsed.guest_last_name),
        ("check_in_date", str(parsed.check_in_date)),
        ("check_out_date", str(parsed.check_out_date)),
    ]
    if isinstance(parsed, AirbnbBookingData):
        fields.append(("confirmation_code", parsed.confirmation_code))
    else:
        fields.append(("reservation_id", parsed.reservation_id))
        if parsed.guest_phone:
            fields.append(("guest_phone", parsed.guest_phone))

    return [DataPoint(field_name=name, value=value, source=src) for name, value in fields]


async def persist_booking(
    msg_id: str,
    msg: Message,
    parsed: AirbnbBookingData | VrboBookingData,
    platform: Platform,
) -> None:
    """Persist a new Booking and its initial tasks/data_points to the database."""
    if isinstance(parsed, AirbnbBookingData):
        external_id = parsed.confirmation_code
        guest_phone = None
        guest_email = None
    else:
        external_id = parsed.reservation_id
        guest_phone = parsed.guest_phone
        guest_email = None

    has_phone = bool(guest_phone)
    has_email = bool(guest_email)

    # Resolve property_id: Phase 3 will add proper property matching; hardcode
    # the first (and currently only) property for now.
    from app.config import load_config
    try:
        config = load_config()
        property_id = config.properties[0].id
    except Exception:
        property_id = "property_1"

    booking = Booking(
        platform=platform,
        external_id=external_id,
        property_id=property_id,
        guest_first_name=parsed.guest_first_name,
        guest_last_name=parsed.guest_last_name,
        guest_phone=guest_phone,
        guest_email=guest_email,
        check_in_date=parsed.check_in_date,
        check_out_date=parsed.check_out_date,
        status=BookingStatus.ACTIVE,
        source_email_message_id=msg_id,
    )
    booking.tasks.extend(_initial_tasks(platform, has_phone=has_phone, has_email=has_email))
    booking.data_points.extend(_booking_data_points(parsed))

    alert_task = next(
        (t for t in booking.tasks if t.task_type == TaskType.OWNER_ALERT_NEW_BOOKING),
        None,
    )

    async with AsyncSessionLocal() as session:
        # F1c (bug hunt 2026-07-22): a re-sent/re-forwarded confirmation for an
        # existing booking used to raise IntegrityError on commit and — never
        # dead-lettered — be re-fetched every poll forever. Check first; the
        # IntegrityError handler below backstops the check-then-insert race.
        existing = (
            await session.execute(
                select(Booking.id).where(
                    Booking.platform == platform,
                    Booking.external_id == external_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            log.warning(
                "Duplicate %s confirmation for existing booking %s (msg_id=%s); "
                "dead-lettering",
                platform.value,
                external_id,
                msg_id,
            )
            await _record_processed_message(
                msg_id,
                "duplicate",
                classified_as=platform.value,
                error=f"booking {external_id} already exists",
            )
            return

        session.add(booking)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            log.warning(
                "Duplicate %s booking %s lost the insert race (msg_id=%s); "
                "dead-lettering: %s",
                platform.value,
                external_id,
                msg_id,
                exc,
            )
            await _record_processed_message(
                msg_id, "duplicate", classified_as=platform.value, error=str(exc)
            )
            return

        log.info("Persisted %s booking %s (msg_id=%s)", platform.value, external_id, msg_id)

        # Fire the new-booking alert email (non-fatal if it fails) and record the
        # outcome on the OWNER_ALERT_NEW_BOOKING task row (R7): COMPLETE when the
        # alert really went out, PENDING + last_error when it didn't.
        try:
            from app.config import load_config
            from app.ingestion.alerts import send_new_booking_alert
            from app.integrations.gmail.oauth import get_alerts_service
            from app.settings import settings

            config = load_config()
            await asyncio.to_thread(  # F11: sync Gmail send off the loop
                send_new_booking_alert,
                booking,
                alerts_service=get_alerts_service(),
                alerts_address=config.email.alerts,
                dashboard_base_url=f"https://{settings.domain}",
            )
        except Exception as exc:
            log.error("Failed to send new-booking alert for %s: %s", external_id, exc)
            if alert_task is not None:
                alert_task.last_error = str(exc)
                await session.commit()
        else:
            if alert_task is not None:
                alert_task.state = TaskState.COMPLETE
                alert_task.completed_at = datetime.now(UTC)
                await session.commit()


# ---------------------------------------------------------------------------
# Cancellation stub (implemented fully in step 2.7)
# ---------------------------------------------------------------------------

async def handle_cancellation(msg_id: str, msg: Message, platform: Platform) -> None:
    """Delegate to the cancellation handler module."""
    from app.ingestion.cancellation import handle_cancellation as _handle
    await _handle(msg_id, msg, platform)


# ---------------------------------------------------------------------------
# APScheduler job
# ---------------------------------------------------------------------------

async def poll_booking_feed() -> None:
    """Poll the booking feed inbox and process any new booking/cancellation emails.

    Registered as an async APScheduler job, fired every 5 minutes.
    Idempotency: message IDs already stored in bookings.source_email_message_id
    are skipped without re-processing.
    """
    from app.integrations.gmail.oauth import get_booking_feed_service

    log.debug("Polling booking feed inbox")

    try:
        service = await asyncio.to_thread(get_booking_feed_service)
    except Exception as exc:
        log.error("Gmail auth failed: %s", exc)
        return

    # Collect already-processed message IDs from both booking and cancellation cols.
    async with AsyncSessionLocal() as session:
        rows = await session.execute(
            select(Booking.source_email_message_id, Booking.cancellation_email_message_id)
        )
        already_seen: set[str] = set()
        for src_id, cancel_id in rows:
            if src_id:
                already_seen.add(src_id)
            if cancel_id:
                already_seen.add(cancel_id)

        # Include dead-lettered messages (EmailType.OTHER / parse failures) so
        # they are not re-fetched every poll (R3b).
        processed = await session.execute(select(ProcessedMessage.message_id))
        already_seen.update(mid for (mid,) in processed)

    # F11: the Gmail list/fetch calls are sync network I/O — run them in a
    # worker thread so a slow/hung poll can't freeze the event loop.
    new_ids = await asyncio.to_thread(fetch_unprocessed_message_ids, service, already_seen)
    if new_ids:
        log.info("Processing %d new message(s)", len(new_ids))
        for msg_id in new_ids:
            try:
                payload = await asyncio.to_thread(fetch_raw_message, service, msg_id)
                await process_message(msg_id, payload)
            except Exception as exc:
                log.exception("Failed to process message %s: %s", msg_id, exc)
    else:
        log.debug("No new messages")

    # Success heartbeat (sustainability audit item 1): a completed poll cycle —
    # auth, inbox listing, processing loop — pings the external dead-man's
    # switch. The Gmail-auth-failure return above deliberately never reaches
    # this line; the resulting silence is what alerts the owner.
    from app.settings import settings

    await ping_heartbeat_async(settings.healthchecks_ping_url_poller, label="poller")
