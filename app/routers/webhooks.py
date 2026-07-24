"""DocuSign webhook endpoint with HMAC-SHA256 signature validation.

Implements POST /webhooks/docusign per DOCUSIGN-02 requirement.

Security (T-04-W1, T-04-W2):
- Raw bytes are read via `await request.body()` BEFORE any JSON parsing.
- HMAC-SHA256 is computed over the raw bytes using settings.docusign_hmac_key.
- Comparison uses hmac.compare_digest() (constant-time) to prevent timing attacks.
- Invalid or missing signature returns 400 BEFORE any DB writes or handler calls.

Status routing:
- "completed" → handle_envelope_completed (download PDF, trigger HOA)
- "declined" → handle_envelope_declined (DOCUSIGN_SEND task → FAILED)
- other ("sent", "delivered", "voided") → log info, return 200

DocuSign Connect requirement: always return 200 on accepted events to suppress retries.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401 — type hint
from sqlalchemy.orm import selectinload

from app.config import load_config
from app.db.models import Booking, BookingTask, TaskState, TaskType
from app.db.session import AsyncSessionLocal
from app.ingestion.alerts import send_docusign_webhook_parse_failure_alert
from app.integrations.gmail.oauth import get_alerts_service
from app.settings import settings
from app.tasks.handlers.docusign import handle_envelope_completed

log = logging.getLogger(__name__)

router = APIRouter()


def _extract_envelope_event(payload: dict) -> tuple[str | None, str | None]:
    """Extract (envelope_id, status) from a DocuSign Connect payload.

    DocuSign Connect payload structure is not fully documented and differs
    between webhook configuration versions (Risk 4 per RESEARCH.md).
    Tries multiple key paths in order of likelihood. Returns (None, None)
    if extraction fails.

    VALIDATION NOTE: The real payload structure should be validated against
    a captured webhook from the DocuSign sandbox before go-live
    (per VALIDATION.md manual-only verification requirement).
    """
    envelope_id: str | None = None
    status_str: str | None = None

    # Try multiple known payload structures for envelope_id
    for path_fn in [
        # Modern Connect "aggregate" JSON (the real production shape, Risk 4).
        lambda p: p.get("data", {}).get("envelopeId"),
        lambda p: p.get("envelopeSummary", {}).get("envelopeId"),
        lambda p: p.get("envelopeId"),
        # Aggregate `uri` = ".../envelopes/{envelopeId}" — fallback if data omits it.
        lambda p: p["uri"].rstrip("/").split("/envelopes/", 1)[1].split("/")[0]
        if "/envelopes/" in str(p.get("uri", "")) else None,
    ]:
        try:
            val = path_fn(payload)
            if val:
                envelope_id = str(val)
                break
        except (KeyError, AttributeError, TypeError, IndexError):
            continue

    # Try multiple known payload structures for status
    for path_fn in [
        # Aggregate JSON carries the status in the event name, e.g.
        # "envelope-completed" -> "completed". Always present; most reliable.
        lambda p: p["event"].split("envelope-", 1)[1]
        if str(p.get("event", "")).startswith("envelope-") else None,
        lambda p: p.get("data", {}).get("envelopeSummary", {}).get("status"),
        lambda p: p.get("data", {}).get("status"),
        lambda p: p.get("envelopeSummary", {}).get("status"),
        lambda p: p.get("status"),
    ]:
        try:
            val = path_fn(payload)
            if val:
                status_str = str(val).lower()
                break
        except (KeyError, AttributeError, TypeError, IndexError):
            continue

    return envelope_id, status_str


async def handle_envelope_declined(envelope_id: str, session: AsyncSession) -> None:
    """Handle DocuSign envelope-declined event.

    Finds the DOCUSIGN_SEND BookingTask matching the given envelope_id
    (via external_ref) and marks it FAILED with a descriptive error message.

    Exposed as a module-level async function so unit tests can patch it
    directly via patch("app.routers.webhooks.handle_envelope_declined").
    """
    result = await session.execute(
        select(BookingTask).where(
            BookingTask.task_type == TaskType.DOCUSIGN_SEND,
            BookingTask.external_ref == envelope_id,
        )
    )
    task = result.scalar_one_or_none()
    if task is not None:
        task.state = TaskState.FAILED
        task.last_error = "Recipient declined to sign"
        log.info(
            "DOCUSIGN_SEND task for envelope %s marked FAILED (recipient declined)",
            envelope_id,
        )
    else:
        log.warning(
            "DocuSign declined webhook: no DOCUSIGN_SEND task found for envelope %s",
            envelope_id,
        )


@router.post("/webhooks/docusign", status_code=200)
async def docusign_webhook(request: Request) -> Response:
    """Receive DocuSign Connect webhook events.

    Security-critical ordering (T-04-W1, T-04-W2):
    1. Read raw bytes FIRST — before any parsing or DB operations.
    2. Validate HMAC-SHA256 — reject with 400 if invalid/missing.
    3. Parse JSON from already-validated raw bytes.
    4. Route on status.
    """
    # Step 1: Capture raw body before any parsing (T-04-W2 mitigation)
    raw_body = await request.body()

    # Step 2: Validate HMAC-SHA256 signature (T-04-W1 mitigation)
    signature = request.headers.get("X-DocuSign-Signature-1", "")
    if not signature:
        log.warning("DocuSign webhook rejected: missing X-DocuSign-Signature-1 header")
        return Response(status_code=400, content="Missing signature")

    secret = settings.docusign_hmac_key.encode("utf-8")
    computed = base64.b64encode(
        hmac.new(secret, raw_body, hashlib.sha256).digest()
    ).decode("utf-8")

    if not hmac.compare_digest(computed, signature):
        log.warning("DocuSign webhook rejected: invalid HMAC-SHA256 signature")
        return Response(status_code=400, content="Invalid signature")

    # Step 3: Parse JSON from validated raw bytes (NOT via await request.json())
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        log.warning("DocuSign webhook rejected: body is not valid JSON")
        return Response(status_code=400, content="Invalid JSON")

    # Step 4: Extract envelope event fields
    envelope_id, status_str = _extract_envelope_event(payload)
    if envelope_id is None or status_str is None:
        # Authentic (HMAC passed) but the envelope id / status could not be
        # extracted — the Connect payload shape differs from what we expect
        # (Risk 4). We still return 200 (a 400 would imply tampering, and would
        # make Connect retry-storm), but we must NOT silently drop what may be a
        # 'completed' event: log loudly AND alert the owner so a signed form
        # cannot vanish unnoticed. The real payload shape is validated live at
        # go-live (Step 21); this makes any mismatch visible instead of silent.
        payload_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
        log.error(
            "DocuSign webhook: authentic payload (valid HMAC) but envelopeId/status "
            "could not be extracted — Connect payload shape may have changed. "
            "Top-level keys: %s | body: %s",
            payload_keys,
            raw_body.decode("utf-8", "replace")[:4000],
        )
        try:
            config = load_config()
            await asyncio.to_thread(  # F11: sync Gmail send off the loop
                send_docusign_webhook_parse_failure_alert,
                payload_keys=payload_keys,
                alerts_service=get_alerts_service(),
                alerts_address=config.email.alerts,
                dashboard_base_url=f"https://{settings.domain}",
            )
        except Exception:
            log.exception("Failed to send DocuSign webhook parse-failure alert")
        return Response(status_code=200, content="OK")

    log.info(
        "DocuSign webhook received: envelope_id=%s status=%s", envelope_id, status_str
    )

    # Step 5: Route on status
    if status_str == "completed":
        async with AsyncSessionLocal() as session:
            task_result = await session.execute(
                select(BookingTask).where(
                    BookingTask.task_type == TaskType.DOCUSIGN_SEND,
                    BookingTask.external_ref == envelope_id,
                )
            )
            task = task_result.scalar_one_or_none()
            if task is None:
                log.warning(
                    "DocuSign completed webhook: no DOCUSIGN_SEND task for envelope %s",
                    envelope_id,
                )
                return Response(status_code=200, content="OK")
            booking_result = await session.execute(
                select(Booking)
                .where(Booking.id == task.booking_id)
                # Eager-load tasks: handle_envelope_completed walks booking.tasks
                # to find the HOA_EMAIL row. Without this, that access triggers an
                # async lazy-load outside a greenlet context and raises
                # MissingGreenlet on the real DB (masked when the handler is mocked).
                .options(selectinload(Booking.tasks))
            )
            booking = booking_result.scalar_one_or_none()
            if booking is None:
                log.warning(
                    "DocuSign completed webhook: no Booking for task %s (envelope %s)",
                    task.id,
                    envelope_id,
                )
                return Response(status_code=200, content="OK")
            await handle_envelope_completed(booking, task, envelope_id, session)
            await session.commit()
        return Response(status_code=200, content="OK")

    elif status_str == "declined":
        async with AsyncSessionLocal() as session:
            await handle_envelope_declined(envelope_id, session)
            await session.commit()
        return Response(status_code=200, content="OK")

    else:
        # "sent", "delivered", "voided" — acknowledged but no action needed
        log.info(
            "DocuSign webhook: status=%s for envelope %s — no action taken",
            status_str,
            envelope_id,
        )
        return Response(status_code=200, content="OK")
