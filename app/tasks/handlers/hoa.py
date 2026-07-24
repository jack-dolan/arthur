"""HOA email task handler — immediate-trigger path only (Phase 4, D-09).

Send policy (owner-adjudicated 2026-07-22, bug hunt F3):
    - Too early (before the window's ``earliest``): do NOT send; leave the task
      WAITING for the hourly scheduler. "No earlier than days_max days before
      check-in" is a hard HOA rule.
    - In window: send.
    - After ``latest``: STILL send, immediately, with a warning log. ``latest``
      is the last *acceptable* day for the scheduled send, not a send-blocker —
      a late-signing guest's form must reach the HOA (late beats never). The
      old behavior (skip + leave WAITING) stranded the task forever.

    All date comparisons use today_et() — the US/Eastern calendar date — never
    the server-local date (the container runs UTC; bug hunt F2).

    Per D-09, this handler NEVER sets task.state = TaskState.FAILED for
    window-miss cases.  The dispatcher's outer try/except will set FAILED
    only on unexpected exceptions.

Handler signature (matches dispatcher convention):
    async def handle_hoa_email(booking, task, session) -> None
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import load_config
from app.db.models import Booking, BookingTask, TaskState
from app.integrations.gmail.oauth import get_alerts_service
from app.integrations.hoa.email import ET, send_hoa_email
from app.integrations.hoa.window import hoa_window, today_et
from app.tasks.claim import claim_task

log = logging.getLogger(__name__)


def _evaluate_window(
    today: date,
    earliest: date,
    latest: date,
) -> Literal["early", "in_window", "past"]:
    """Classify today relative to the HOA email window.

    Returns
    -------
    "early"     — window has not yet opened
    "in_window" — today is within [earliest, latest] inclusive
    "past"      — window has already closed
    """
    if today < earliest:
        return "early"
    if today > latest:
        return "past"
    return "in_window"


async def handle_hoa_email(
    booking: Booking,
    task: BookingTask,
    session: AsyncSession,
) -> None:
    """Immediate-trigger HOA email handler.

    Sends the HOA guest-arrival notice if the signed PDF is present and the
    current date falls inside the HOA email window.  Does NOT set FAILED on
    window misses — Phase 5's daily scheduler handles those cases (D-09).

    Parameters
    ----------
    booking:
        The booking whose HOA_EMAIL task is being dispatched.
    task:
        The ``BookingTask`` with ``task_type == TaskType.HOA_EMAIL``.
    session:
        The current async DB session (not committed by this handler — the
        dispatcher commits after each handler returns).
    """
    # Guard: signed PDF required to attach to the email
    if booking.signed_pdf_path is None:
        log.warning(
            "HOA email for booking %s skipped — no signed PDF yet", booking.id
        )
        return  # Leave task in current state for Phase 5

    # Load per-property config to get HOA settings
    config = load_config()
    prop = next(
        (p for p in config.properties if p.id == booking.property_id),
        None,
    )
    if prop is None:
        log.warning(
            "HOA email for booking %s skipped — property %r not found in config",
            booking.id,
            booking.property_id,
        )
        return

    # Calculate the valid send window
    earliest, latest = hoa_window(
        booking.check_in_date,
        prop.hoa.open_days,
        prop.hoa.email_window_days_min,
        prop.hoa.email_window_days_max,
    )

    today = today_et()
    window_status = _evaluate_window(today, earliest, latest)

    if window_status == "early":
        log.info(
            "HOA window not yet open for booking %s "
            "(earliest=%s, today=%s); leaving for Phase 5 scheduler",
            booking.id,
            earliest,
            today,
        )
        return  # D-09: do NOT set FAILED; leave as WAITING for Phase 5

    if window_status == "past":
        # F3: `latest` is a scheduling deadline, not a send-blocker. A form
        # signed late must still reach the HOA — skipping here stranded the
        # task WAITING forever (rescanned hourly, never sent, never alerted).
        log.warning(
            "HOA send for booking %s is LATE (latest=%s, today=%s); "
            "sending anyway — the HOA may need a call to expedite the packet",
            booking.id,
            latest,
            today,
        )

    # In window (or late) — atomically claim the task before sending (R1 / Step 19).
    # The webhook-immediate path (handle_envelope_completed) uses the same
    # claim seam, so the two HOA send paths are mutually exclusive: whichever
    # commits WAITING -> IN_PROGRESS first sends; the other finds a non-WAITING
    # row and skips, preventing a double HOA email.
    claimed = await claim_task(
        session, task.id, expect=TaskState.WAITING, to=TaskState.IN_PROGRESS
    )
    if not claimed:
        log.info(
            "HOA email for booking %s already claimed by another path; skipping",
            booking.id,
        )
        return

    alerts_service = get_alerts_service()
    try:
        # F11: the Gmail send is sync network I/O — run it in a worker thread
        # so a slow/hung send can't freeze the event loop (webhooks, dashboard,
        # scheduler all share it).
        await asyncio.to_thread(
            send_hoa_email,
            booking,
            pdf_path=booking.signed_pdf_path,
            property_config=prop,
            alerts_service=alerts_service,
            owners_signature_name=config.owners.signature_name,
            from_address=config.email.alerts,
        )
    except Exception:
        # Release the claim so a later scan can retry the send.
        await claim_task(
            session, task.id, expect=TaskState.IN_PROGRESS, to=TaskState.WAITING
        )
        raise

    task.state = TaskState.COMPLETE
    task.completed_at = datetime.now(UTC)
    log.info("HOA email task completed for booking %s", booking.id)
