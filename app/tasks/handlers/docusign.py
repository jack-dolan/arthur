"""DocuSign task handlers: envelope send, envelope completed, idempotent void."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from pathlib import Path

import docusign_esign as ds
from docusign_esign.client.api_exception import ApiException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PropertyConfig, load_config
from app.db.models import Booking, BookingTask, TaskState, TaskType
from app.integrations.docusign.client import get_envelope_api
from app.integrations.gmail.oauth import get_alerts_service
from app.integrations.hoa.email import send_hoa_email
from app.integrations.hoa.window import hoa_window, today_et
from app.tasks.claim import claim_task

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_property_config(booking: Booking) -> PropertyConfig:
    """Look up the PropertyConfig for the given booking's property_id.

    Raises ValueError if no matching property is found in config.yaml.
    """
    config = load_config()
    for prop in config.properties:
        if prop.id == booking.property_id:
            return prop
    raise ValueError(
        f"No property config found for property_id={booking.property_id!r}"
    )


def _pdf_path_for_booking(booking: Booking) -> Path:
    """Return the PDF path for a booking.

    Uses booking.id (a UUID) as the filename — never a user-controlled
    string — to prevent path traversal (T-04-D1).
    """
    return Path(f"/app/data/pdfs/{booking.id}.pdf")


# ---------------------------------------------------------------------------
# Task: DOCUSIGN_SEND
# ---------------------------------------------------------------------------


async def handle_docusign_send(
    booking: Booking, task: BookingTask, session: AsyncSession
) -> None:
    """Send a DocuSign envelope from template for the given booking.

    Sets task.external_ref = envelope_id and task.state = COMPLETE on success.
    Sets task.state = WAITING if guest_email is not yet available.
    """
    # Idempotency guard (Step 17): if an envelope was already created for this
    # task, never send a second one — just mark COMPLETE. Defends against a
    # re-dispatch of a task whose external side effect already happened.
    if task.external_ref:
        log.info(
            "DocuSign envelope already exists for booking %s (%s); skipping re-send",
            booking.id, task.external_ref,
        )
        task.state = TaskState.COMPLETE
        if task.completed_at is None:
            task.completed_at = datetime.now(UTC)
        return

    if booking.guest_email is None:
        # Cannot send without an email; stay WAITING until email is entered
        task.state = TaskState.WAITING
        log.info(
            "DocuSign send for booking %s deferred: guest_email not yet set", booking.id
        )
        return

    task.state = TaskState.IN_PROGRESS

    prop = _get_property_config(booking)

    signer = ds.TemplateRole(
        email=booking.guest_email,
        name=f"{booking.guest_first_name} {booking.guest_last_name}",
        # role_name MUST match the role in the DocuSign template; silent failure if wrong (RESEARCH Risk 3)
        role_name=prop.docusign_signer_role,
    )

    notification = ds.Notification(
        use_account_defaults="false",
        reminders=ds.Reminders(
            reminder_enabled="true",
            reminder_delay="7",
            reminder_frequency="0",
        ),
    )

    envelope_def = ds.EnvelopeDefinition(
        status="sent",
        template_id=prop.docusign_template_id,
        template_roles=[signer],
        notification=notification,
    )

    # F11: get_envelope_api performs the sync httpx token exchange — keep it
    # (and the SDK call) off the event loop.
    envelopes_api, account_id = await asyncio.to_thread(get_envelope_api)
    result = await asyncio.to_thread(
        envelopes_api.create_envelope,
        account_id,
        envelope_definition=envelope_def,
    )

    task.external_ref = result.envelope_id
    task.state = TaskState.COMPLETE
    task.completed_at = datetime.now(UTC)
    log.info(
        "Sent DocuSign envelope %s for booking %s", result.envelope_id, booking.id
    )


# ---------------------------------------------------------------------------
# Task: envelope completed webhook handler
# ---------------------------------------------------------------------------


async def handle_envelope_completed(
    booking: Booking,
    task: BookingTask,
    envelope_id: str,
    session: AsyncSession,
) -> None:
    """Handle DocuSign envelope-completed event.

    1. Downloads combined PDF from DocuSign.
    2. Writes to /app/data/pdfs/{booking.id}.pdf (UUID-only path; T-04-D1 mitigation).
    3. Sets booking.signed_pdf_path and COMMITS it before attempting any send.
    4. Evaluates HOA window: if current date is in window, triggers HOA email immediately.
       If too early, leaves HOA task WAITING for Phase 5 scheduler (D-09 / Risk 10).

    Commit ordering (go-live Step 2): the signed-PDF path is the durable record of
    an irreversible external fact (the guest signed), whereas the HOA email is a
    retryable side effect. We commit signed_pdf_path BEFORE attempting the HOA send
    so a failed send can never roll back that record — the scheduled path in
    app/tasks/handlers/hoa.py then recovers the send with no re-download. The caller
    still commits at the end to persist hoa_task.state after a successful send.
    """
    # F11: token exchange off the loop (see handle_docusign_send).
    envelopes_api, account_id = await asyncio.to_thread(get_envelope_api)

    # Download combined PDF (all docs + Certificate of Completion).
    # SDK signature: get_document(account_id, document_id, envelope_id) —
    # "combined" is the document_id; envelope_id is the third argument.
    pdf_bytes = await asyncio.to_thread(
        envelopes_api.get_document, account_id, "combined", envelope_id
    )

    # Write PDF to filesystem — UUID-only path, no user-controlled component (T-04-D1)
    pdf_path = _pdf_path_for_booking(booking)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)

    booking.signed_pdf_path = str(pdf_path)
    # The guest has signed — still-pending unsigned-DocuSign reminders are moot;
    # record SKIPPED so the dashboard stops showing them as outstanding. Fired
    # reminders (COMPLETE) are history and stay untouched.
    for t in booking.tasks:
        if (
            t.task_type
            in (
                TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D,
                TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D,
            )
            and t.state == TaskState.PENDING
        ):
            t.state = TaskState.SKIPPED
    # Persist the durable signed-PDF record BEFORE any retryable side effect
    # (HOA send) so a send failure cannot roll it back.
    await session.commit()
    log.info(
        "Stored signed PDF for booking %s at %s", booking.id, pdf_path
    )

    # HOA window evaluation (D-09: immediate-trigger path only in Phase 4)
    prop = _get_property_config(booking)
    earliest, latest = hoa_window(
        booking.check_in_date,
        prop.hoa.open_days,
        prop.hoa.email_window_days_min,
        prop.hoa.email_window_days_max,
    )

    # Find HOA_EMAIL task on the booking
    hoa_task: BookingTask | None = next(
        (t for t in booking.tasks if t.task_type == TaskType.HOA_EMAIL),
        None,
    )

    # ET date, never the server (UTC) date — bug hunt F2. Sendable means "the
    # window has opened"; there is no upper cutoff (owner decision 2026-07-22,
    # F3): a late signature still sends immediately, with a warning.
    today = today_et()
    sendable = today >= earliest
    if sendable and today > latest and hoa_task is not None:
        log.warning(
            "HOA send for booking %s is LATE (latest=%s, today=%s); "
            "sending anyway — the HOA may need a call to expedite the packet",
            booking.id,
            latest,
            today,
        )
    if sendable and hoa_task is not None:
        # R1 / Step 19: atomically claim the HOA task (WAITING -> IN_PROGRESS,
        # committed) BEFORE sending. This is the same atomic-claim seam the
        # hourly check_hoa_window path uses (app/tasks/claim.py), so the two
        # HOA send paths are mutually exclusive: if the hourly scan fires during
        # this network send, its claim finds a non-WAITING row and skips.
        # A duplicate/replayed webhook (task already COMPLETE/IN_PROGRESS) also
        # loses the claim, preserving the Step 17 at-least-once idempotency.
        claimed = await claim_task(
            session, hoa_task.id, expect=TaskState.WAITING, to=TaskState.IN_PROGRESS
        )
        if not claimed:
            log.info(
                "HOA email for booking %s not sent: HOA task already claimed "
                "(duplicate/replayed webhook or the scheduler won the race); "
                "skipping to stay idempotent",
                booking.id,
            )
            return
        log.info(
            "HOA window open for booking %s (earliest=%s, latest=%s); sending immediately",
            booking.id,
            earliest,
            latest,
        )
        # Converge on the single real integration function in
        # app/integrations/hoa/email.py — identical call shape to the scheduled
        # path in app/tasks/handlers/hoa.py.
        config = load_config()
        try:
            # F11: sync Gmail send off the event loop.
            await asyncio.to_thread(
                send_hoa_email,
                booking,
                pdf_path=booking.signed_pdf_path,
                property_config=prop,
                alerts_service=get_alerts_service(),
                owners_signature_name=config.owners.signature_name,
                from_address=config.email.alerts,
            )
        except Exception:
            # Release the claim so the hourly scheduler can retry the send.
            await claim_task(
                session,
                hoa_task.id,
                expect=TaskState.IN_PROGRESS,
                to=TaskState.WAITING,
            )
            raise
        hoa_task.state = TaskState.COMPLETE
        hoa_task.completed_at = datetime.now(UTC)
    elif not sendable:
        log.info(
            "HOA window not yet open for booking %s (today=%s, earliest=%s); leaving for scheduler",
            booking.id,
            today,
            earliest,
        )
        # Leave hoa_task in current state (WAITING) for Phase 5 scheduler
    else:
        log.warning(
            "HOA window open for booking %s but no HOA_EMAIL task found; skipping",
            booking.id,
        )


# ---------------------------------------------------------------------------
# Idempotent void (DOCUSIGN-04)
# ---------------------------------------------------------------------------


def void_envelope_idempotent(envelope_id: str) -> None:
    """Void a DocuSign envelope, swallowing already-terminal-state errors.

    ApiException 400 (already in completed/declined/voided state) and
    409 (conflict / already voided) are swallowed so cancellation flows
    are not broken by terminal envelopes (T-04-D4 mitigation).

    All other ApiExceptions are re-raised.
    """
    envelopes_api, account_id = get_envelope_api()

    env = ds.Envelope()
    env.status = "voided"
    env.voided_reason = "Booking cancelled"

    try:
        envelopes_api.update(account_id, envelope_id, envelope=env)
        log.info("Voided DocuSign envelope %s", envelope_id)
    except ApiException as exc:
        if exc.status in (400, 409):
            log.info(
                "Envelope %s already in terminal state (status=%s); skipping void",
                envelope_id,
                exc.status,
            )
            return
        raise
