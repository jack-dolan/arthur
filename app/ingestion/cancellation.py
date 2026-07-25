"""Cancellation handler (step 2.7).

On receiving a cancellation email the handler:
  1. Looks up the booking by platform external ID.
  2. Returns immediately (idempotent) if already CANCELLED.
  3. Marks the booking CANCELLED and records the cancellation email message ID.
  4. Voids the DocuSign envelope IF one was sent and the form is not yet complete.
     (HOA_EMAIL == COMPLETE means the form was signed — voiding a completed
     envelope would fail, so we skip it.)
  5. Deletes the Seam access code IF one was created.
  6. Sends a cancellation alert email to the owners regardless of workflow state,
     telling them to check the HOA and cleaner sheet manually.

External API stubs (void_docusign_envelope, delete_seam_access_code) are
thin wrappers that will gain real implementations in Phase 4B and 4C.
parse_cancellation_external_id and _apply_cancellation are also called from
poller.handle_cancellation.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import UTC, datetime
from email.message import Message
from email.mime.text import MIMEText

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.integrations.seam.client as _seam_client_module
import app.tasks.handlers.docusign as _docusign_handlers
from app.config import load_config
from app.db.models import (
    Booking,
    BookingStatus,
    BookingTask,
    Platform,
    TaskState,
    TaskType,
)
from app.db.session import AsyncSessionLocal
from app.ingestion.alerts import build_cancellation_alert
from app.integrations.gmail.oauth import get_alerts_service
from app.settings import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# External-ID extraction from cancellation emails
# ---------------------------------------------------------------------------

# Airbnb: "Canceled: Reservation HMTEST0001 for Feb 23 – 27, 2026"
# Also handles forwarded wrapping — the classifier already verified type.
# The code group is deliberately strict (uppercase-only, at least one digit):
# a fully case-insensitive pattern extracted the dictionary word "CANCELED"
# from "Reservation canceled by guest" as a garbage id (F1b, bug hunt
# 2026-07-22). A real code the strictness misses now dead-letters WITH an
# owner alert rather than silently replaying, so strict is the safe side.
_AIRBNB_CODE_RE = re.compile(r"(?i:Reservation)\s+((?=[A-Z0-9]*\d)[A-Z0-9]{8,})")

# VRBO: "Your reservation HA-TEST01 was canceled ..."
_VRBO_ID_RE = re.compile(r"reservation\s+(HA-[A-Z0-9]+)", re.IGNORECASE)


def _get_effective_subject(msg: Message) -> str:
    """Return the inner subject, stripping a Gmail manual-forward wrapper if present."""
    from app.ingestion.classifier import _FORWARD_HEADER_RE, _FWD_SUBJECT_RE, _get_text_body

    outer_subject = msg.get("Subject", "")
    body = _get_text_body(msg)
    if not _FORWARD_HEADER_RE.search(body):
        return outer_subject

    for line in body.splitlines():
        m = _FWD_SUBJECT_RE.match(line)
        if m:
            return m.group(1).strip()
    return outer_subject


def parse_cancellation_external_id(msg: Message, platform: Platform) -> str | None:
    """Extract the confirmation/reservation ID from a cancellation email."""
    subject = _get_effective_subject(msg)

    if platform == Platform.AIRBNB:
        m = _AIRBNB_CODE_RE.search(subject)
        return m.group(1).upper() if m else None

    if platform == Platform.VRBO:
        m = _VRBO_ID_RE.search(subject)
        return m.group(1).upper() if m else None

    return None


# ---------------------------------------------------------------------------
# External API wrappers (Phase 4 Wave 3 — stubs replaced)
# ---------------------------------------------------------------------------

def void_docusign_envelope(envelope_id: str) -> None:
    """Void a DocuSign envelope by calling the idempotent void handler.

    Delegates to void_envelope_idempotent (app/tasks/handlers/docusign.py),
    which swallows 400/409 ApiExceptions for already-terminal envelopes so
    cancellation flows are not broken by previously-completed envelopes.

    Must remain a sync def: _apply_cancellation calls this without await.
    """
    _docusign_handlers.void_envelope_idempotent(envelope_id)
    log.info("Voided DocuSign envelope %s", envelope_id)


def delete_seam_access_code(access_code_id: str) -> None:
    """Delete a Seam access code by calling the Seam SDK directly.

    Calls get_seam_client().access_codes.delete() synchronously. This is
    acceptable because:
    1. _apply_cancellation is async but calls this function without await
       (sync call inside an async context).
    2. Cancellations are rare (one per cancellation event), so the momentary
       event-loop block is an acceptable tradeoff over asyncio.run() complexity
       (which would fail inside a running event loop anyway — Risk 7).

    Must remain a sync def: _apply_cancellation calls this without await.
    """
    client = _seam_client_module.get_seam_client()
    client.access_codes.delete(access_code_id=access_code_id)
    log.info("Deleted Seam access code %s", access_code_id)


def send_cancellation_alert(booking: Booking) -> None:
    """Send the owner-facing cancellation alert via the alerts Gmail account.

    Builds a single summary email (see ``build_cancellation_alert``) telling the
    owners that the booking is cancelled and naming the two ALERT-ONLY manual
    cleanups (HOA notification + cleaner-schedule row). DocuSign void + Seam
    code deletion are automatic and are not the owner's job.

    Stays a sync def: ``_apply_cancellation`` calls it without await, matching
    the sibling senders in ``app.ingestion.alerts``.
    """
    config = load_config()
    dashboard_base_url = f"https://{settings.domain}"
    subject, body = build_cancellation_alert(booking, dashboard_base_url=dashboard_base_url)

    mime = MIMEText(body, "plain")
    mime["To"] = config.email.alerts
    mime["From"] = config.email.alerts
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    alerts_service = get_alerts_service()
    alerts_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log.info(
        "Sent cancellation alert for booking %s (platform=%s external_id=%s)",
        booking.id,
        booking.platform.value,
        booking.external_id,
    )


def _complete_cancellation_alert_tasks(booking: Booking) -> None:
    """Mark the two cancellation-alert task rows COMPLETE (creating any missing).

    COMPLETE here means "the owner has been notified" — the same semantic the
    reminder alerts use. These rows are not in the initial task set, so they are
    created on first cancellation and surfaced in the dashboard 'Alerts' group.
    """
    by_type = {t.task_type: t for t in booking.tasks}
    now = datetime.now(UTC)
    for task_type in (
        TaskType.OWNER_ALERT_CANCELLATION_HOA,
        TaskType.OWNER_ALERT_CANCELLATION_CLEANER,
    ):
        task = by_type.get(task_type)
        if task is None:
            task = BookingTask(task_type=task_type, state=TaskState.COMPLETE)
            booking.tasks.append(task)
        else:
            task.state = TaskState.COMPLETE
        task.completed_at = now


# ---------------------------------------------------------------------------
# Core handler
# ---------------------------------------------------------------------------

async def _apply_cancellation(
    booking: Booking,
    *,
    cancellation_msg_id: str,
    session: AsyncSession,
) -> None:
    """Apply cancellation logic to an already-loaded Booking.

    Separated from handle_cancellation for testability (no DB lookup needed
    in unit tests — the booking is passed in directly).
    """
    if booking.status == BookingStatus.CANCELLED:
        log.info("Booking %s already cancelled; skipping", booking.id)
        return

    # Determine what has been done so far by inspecting task states.
    tasks_by_type = {t.task_type: t for t in booking.tasks}

    docusign_task = tasks_by_type.get(TaskType.DOCUSIGN_SEND)
    hoa_task = tasks_by_type.get(TaskType.HOA_EMAIL)
    seam_task = tasks_by_type.get(TaskType.ACCESS_CODE_CREATE)

    # Mark the booking CANCELLED up front (Step 17). The cancellation is a fact the
    # moment the guest cancels; the DB must record it — with the message id so the
    # poller never re-processes this email — even if a downstream automatic cleanup
    # or the owner alert then fails. Doing this first (and isolating every fallible
    # step below) prevents the pathological state where an alert/API failure rolls
    # back the whole transaction, leaving the booking ACTIVE after the DocuSign
    # envelope was already voided and the Seam code already deleted, and re-polling
    # the cancellation every 5 minutes forever.
    booking.status = BookingStatus.CANCELLED
    booking.cancellation_email_message_id = cancellation_msg_id

    # --- Automatic cleanups (each isolated; failures are logged loudly) ---
    # Void DocuSign envelope only if a send happened (COMPLETE + external_ref) and
    # the HOA email has NOT been sent (HOA COMPLETE implies the form was signed —
    # voiding a completed envelope is a no-op the idempotent voider already swallows).
    hoa_complete = hoa_task is not None and hoa_task.state == TaskState.COMPLETE
    if (
        docusign_task is not None
        and docusign_task.state == TaskState.COMPLETE
        and docusign_task.external_ref
        and not hoa_complete
    ):
        try:
            # F11: sync DocuSign call off the event loop.
            await asyncio.to_thread(void_docusign_envelope, docusign_task.external_ref)
        except Exception:
            log.exception(
                "Cancellation: DocuSign void failed for booking %s (envelope %s) — "
                "booking still marked CANCELLED; envelope may need a manual void",
                booking.id,
                docusign_task.external_ref,
            )

    if (
        seam_task is not None
        and seam_task.state == TaskState.COMPLETE
        and seam_task.external_ref
    ):
        try:
            # F11: sync Seam call off the event loop.
            await asyncio.to_thread(delete_seam_access_code, seam_task.external_ref)
        except Exception:
            log.exception(
                "Cancellation: Seam code delete failed for booking %s (code %s) — "
                "booking still marked CANCELLED; code may need a manual delete",
                booking.id,
                seam_task.external_ref,
            )

    # F5 (bug hunt 2026-07-22): cancel the WORKFLOW too. Unstarted automation
    # tasks must never run for a cancelled stay — left PENDING/WAITING they
    # stayed dispatchable, and a later contact-save would fire a real DocuSign
    # envelope, door code and cleaner row. COMPLETE tasks are history (their
    # side effects were just handled above); IN_PROGRESS/FAILED are left for a
    # human to inspect.
    for automation_type in (
        TaskType.CLEANER_SHEET_ADD,
        TaskType.DOCUSIGN_SEND,
        TaskType.ACCESS_CODE_CREATE,
        TaskType.HOA_EMAIL,
    ):
        automation_task = tasks_by_type.get(automation_type)
        if automation_task is not None and automation_task.state in (
            TaskState.PENDING,
            TaskState.WAITING,
        ):
            automation_task.state = TaskState.SKIPPED

    # Owner alert — isolated. Only record the manual-cleanup task rows as COMPLETE
    # ("owner notified") if the alert actually sent; a send failure leaves them
    # not-COMPLETE so the gap is visible rather than silently swallowed.
    try:
        # F11: sync Gmail send off the event loop.
        await asyncio.to_thread(send_cancellation_alert, booking)
    except Exception:
        log.exception(
            "Cancellation: owner alert send failed for booking %s — booking is "
            "CANCELLED but the owner was NOT notified of the manual cleanups",
            booking.id,
        )
    else:
        _complete_cancellation_alert_tasks(booking)


async def handle_cancellation(msg_id: str, msg: Message, platform: Platform) -> None:
    """Entry point called by the poller for cancellation emails."""
    external_id = parse_cancellation_external_id(msg, platform)
    if not external_id:
        log.warning("Could not extract external ID from cancellation email %s", msg_id)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Booking).where(
                Booking.platform == platform,
                Booking.external_id == external_id,
            )
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            log.warning(
                "Cancellation for unknown booking %s/%s (msg_id=%s)",
                platform.value,
                external_id,
                msg_id,
            )
            return

        # Eagerly load tasks so _apply_cancellation can inspect them.
        await session.refresh(booking, attribute_names=["tasks"])
        await _apply_cancellation(booking, cancellation_msg_id=msg_id, session=session)
        await session.commit()
