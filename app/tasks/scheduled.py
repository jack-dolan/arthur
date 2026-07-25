"""Scheduling helpers and async job coroutines for the daily reminder workflow.

This module contains:

Plan 02 — Pure-function threshold and idempotency helpers:
  _REMINDER_MATRIX     — list of (TaskType, threshold_days, field_check_fn) tuples
  _is_docusign_unsigned — pure predicate: True when booking.signed_pdf_path is None
  _pending_reminders   — pure function: returns (task, threshold) pairs to fire today

Plan 03 — APScheduler-ready async job coroutines:
  check_daily_reminders()       — daily 8 AM Eastern job (D-01)
  _process_booking_reminders()  — per-booking helper called by check_daily_reminders
  check_hoa_window()            — hourly job dispatching HOA emails (D-01, D-13)

Design rationale (RESEARCH.md "Primary recommendation"):
  Extracting the decision logic into pure functions (Plan 02) keeps unit tests trivial
  — no mocking of DB sessions, scheduler instances, or async context managers.
  The async wrappers (Plan 03) are kept thin: they query the DB, iterate over bookings,
  call the pure-function helpers, and dispatch sends.

APScheduler registration happens in Plan 04 (app/main.py lifespan). This module only
exports the coroutines — it does NOT register them.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import selectinload

from app.config import load_config
from app.db.models import (
    Booking,
    BookingStatus,
    BookingTask,
    ProcessedMessage,
    TaskState,
    TaskType,
)
from app.db.session import AsyncSessionLocal
from app.ingestion.alerts import (
    send_access_code_problem_alert,
    send_classifier_drift_digest,
    send_credential_sentinel_alert,
    send_docusign_keepalive_failure_alert,
    send_monthly_status_report,
    send_reminder_alert,
    send_stalled_automations_alert,
)
from app.integrations.gmail.oauth import get_alerts_service, get_booking_feed_service
from app.integrations.seam.client import get_seam_client
from app.integrations.sheets.client import get_sheets_service
from app.monitoring import ping_heartbeat_async
from app.settings import settings
from app.tasks.handlers.hoa import handle_hoa_email

log = logging.getLogger(__name__)


def _is_docusign_unsigned(booking: Booking) -> bool:
    """Return True if the DocuSign form has NOT been signed (completed).

    Signed = booking.signed_pdf_path is not None.
    The Phase 4 DocuSign webhook handler sets this field when the envelope-completed
    event fires (DOCUSIGN-03). If it is None, the form is either not yet sent or not
    yet signed — either way, the unsigned alert is appropriate (D-12).
    """
    return booking.signed_pdf_path is None


# The 6-entry reminder matrix: (TaskType, threshold_days, field_check_fn)
# field_check_fn(booking) -> bool: True means "condition still present; alert should fire"
# Order matches ALERT-01 (phone), ALERT-02 (email), ALERT-03 (docusign unsigned).
_REMINDER_MATRIX: list[tuple[TaskType, int, Callable[[Booking], bool]]] = [
    (TaskType.OWNER_ALERT_MISSING_PHONE_7D, 7, lambda b: b.guest_phone is None),
    (TaskType.OWNER_ALERT_MISSING_PHONE_4D, 4, lambda b: b.guest_phone is None),
    (TaskType.OWNER_ALERT_MISSING_EMAIL_7D, 7, lambda b: b.guest_email is None),
    (TaskType.OWNER_ALERT_MISSING_EMAIL_4D, 4, lambda b: b.guest_email is None),
    (TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D, 7, _is_docusign_unsigned),
    (TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D, 4, _is_docusign_unsigned),
]

REMINDER_TASK_TYPES: tuple[TaskType, ...] = tuple(t for t, _, _ in _REMINDER_MATRIX)

# 7d reminder -> its 4d sibling (used to suppress same-morning double-fires).
_SIBLING_4D: dict[TaskType, TaskType] = {
    TaskType.OWNER_ALERT_MISSING_PHONE_7D: TaskType.OWNER_ALERT_MISSING_PHONE_4D,
    TaskType.OWNER_ALERT_MISSING_EMAIL_7D: TaskType.OWNER_ALERT_MISSING_EMAIL_4D,
    TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D: TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D,
}


def _pending_reminders(
    booking: Booking,
    today_et: date,
    tasks_by_type: dict[TaskType, BookingTask],
) -> list[tuple[BookingTask, int]]:
    """Return list of (task, threshold_days) for tasks that should fire today.

    Pure function — no I/O, no async, easily unit-testable.

    Filtering rules (D-05, D-06, RESEARCH.md Anti-pattern 5):
      1. Skip if task row does not exist for this booking.
      2. Skip if task.state != TaskState.PENDING (idempotency — COMPLETE and SKIPPED
         tasks are never re-processed).
      3. Skip if days_until > threshold (D-05: "within N days remaining", uses <=).
      4. Skip if field is no longer missing at runtime — re-check field presence even
         when the task is still PENDING (phone/email may have been entered via dashboard
         between booking creation and alert threshold crossing).

    Args:
        booking:       The booking to evaluate (Booking ORM instance).
        today_et:      Today's date in US/Eastern time (date object).
        tasks_by_type: dict mapping TaskType -> BookingTask for this booking's tasks.

    Returns:
        List of (BookingTask, threshold_days) pairs to fire, in matrix order.
    """
    days_until = (booking.check_in_date - today_et).days
    if days_until < 0:
        return []  # booking is in the past; never alert
    result: list[tuple[BookingTask, int]] = []

    for task_type, threshold, field_check in _REMINDER_MATRIX:
        task = tasks_by_type.get(task_type)
        if task is None:
            continue
        if task.state != TaskState.PENDING:
            # D-06: only PENDING tasks are processed; COMPLETE/SKIPPED are idempotent
            continue
        if days_until > threshold:
            # D-05: threshold is "within N days remaining"; > threshold means not yet
            continue
        if not field_check(booking):
            # Re-check field at runtime: if it was filled after task creation, skip
            continue
        result.append((task, threshold))

    # F16 (bug hunt 2026-07-22): a booking created <= 4 days out qualifies for
    # BOTH thresholds the same morning — suppress the 7d entry when its 4d
    # sibling fires too (one urgent email per condition, not two near-
    # duplicates). The suppressed 7d row stays PENDING and is swept to SKIPPED
    # once the condition resolves.
    firing_types = {t.task_type for t, _ in result}
    result = [
        (t, threshold)
        for t, threshold in result
        if not (
            threshold == 7 and _SIBLING_4D.get(t.task_type) in firing_types
        )
    ]

    return result


def _stale_reminders(
    booking: Booking,
    tasks_by_type: dict[TaskType, BookingTask],
) -> list[BookingTask]:
    """Return PENDING reminder tasks whose condition has since resolved.

    Pure function. A reminder created PENDING (field missing / form unsigned at
    booking creation) becomes moot once the condition resolves — leaving the row
    PENDING forever misreads as outstanding work on the dashboard. These rows
    should flip to SKIPPED, matching what _initial_tasks records when the
    condition was already satisfied at creation time.

    Only PENDING rows are returned: COMPLETE (the alert really fired) and
    SKIPPED are history and never rewritten.
    """
    stale: list[BookingTask] = []
    for task_type, _threshold, field_check in _REMINDER_MATRIX:
        task = tasks_by_type.get(task_type)
        if task is None or task.state != TaskState.PENDING:
            continue
        if not field_check(booking):
            stale.append(task)
    return stale


async def sweep_moot_reminders(session, today_et: date) -> int:
    """Flip PENDING reminders that can never fire to SKIPPED; return row count.

    Covers the two cases the daily scan (ACTIVE bookings with future check-in)
    never revisits: bookings whose check-in has passed and cancelled bookings.
    Strictly ``< today_et`` so a reminder can still fire (or retry a failed
    send) on check-in day itself.
    """
    result = await session.execute(
        update(BookingTask)
        .where(
            BookingTask.state == TaskState.PENDING,
            BookingTask.task_type.in_(REMINDER_TASK_TYPES),
            BookingTask.booking_id.in_(
                select(Booking.id).where(
                    or_(
                        Booking.check_in_date < today_et,
                        Booking.status == BookingStatus.CANCELLED,
                    )
                )
            ),
        )
        .values(state=TaskState.SKIPPED)
    )
    return result.rowcount


# ===========================================================================
# Plan 03 — Async APScheduler job coroutines
# ===========================================================================


async def check_daily_reminders() -> None:
    """Daily job: fire missing-phone, missing-email, and unsigned-DocuSign alerts.

    Registered by Plan 04 as a CronTrigger job at 8 AM US/Eastern (D-01).

    Query: ACTIVE bookings with future check-in dates (D-07, Pitfall 4).
    Eager-loads booking.tasks via selectinload to avoid N+1 (RESEARCH.md Anti-pattern).

    The outer session is closed before the per-booking loop. Each booking that
    requires writes opens its own session in _process_booking_reminders (Pattern 4).

    Per-booking failures are caught and logged; one failure never aborts the job
    (Pattern 4, T-05-09).
    """
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    # INFO heartbeat even on idle runs: APScheduler silently discards missed
    # fires, so ops treats a missing daily line as "job never ran".
    log.info("check_daily_reminders: run started (today_et=%s)", today_et)

    # Backstop sweep first: reminders on past-check-in or cancelled bookings can
    # never fire — flip them to SKIPPED so they stop reading as outstanding.
    try:
        async with AsyncSessionLocal() as session:
            swept = await sweep_moot_reminders(session, today_et)
            await session.commit()
            if swept:
                log.info("check_daily_reminders: swept %s moot reminder task(s)", swept)
    except Exception:
        log.exception("check_daily_reminders: moot-reminder sweep failed")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.status == BookingStatus.ACTIVE)
            .where(Booking.check_in_date > today_et)
            .options(selectinload(Booking.tasks))
        )
        bookings = result.scalars().all()

    for booking in bookings:
        try:
            await _process_booking_reminders(booking, today_et)
        except Exception:
            log.exception(
                "check_daily_reminders: error processing booking %s", booking.id
            )


async def _process_booking_reminders(booking: Booking, today_et: date) -> None:
    """Process all pending reminders for a single booking.

    Called by check_daily_reminders inside a per-booking try/except.

    Opens a fresh session, re-attaches the detached booking, evaluates the
    reminder matrix, dispatches sends, and flips task state to COMPLETE on
    success (D-06). Commits once after the full per-booking loop.
    """
    tasks_by_type = {t.task_type: t for t in booking.tasks}
    pending = _pending_reminders(booking, today_et, tasks_by_type)
    stale = _stale_reminders(booking, tasks_by_type)
    if not pending and not stale:
        return

    async with AsyncSessionLocal() as session:
        # Re-attach the detached booking instance to this new session
        session.add(booking)

        # Resolved-condition reminders are moot — record SKIPPED so the
        # dashboard stops showing them as outstanding.
        for task in stale:
            task.state = TaskState.SKIPPED

        if not pending:
            await session.commit()
            return

        config = load_config()
        dashboard_base_url = f"https://{settings.domain}"
        alerts_service = get_alerts_service()

        for task, threshold in pending:
            try:
                # F11: sync Gmail send off the event loop.
                await asyncio.to_thread(
                    send_reminder_alert,
                    task.task_type,
                    booking,
                    alerts_service=alerts_service,
                    alerts_address=config.email.alerts,
                    dashboard_base_url=dashboard_base_url,
                    threshold_days=threshold,
                )
            except Exception:
                log.exception(
                    "_process_booking_reminders: send failed for task %s booking %s",
                    task.task_type,
                    booking.id,
                )
                continue  # leave as PENDING for retry on next run

            # D-06: flip task state to COMPLETE only when send actually succeeded
            task.state = TaskState.COMPLETE
            task.completed_at = datetime.now(UTC)

        await session.commit()


async def check_hoa_window() -> None:
    """Hourly job: send HOA emails for bookings whose window has opened (D-01, D-13).

    Registered by Plan 04 as an IntervalTrigger job every 1 hour.

    Query: ACTIVE bookings with signed PDF (form is complete) and an HOA_EMAIL task
    still in WAITING state. Uses .unique() because the JOIN on tasks can produce
    duplicate Booking rows when multiple task rows match (RESEARCH.md HOA query pattern).

    Delegates to handle_hoa_email() for each booking — that function owns the window
    calculation and send logic (D-13). This coroutine only drives the query loop
    and commits the session after each handler returns.

    Per-booking failures are caught and logged; one failure never aborts the job
    (Pattern 4, T-05-09).
    """
    log.debug("check_hoa_window: scanning for bookings ready for HOA email")

    # Scan for candidate booking ids in one short-lived session, then close it.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Booking.id)
            .join(Booking.tasks)
            .where(
                and_(
                    Booking.status == BookingStatus.ACTIVE,
                    Booking.signed_pdf_path.is_not(None),
                    BookingTask.task_type == TaskType.HOA_EMAIL,
                    BookingTask.state == TaskState.WAITING,
                )
            )
        )
        booking_ids = [row[0] for row in result.all()]

    # Process each booking in its OWN session so one booking's failure (and the
    # rollback it triggers) cannot expire/detach the others. A shared session here
    # meant a rollback on booking N left booking N+1 expired, so the next loop
    # iteration's `booking.tasks` access raised MissingGreenlet and aborted the
    # whole run — defeating the per-booking failure isolation (Pattern 4).
    for booking_id in booking_ids:
        try:
            async with AsyncSessionLocal() as session:
                booking = (
                    await session.execute(
                        select(Booking)
                        .where(Booking.id == booking_id)
                        .options(selectinload(Booking.tasks))
                    )
                ).scalar_one_or_none()
                if booking is None:
                    continue

                hoa_task = next(
                    (t for t in booking.tasks if t.task_type == TaskType.HOA_EMAIL),
                    None,
                )
                if hoa_task is None:
                    log.warning(
                        "check_hoa_window: HOA_EMAIL task not found for booking %s",
                        booking_id,
                    )
                    continue
                # Re-check under this fresh read: if the webhook-immediate path
                # already claimed/sent it between the scan and now, skip to avoid
                # a double send.
                if hoa_task.state != TaskState.WAITING:
                    continue

                await handle_hoa_email(booking, hoa_task, session)
                await session.commit()
        except Exception:
            log.exception("check_hoa_window: error processing booking %s", booking_id)


async def refresh_docusign_token() -> None:
    """Weekly keep-alive: refresh the DocuSign token so it never expires idle.

    DocuSign refresh tokens live 30 days and are rotated on each exchange (R5 /
    Step 19). During a quiet period with no bookings, no DocuSign call is made,
    so the token could otherwise expire. This job performs a plain OAuth
    refresh-token exchange — it sends NO envelope, so it costs nothing — and
    ``_refresh_access_token`` persists the rotated token, resetting the 30-day
    clock. Run weekly (a 4x margin inside the 30-day window).

    Failures are logged AND alerted to the owner (F4) — a failing keep-alive is
    the early warning before the 30-day token death, and this job is the only
    thing watching. The alert is weekly at most (the job's own cadence). The
    job itself never raises.
    """
    from app.integrations.docusign.client import _refresh_access_token

    try:
        await asyncio.to_thread(_refresh_access_token)
        log.info("DocuSign refresh-token keep-alive succeeded")
        # Sustainability audit item 2: success pings the external heartbeat, so
        # a keep-alive that stops running (or stops succeeding) is caught by
        # the monitor even when the failure-alert email path is itself broken.
        await ping_heartbeat_async(
            settings.healthchecks_ping_url_keepalive, label="docusign-keepalive"
        )
    except Exception as exc:
        log.exception("DocuSign refresh-token keep-alive failed")
        try:
            config = load_config()
            await asyncio.to_thread(  # F11: sync Gmail send off the loop
                send_docusign_keepalive_failure_alert,
                error=str(exc),
                alerts_service=get_alerts_service(),
                alerts_address=config.email.alerts,
                dashboard_base_url=f"https://{settings.domain}",
            )
        except Exception:  # noqa: BLE001 — alert failure must not kill the job
            log.exception("Failed to send DocuSign keep-alive failure alert")


# ===========================================================================
# F17 (bug hunt 2026-07-22) — daily requeue/redispatch of stalled automations
# ===========================================================================

# Automation task types the daily job may requeue. HOA_EMAIL is deliberately
# excluded: its retry loop is the hourly check_hoa_window scan (a failed send
# releases the claim back to WAITING).
REQUEUEABLE_TASK_TYPES: tuple[TaskType, ...] = (
    TaskType.CLEANER_SHEET_ADD,
    TaskType.DOCUSIGN_SEND,
    TaskType.ACCESS_CODE_CREATE,
)

# After this many failed attempts a task is terminal: no more silent retries —
# the owner gets the stalled-automations digest instead.
MAX_AUTOMATION_ATTEMPTS = 5

# IN_PROGRESS older than this is the risk register's crash-window edge (claim
# committed, process died mid-external-call). Alert-only — the external side
# effect may have happened, so a human must inspect before any reset.
STUCK_IN_PROGRESS_AFTER = 24  # hours


async def requeue_stalled_automations() -> None:
    """Daily job: retry FAILED automations, re-dispatch orphaned PENDING ones,
    and alert the owner about anything terminally stalled (F17).

    Before this job the dispatcher only ever ran on a contact-form POST, so a
    transient failure (Sheets 500, DocuSign blip) left a task FAILED forever,
    and a PENDING task whose dispatch was lost (restart between the commit and
    asyncio.create_task) never ran at all — with re-submitting the contact
    form as the only, undocumented, recovery.

    Safety: requeueing FAILED is safe because every requeueable handler is
    idempotent (external_ref guards on DocuSign/Seam; the cleaner-sheet
    handler rolls back its partial insert and re-reads the sheet fresh).
    IN_PROGRESS is NEVER touched — only reported.
    """
    from app.tasks.dispatch import _dispatch_pending_tasks

    log.info("requeue_stalled_automations: run started")  # heartbeat (see above)
    today = datetime.now(ZoneInfo("America/New_York")).date()
    actionable_booking_ids = select(Booking.id).where(
        Booking.status == BookingStatus.ACTIVE,
        Booking.check_in_date >= today,
    )

    async with AsyncSessionLocal() as session:
        requeued = await session.execute(
            update(BookingTask)
            .where(
                BookingTask.state == TaskState.FAILED,
                BookingTask.task_type.in_(REQUEUEABLE_TASK_TYPES),
                BookingTask.attempt_count < MAX_AUTOMATION_ATTEMPTS,
                BookingTask.booking_id.in_(actionable_booking_ids),
            )
            .values(state=TaskState.PENDING)
            .returning(BookingTask.booking_id)
        )
        requeued_ids = {row[0] for row in requeued}
        await session.commit()
        if requeued_ids:
            log.info(
                "requeue_stalled_automations: requeued FAILED task(s) on %d booking(s)",
                len(requeued_ids),
            )

        # Re-dispatch every actionable booking with a PENDING automation —
        # covers both the just-requeued tasks and orphaned PENDING ones.
        pending_rows = await session.execute(
            select(BookingTask.booking_id)
            .where(
                BookingTask.state == TaskState.PENDING,
                BookingTask.task_type.in_(REQUEUEABLE_TASK_TYPES),
                BookingTask.booking_id.in_(actionable_booking_ids),
            )
            .distinct()
        )
        dispatch_ids = [row[0] for row in pending_rows]

    for booking_id in dispatch_ids:
        try:
            await _dispatch_pending_tasks(booking_id)
        except Exception:
            log.exception(
                "requeue_stalled_automations: dispatch failed for booking %s",
                booking_id,
            )

    # Digest of terminally stalled tasks: FAILED at the cap, or IN_PROGRESS
    # for >24h. Daily repetition is deliberate — these need a human.
    stuck_cutoff = datetime.now(UTC) - timedelta(hours=STUCK_IN_PROGRESS_AFTER)
    async with AsyncSessionLocal() as session:
        stalled_rows = (
            await session.execute(
                select(BookingTask, Booking)
                .join(Booking, BookingTask.booking_id == Booking.id)
                .where(
                    Booking.status == BookingStatus.ACTIVE,
                    Booking.check_in_date >= today,
                    BookingTask.task_type.in_(REQUEUEABLE_TASK_TYPES),
                    or_(
                        and_(
                            BookingTask.state == TaskState.FAILED,
                            BookingTask.attempt_count >= MAX_AUTOMATION_ATTEMPTS,
                        ),
                        and_(
                            BookingTask.state == TaskState.IN_PROGRESS,
                            BookingTask.updated_at < stuck_cutoff,
                        ),
                    ),
                )
            )
        ).all()

    if not stalled_rows:
        return

    try:
        config = load_config()
        base_url = f"https://{settings.domain}"
        items = [
            {
                "task": task.task_type.value,
                "state": task.state.value,
                "guest": f"{booking.guest_first_name} {booking.guest_last_name}".strip(),
                "check_in": booking.check_in_date.strftime("%b %-d, %Y"),
                "attempts": task.attempt_count,
                "last_error": task.last_error,
                "booking_url": f"{base_url}/bookings/{booking.id}",
            }
            for task, booking in stalled_rows
        ]
        await asyncio.to_thread(  # F11: sync Gmail send off the loop
            send_stalled_automations_alert,
            items,
            alerts_service=get_alerts_service(),
            alerts_address=config.email.alerts,
            dashboard_base_url=base_url,
        )
    except Exception:
        log.exception("requeue_stalled_automations: stalled-tasks alert failed")


# ===========================================================================
# F10 (bug hunt 2026-07-22) — daily on-device access-code verification
# ===========================================================================

# How close to check-in a booking must be before its code is verified. Far
# enough out for the owner to react; close enough that Seam should already
# have (or be about to have) the code on the device.
VERIFY_CODES_WITHIN_DAYS = 3


async def verify_access_codes() -> None:
    """Daily job: confirm upcoming bookings' codes actually exist on the lock.

    ``handle_access_code_create`` records COMPLETE when the Seam API accepts
    the create — but Seam programs the device ASYNCHRONOUSLY, and a lock that
    is offline (Wi-Fi/power) at programming time silently never gets the code.
    Without this check the first person to notice is the guest at 4 PM.

    For ACTIVE bookings checking in within VERIFY_CODES_WITHIN_DAYS days whose
    ACCESS_CODE_CREATE is COMPLETE with an external_ref, fetch the code from
    Seam and flag:
      - any reported errors on the code object,
      - a fetch failure (code deleted / 404 / API error),
      - status != "set" within 1 day of check-in (the guest is imminent).

    Alert-only: never mutates task state. Daily repetition while a problem
    persists is deliberate.
    """
    log.info("verify_access_codes: run started")  # heartbeat (see above)
    today = datetime.now(ZoneInfo("America/New_York")).date()
    horizon = today + timedelta(days=VERIFY_CODES_WITHIN_DAYS)

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(BookingTask, Booking)
                .join(Booking, BookingTask.booking_id == Booking.id)
                .where(
                    Booking.status == BookingStatus.ACTIVE,
                    Booking.check_in_date >= today,
                    Booking.check_in_date <= horizon,
                    BookingTask.task_type == TaskType.ACCESS_CODE_CREATE,
                    BookingTask.state == TaskState.COMPLETE,
                    BookingTask.external_ref.is_not(None),
                )
            )
        ).all()

    if not rows:
        return

    client = get_seam_client()
    problems: list[dict] = []
    base_url = f"https://{settings.domain}"

    for task, booking in rows:
        guest = f"{booking.guest_first_name} {booking.guest_last_name}".strip()
        entry = {
            "guest": guest,
            "check_in": booking.check_in_date.strftime("%b %-d, %Y"),
            "booking_url": f"{base_url}/bookings/{booking.id}",
        }
        try:
            code = await asyncio.to_thread(
                client.access_codes.get, access_code_id=task.external_ref
            )
        except Exception as exc:  # noqa: BLE001 — a fetch failure IS the finding
            problems.append(
                {**entry, "status": "unknown", "problem": f"could not fetch code: {exc}"}
            )
            continue

        status = str(getattr(code, "status", "unknown"))
        errors = list(getattr(code, "errors", None) or [])
        imminent = (booking.check_in_date - today).days <= 1

        if errors:
            problems.append(
                {**entry, "status": status, "problem": f"Seam reports errors: {errors}"}
            )
        elif imminent and status != "set":
            problems.append(
                {
                    **entry,
                    "status": status,
                    "problem": "code not confirmed on the device and check-in "
                    "is within a day",
                }
            )

    if not problems:
        log.info("verify_access_codes: %d code(s) checked, all healthy", len(rows))
        return

    log.warning("verify_access_codes: %d problem(s) found", len(problems))
    try:
        config = load_config()
        await asyncio.to_thread(  # F11: sync Gmail send off the loop
            send_access_code_problem_alert,
            problems,
            alerts_service=get_alerts_service(),
            alerts_address=config.email.alerts,
            dashboard_base_url=base_url,
        )
    except Exception:
        log.exception("verify_access_codes: problem alert failed")


async def complete_past_bookings() -> None:
    """Daily job: flip ACTIVE bookings whose stay has ended to COMPLETED (F15).

    Without a terminal state the dashboard's "active" list and every daily
    scan (HOA window, reminders, requeue) accumulate every booking forever —
    not urgent at one property, but a slow-burn maintenance cost with no
    ceiling. Strictly ``< today`` so a booking still checking out today stays
    ACTIVE through its final day. A single bulk UPDATE, no per-row I/O, so
    (unlike the other jobs) there's nothing to isolate per item.
    """
    log.info("complete_past_bookings: run started")  # heartbeat (see verify_access_codes)
    today = datetime.now(ZoneInfo("America/New_York")).date()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(Booking)
            .where(
                Booking.status == BookingStatus.ACTIVE,
                Booking.check_out_date < today,
            )
            .values(status=BookingStatus.COMPLETED)
        )
        await session.commit()

    if result.rowcount:
        log.info(
            "complete_past_bookings: marked %d booking(s) COMPLETED", result.rowcount
        )


# ===========================================================================
# Credential sentinel (sustainability audit 2026-07-23, item 3)
# ===========================================================================

# S105 is a false positive: Google's public, documented token endpoint, not a secret.
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105


def _check_gmail_booking_feed() -> None:
    """Prove the booking-feed Gmail token still refreshes (read-only)."""
    get_booking_feed_service().users().getProfile(userId="me").execute()


def _check_gmail_alerts() -> None:
    """Prove the alerts Gmail token still refreshes (read-only).

    Deliberately exercised daily: Google revokes refresh tokens unused for six
    months, and in a quiet season the alerts token has no organic use — it
    would die precisely because nothing went wrong.
    """
    get_alerts_service().users().getProfile(userId="me").execute()


def _check_google_sheets() -> None:
    """Prove the Sheets token works against the real spreadsheet (metadata only)."""
    config = load_config()
    spreadsheet_id = config.properties[0].cleaner_schedule.spreadsheet_id
    get_sheets_service().spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="spreadsheetId"
    ).execute()


def _check_seam() -> None:
    """Prove the Seam API key works and the real lock is still visible."""
    config = load_config()
    get_seam_client().devices.get(device_id=config.properties[0].seam_device_id)


def _check_dashboard_oauth_client() -> None:
    """Prove the dashboard's Google *web* OAuth client secret is still valid.

    There is no stored refresh token for the web client (sessions are
    per-user), so probe Google's token endpoint with a deliberately bogus
    authorization code. Google validates the client credentials FIRST:
    ``invalid_client`` means the secret is dead; ``invalid_grant`` (or any
    other grant-level error) means the client credentials passed — which is
    all this check asserts.
    """
    response = httpx.post(
        _GOOGLE_TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "code": "credential-sentinel-probe",
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uri": f"https://{settings.domain}/auth/callback",
        },
        timeout=30,
    )
    error = response.json().get("error", "")
    if error == "invalid_client":
        raise RuntimeError(
            "Google rejected the dashboard OAuth client credentials (invalid_client)"
        )


_CREDENTIAL_CHECKS: tuple[tuple[str, Callable[[], None]], ...] = (
    ("gmail_booking_feed", _check_gmail_booking_feed),
    ("gmail_alerts", _check_gmail_alerts),
    ("google_sheets", _check_google_sheets),
    ("seam", _check_seam),
    ("dashboard_oauth_client", _check_dashboard_oauth_client),
)


async def verify_credentials() -> None:
    """Daily job: actively verify every integration credential (read-only).

    All checks pass → ping the external sentinel heartbeat; any failure →
    skip the ping (heartbeat silence is the primary, Gmail-independent alarm)
    and additionally attempt the detail alert email. Per-check isolation:
    one dead credential must not hide the state of the others. Never raises.
    """
    log.info("verify_credentials: run started")
    failures: list[dict] = []
    for name, check in _CREDENTIAL_CHECKS:
        try:
            # F11: every probe is sync network I/O — keep it off the event loop.
            await asyncio.to_thread(check)
        except Exception as exc:  # noqa: BLE001 — the failure IS the finding
            log.exception("verify_credentials: %s check FAILED", name)
            failures.append({"credential": name, "error": str(exc)})

    if not failures:
        log.info(
            "verify_credentials: all %d credential checks passed",
            len(_CREDENTIAL_CHECKS),
        )
        await ping_heartbeat_async(
            settings.healthchecks_ping_url_sentinel, label="credential-sentinel"
        )
        return

    try:
        config = load_config()
        await asyncio.to_thread(
            send_credential_sentinel_alert,
            failures=failures,
            alerts_service=get_alerts_service(),
            alerts_address=config.email.alerts,
            dashboard_base_url=f"https://{settings.domain}",
        )
    except Exception:  # noqa: BLE001 — if the alerts token died, this is expected
        log.exception(
            "verify_credentials: failure alert could not be sent (external "
            "heartbeat silence is the primary alarm)"
        )


# ===========================================================================
# Classifier-drift weekly digest (sustainability audit 2026-07-23, item 4)
# ===========================================================================

# Sender domains whose OTHER dead-letters deserve weekly human review. A
# reworded booking subject from one of these would be dead-lettered silently
# (the parse-failure alert only covers mail that classified AS a booking).
_PLATFORM_DRIFT_DOMAINS: tuple[str, ...] = (
    "airbnb.com",
    "vrbo.com",
    "homeaway.com",
    "expediagroup.com",
)


def _is_platform_sender(sender: str | None) -> bool:
    if not sender:
        return False
    domain = _extract_sender_domain(sender)
    return any(
        domain == d or domain.endswith("." + d) for d in _PLATFORM_DRIFT_DOMAINS
    )


def _extract_sender_domain(address: str) -> str:
    from app.ingestion.classifier import _extract_domain

    return _extract_domain(address)


async def check_classifier_drift() -> None:
    """Weekly job: surface platform-domain OTHER dead-letters for human review.

    TODO(future): a second, LLM-based layer was designed (weekly in-app job
    asking Claude whether any OTHER dead-letter looks like a real booking,
    detect-and-alert only) and deliberately deferred by the owner on
    2026-07-24. Until it ships, this digest is the only drift mitigation —
    the owner must actually read it.
    """
    log.info("check_classifier_drift: run started")
    cutoff = datetime.now(UTC) - timedelta(days=7)
    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(ProcessedMessage).where(
                        ProcessedMessage.disposition == "other",
                        ProcessedMessage.created_at >= cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )

    suspects = [r for r in rows if _is_platform_sender(r.sender)]
    if not suspects:
        log.info(
            "check_classifier_drift: no platform-domain OTHER dead-letters "
            "in the last 7 days"
        )
        return

    items = [
        {
            "date": str(r.created_at.date()) if r.created_at else "?",
            "sender": r.sender,
            "subject": r.subject or "(no subject)",
        }
        for r in suspects
    ]
    try:
        config = load_config()
        await asyncio.to_thread(  # F11: sync Gmail send off the loop
            send_classifier_drift_digest,
            items=items,
            alerts_service=get_alerts_service(),
            alerts_address=config.email.alerts,
            dashboard_base_url=f"https://{settings.domain}",
        )
        log.info("check_classifier_drift: digest sent (%d suspect emails)", len(items))
    except Exception:  # noqa: BLE001 — job must not raise
        log.exception("check_classifier_drift: digest send failed")


# ===========================================================================
# Monthly systems-normal report (sustainability audit 2026-07-23, item 3)
# ===========================================================================

async def _gather_monthly_stats() -> dict:
    """Collect the vitals for the monthly report (read-only)."""
    cutoff_30d = datetime.now(UTC) - timedelta(days=30)
    async with AsyncSessionLocal() as session:
        active = (
            await session.execute(
                select(func.count())
                .select_from(Booking)
                .where(Booking.status == BookingStatus.ACTIVE)
            )
        ).scalar_one()
        completed = (
            await session.execute(
                select(func.count())
                .select_from(Booking)
                .where(
                    Booking.status == BookingStatus.COMPLETED,
                    Booking.check_out_date >= cutoff_30d.date(),
                )
            )
        ).scalar_one()
        failed = (
            await session.execute(
                select(func.count())
                .select_from(BookingTask)
                .where(BookingTask.state == TaskState.FAILED)
            )
        ).scalar_one()
        stuck = (
            await session.execute(
                select(func.count())
                .select_from(BookingTask)
                .where(BookingTask.state == TaskState.IN_PROGRESS)
            )
        ).scalar_one()
        dead_other = (
            await session.execute(
                select(func.count())
                .select_from(ProcessedMessage)
                .where(
                    ProcessedMessage.disposition == "other",
                    ProcessedMessage.created_at >= cutoff_30d,
                )
            )
        ).scalar_one()
        dead_error = (
            await session.execute(
                select(func.count())
                .select_from(ProcessedMessage)
                .where(
                    ProcessedMessage.disposition != "other",
                    ProcessedMessage.created_at >= cutoff_30d,
                )
            )
        ).scalar_one()

    token_age_days: int | None = None
    try:
        from app.integrations.docusign.client import _TOKEN_STORE

        if _TOKEN_STORE.exists():
            mtime = datetime.fromtimestamp(_TOKEN_STORE.stat().st_mtime, tz=UTC)
            token_age_days = (datetime.now(UTC) - mtime).days
    except Exception:  # noqa: BLE001 — stats must not kill the report
        log.exception("_gather_monthly_stats: token store check failed")

    return {
        "active_bookings": active,
        "completed_last_30d": completed,
        "failed_tasks": failed,
        "stuck_in_progress": stuck,
        "dead_letters_other_30d": dead_other,
        "dead_letters_error_30d": dead_error,
        "docusign_token_store_age_days": token_age_days,
    }


async def send_monthly_status_email() -> None:
    """Monthly job: positive-confirmation status email to the owner.

    Sent even when everything is fine — it proves the alerts token and send
    path end-to-end (defeating Google's 6-month-inactivity revocation), and
    its absence is itself the alarm. Never raises.
    """
    log.info("send_monthly_status_email: run started")
    try:
        stats = await _gather_monthly_stats()
        config = load_config()
        await asyncio.to_thread(  # F11: sync Gmail send off the loop
            send_monthly_status_report,
            stats=stats,
            alerts_service=get_alerts_service(),
            alerts_address=config.email.alerts,
            dashboard_base_url=f"https://{settings.domain}",
        )
        log.info("send_monthly_status_email: report sent")
    except Exception:  # noqa: BLE001 — job must not raise
        log.exception("send_monthly_status_email: failed")
