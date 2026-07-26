"""Dashboard router for the rental automation service.

Provides a Google-login-protected owner dashboard (see app/routers/auth.py) with:
- Booking list (active + recent cancellations)
- Booking detail with provenance and task status
- Manual contact-info entry (phone, email) with PRG redirect
"""
from __future__ import annotations

import asyncio
import enum
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import load_config
from app.db.models import (
    Booking,
    BookingStatus,
    BookingTask,
    DataPoint,
    DataPointSource,
    Platform,
    TaskState,
    TaskType,
    UnknownEnumValue,
)
from app.db.session import get_db
from app.ingestion.cancellation import delete_seam_access_code
from app.integrations.hoa.window import hoa_window, today_et
from app.routers.auth import require_user
from app.tasks.dispatch import _dispatch_pending_tasks
from app.tasks.handlers.docusign import void_envelope_idempotent

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Strong references to background tasks — prevents GC before completion.
# See: https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
_background_tasks: set[asyncio.Task] = set()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVENANCE_FIELDS = [
    "guest_first_name",
    "guest_last_name",
    "guest_phone",
    "guest_email",
    "check_in_date",
    "check_out_date",
]

EMAIL_DEPENDENT_TASKS = {TaskType.DOCUSIGN_SEND}
PHONE_DEPENDENT_TASKS = {TaskType.ACCESS_CODE_CREATE}

# Reminders made moot by the corresponding contact field being entered.
EMAIL_REMINDER_TASKS = {
    TaskType.OWNER_ALERT_MISSING_EMAIL_7D,
    TaskType.OWNER_ALERT_MISSING_EMAIL_4D,
}
PHONE_REMINDER_TASKS = {
    TaskType.OWNER_ALERT_MISSING_PHONE_7D,
    TaskType.OWNER_ALERT_MISSING_PHONE_4D,
}

TASK_GROUPS: dict[str, list[TaskType]] = {
    "Alerts": [
        TaskType.OWNER_ALERT_NEW_BOOKING,
        TaskType.OWNER_ALERT_CANCELLATION_HOA,
        TaskType.OWNER_ALERT_CANCELLATION_CLEANER,
    ],
    "Automations": [
        TaskType.DOCUSIGN_SEND,
        TaskType.CLEANER_SHEET_ADD,
        TaskType.ACCESS_CODE_CREATE,
        TaskType.HOA_EMAIL,
    ],
    "Reminders": [
        TaskType.OWNER_ALERT_MISSING_PHONE_7D,
        TaskType.OWNER_ALERT_MISSING_PHONE_4D,
        TaskType.OWNER_ALERT_MISSING_EMAIL_7D,
        TaskType.OWNER_ALERT_MISSING_EMAIL_4D,
        TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D,
        TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D,
    ],
}

# Human names for task rows on the detail page (raw enum values read like logs).
TASK_LABELS: dict[TaskType, str] = {
    TaskType.OWNER_ALERT_NEW_BOOKING: "New-booking alert",
    TaskType.DOCUSIGN_SEND: "Send guest form (DocuSign)",
    TaskType.CLEANER_SHEET_ADD: "Add cleaner schedule row",
    TaskType.ACCESS_CODE_CREATE: "Create door access code",
    TaskType.HOA_EMAIL: "Email registration to HOA",
    TaskType.OWNER_ALERT_MISSING_PHONE_7D: "Reminder: phone still missing (7 days out)",
    TaskType.OWNER_ALERT_MISSING_PHONE_4D: "Reminder: phone still missing (4 days out)",
    TaskType.OWNER_ALERT_MISSING_EMAIL_7D: "Reminder: email still missing (7 days out)",
    TaskType.OWNER_ALERT_MISSING_EMAIL_4D: "Reminder: email still missing (4 days out)",
    TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D: "Reminder: form still unsigned (7 days out)",
    TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D: "Reminder: form still unsigned (4 days out)",
    TaskType.OWNER_ALERT_CANCELLATION_HOA: "Cancellation: notify HOA (manual)",
    TaskType.OWNER_ALERT_CANCELLATION_CLEANER: "Cancellation: remove cleaner row (manual)",
}

# Reminders are notifications, not work items — relabel their states in owner
# terms: a PENDING reminder is merely scheduled, a SKIPPED one wasn't needed.
REMINDER_STATE_LABELS: dict[str, str] = {
    TaskState.PENDING.value: "scheduled",
    TaskState.WAITING.value: "scheduled",
    TaskState.IN_PROGRESS.value: "sending",
    TaskState.COMPLETE.value: "sent",
    TaskState.SKIPPED.value: "not needed",
    TaskState.FAILED.value: "failed",
}

SOURCE_LABELS: dict[str, str] = {
    DataPointSource.EMAIL_PARSE.value: "email parse",
    DataPointSource.MANUAL_ENTRY.value: "manual entry",
    DataPointSource.WEB_SCRAPE.value: "web scrape",
    DataPointSource.DOCUSIGN_WEBHOOK.value: "DocuSign webhook",
    DataPointSource.SEAM_API.value: "Seam API",
    DataPointSource.SYSTEM_DEFAULT.value: "system default",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _readable_enum_value(value: enum.Enum | UnknownEnumValue) -> str:
    return value.value.replace("_", " ")


def _platform_label(value: Platform | UnknownEnumValue) -> str:
    if isinstance(value, Platform):
        return value.value
    return f"unknown ({_readable_enum_value(value)})"


def _booking_status_label(value: BookingStatus | UnknownEnumValue) -> str:
    if isinstance(value, BookingStatus):
        return value.value.replace("_", " ")
    return f"unknown ({_readable_enum_value(value)})"


def _task_label(value: TaskType | UnknownEnumValue) -> str:
    if isinstance(value, TaskType):
        return TASK_LABELS[value]
    return f"Unknown task type ({_readable_enum_value(value)})"


def _task_state_label(
    value: TaskState | UnknownEnumValue,
    *,
    reminder: bool,
) -> str:
    if not isinstance(value, TaskState):
        return f"unknown ({_readable_enum_value(value)})"
    if reminder:
        return REMINDER_STATE_LABELS[value.value]
    return value.value.replace("_", " ")


def _enum_badge(value: enum.Enum | UnknownEnumValue) -> str:
    return "unknown" if isinstance(value, UnknownEnumValue) else value.value


def _source_label(value: DataPointSource | UnknownEnumValue) -> str:
    if isinstance(value, DataPointSource):
        return SOURCE_LABELS[value.value]
    return f"unknown ({_readable_enum_value(value)})"


def _validate_phone(raw: str) -> str | None:
    """Return stripped digit string (10+ digits) or None on failure."""
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) >= 10 else None


def _validate_email(raw: str) -> str | None:
    """Return normalized email if valid, else None."""
    _, addr = parseaddr(raw)
    if "@" in addr and "." in addr.split("@", 1)[-1]:
        return addr
    return None


def _flip_waiting_tasks(
    booking: Booking,
    *,
    email_saved: bool,
    phone_saved: bool,
) -> None:
    """Flip WAITING tasks to PENDING for newly available fields.

    Also flips the now-moot missing-field reminders PENDING→SKIPPED: once the
    field is entered they can never fire, and leaving them PENDING misreads as
    outstanding work on the dashboard. Fired reminders (COMPLETE) are history
    and stay untouched.
    """
    for task in booking.tasks:
        if email_saved and task.task_type in EMAIL_DEPENDENT_TASKS:
            if task.state == TaskState.WAITING:
                task.state = TaskState.PENDING
        if phone_saved and task.task_type in PHONE_DEPENDENT_TASKS:
            if task.state == TaskState.WAITING:
                task.state = TaskState.PENDING
        if email_saved and task.task_type in EMAIL_REMINDER_TASKS:
            if task.state == TaskState.PENDING:
                task.state = TaskState.SKIPPED
        if phone_saved and task.task_type in PHONE_REMINDER_TASKS:
            if task.state == TaskState.PENDING:
                task.state = TaskState.SKIPPED


def _reset_access_code_for_recreate(booking: Booking) -> None:
    """F13: a phone correction after ACCESS_CODE_CREATE completed invalidates
    the code already on the lock. Delete it and reset the task to PENDING so
    the dispatcher creates a fresh one for the corrected number — reuses
    handle_access_code_create's existing external_ref idempotency guard, no
    handler changes needed.

    Sync Seam call inside this async route: same tradeoff as
    app.ingestion.cancellation.delete_seam_access_code's other caller —
    owner-triggered corrections are rare, so the momentary event-loop block
    is acceptable over asyncio.run() complexity.
    """
    task = next(
        (t for t in booking.tasks if t.task_type == TaskType.ACCESS_CODE_CREATE),
        None,
    )
    if task is None or task.state != TaskState.COMPLETE or not task.external_ref:
        return
    old_ref = task.external_ref
    delete_seam_access_code(old_ref)
    task.external_ref = None
    task.state = TaskState.PENDING
    task.completed_at = None
    task.last_error = None
    log.info(
        "Dashboard: deleted Seam access code %s for booking %s after phone correction",
        old_ref,
        booking.id,
    )


def _reset_docusign_for_resend(booking: Booking) -> None:
    """F13: an email correction after DOCUSIGN_SEND completed means the
    envelope went to the wrong address. If the guest hasn't signed yet, void
    it and reset the task so the dispatcher sends a fresh one. Once signed
    (booking.signed_pdf_path set), voiding would fail anyway — DocuSign
    envelopes are terminal on completion — and there's nothing to resend:
    the guest already has a validly signed copy.
    """
    if booking.signed_pdf_path is not None:
        return
    task = next(
        (t for t in booking.tasks if t.task_type == TaskType.DOCUSIGN_SEND),
        None,
    )
    if task is None or task.state != TaskState.COMPLETE or not task.external_ref:
        return
    old_ref = task.external_ref
    void_envelope_idempotent(old_ref)
    task.external_ref = None
    task.state = TaskState.PENDING
    task.completed_at = None
    task.last_error = None
    log.info(
        "Dashboard: voided DocuSign envelope %s for booking %s after email correction",
        old_ref,
        booking.id,
    )


# Owner-facing booking statuses, most urgent first. Each feeds `badge-{status}`
# in the templates, so style.css must carry a rule per value (asserted in tests).
BOOKING_STATUSES = (
    "failed",
    "action_needed",
    "in_progress",
    "waiting_on_guest",
    "scheduled",
    "complete",
    "pending",
)


def _summarize_tasks(booking: Booking) -> dict[str, object]:
    """Derive the owner-facing status + task counts for a booking.

    Presentation-only: reads existing task states plus the booking's contact
    fields and signed_pdf_path; never creates, dispatches or mutates anything.
    The status answers "who is the ball with":

      failed          — an automation errored; the owner should look
      action_needed   — waiting on the owner (a contact field is missing)
      in_progress     — an automation is running right now
      waiting_on_guest— form sent, the guest hasn't signed yet
      scheduled       — only the HOA email remains, waiting for its window
      complete        — every task settled
      pending         — transient dispatch states (nothing is blocked)
    """
    tasks = list(booking.tasks)
    states = [t.state for t in tasks]
    # done/total cover the automations only — reminders and alerts are
    # notifications, not work items, and would inflate the progress metric.
    auto_states = [t.state for t in tasks if t.task_type in TASK_GROUPS["Automations"]]
    total = len(auto_states)
    done = sum(1 for s in auto_states if s in (TaskState.COMPLETE, TaskState.SKIPPED))
    # A failure anywhere must surface, so failed counts every task row.
    failed = sum(1 for s in states if s == TaskState.FAILED)
    all_settled = bool(tasks) and all(
        s in (TaskState.COMPLETE, TaskState.SKIPPED) for s in states
    )

    docusign_sent = any(
        t.task_type == TaskType.DOCUSIGN_SEND and t.state == TaskState.COMPLETE
        for t in tasks
    )
    hoa_waiting = any(
        t.task_type == TaskType.HOA_EMAIL and t.state == TaskState.WAITING
        for t in tasks
    )
    only_hoa_left = hoa_waiting and all(
        t.state in (TaskState.COMPLETE, TaskState.SKIPPED)
        for t in tasks
        if t.task_type != TaskType.HOA_EMAIL
    )

    if failed:
        status = "failed"
    elif not tasks:
        status = "pending"
    elif booking.guest_email is None or booking.guest_phone is None:
        status = "action_needed"
    elif TaskState.IN_PROGRESS in states:
        status = "in_progress"
    elif docusign_sent and booking.signed_pdf_path is None:
        status = "waiting_on_guest"
    elif only_hoa_left:
        status = "scheduled"
    elif all_settled:
        status = "complete"
    else:
        status = "pending"

    return {"status": status, "done": done, "total": total, "failed": failed}


def _hoa_pipeline(
    booking: Booking, tasks_by_type: dict[TaskType, BookingTask]
) -> dict[str, object] | None:
    """Timeline data for the HOA-registration panel on the detail page.

    Reads existing state only. Returns None when the property's config is
    unavailable (the panel is simply omitted rather than failing the page).
    """
    try:
        config = load_config()
        prop = next(p for p in config.properties if p.id == booking.property_id)
    except Exception:
        log.warning(
            "Dashboard: no property config for booking %s; omitting HOA panel",
            booking.id,
        )
        return None

    earliest, latest = hoa_window(
        booking.check_in_date,
        prop.hoa.open_days,
        prop.hoa.email_window_days_min,
        prop.hoa.email_window_days_max,
    )

    send_task = tasks_by_type.get(TaskType.DOCUSIGN_SEND)
    hoa_task = tasks_by_type.get(TaskType.HOA_EMAIL)
    docusign_sent = send_task is not None and send_task.state == TaskState.COMPLETE
    hoa_sent = hoa_task is not None and hoa_task.state == TaskState.COMPLETE

    # ET date, never the server (UTC) date — the panel must agree with the
    # send logic in the handlers (bug hunt F2).
    today = today_et()
    if hoa_sent:
        window_status = "sent"
    elif today < earliest:
        window_status = "before"
    elif today <= latest:
        window_status = "open"
    else:
        window_status = "passed"

    return {
        "docusign_sent": docusign_sent,
        "docusign_sent_at": send_task.completed_at if docusign_sent else None,
        "signed": booking.signed_pdf_path is not None,
        "earliest": earliest,
        "latest": latest,
        "hoa_sent": hoa_sent,
        "hoa_sent_at": hoa_task.completed_at if hoa_sent else None,
        "window_status": window_status,
    }


def _detail_context(booking: Booking) -> dict[str, object]:
    """Shared template context for the booking detail page."""
    tasks_by_type = {t.task_type: t for t in booking.tasks}
    task_groups = dict(TASK_GROUPS)
    unknown_task_types = [
        task.task_type
        for task in booking.tasks
        if isinstance(task.task_type, UnknownEnumValue)
    ]
    if unknown_task_types:
        task_groups["Unknown tasks"] = unknown_task_types
    return {
        "booking": booking,
        "provenance": _build_provenance_map(booking),
        "task_groups": task_groups,
        "tasks_by_type": tasks_by_type,
        "platform_label": _platform_label,
        "booking_status_label": _booking_status_label,
        "task_label": _task_label,
        "task_state_label": _task_state_label,
        "enum_badge": _enum_badge,
        "source_label": _source_label,
        "summary": _summarize_tasks(booking),
        "hoa_pipeline": _hoa_pipeline(booking, tasks_by_type),
        "access_code": _latest_access_code(booking),
    }


def _latest_access_code(booking: Booking) -> str | None:
    """Most recent door code recorded from the Seam API, or None.

    Reads the access_code DataPoint written by handle_access_code_create —
    the value Seam actually set, deliberately not derived from the phone.
    """
    points = [dp for dp in booking.data_points if dp.field_name == "access_code"]
    if not points:
        return None
    return max(points, key=lambda d: d.recorded_at).value


def _build_provenance_map(booking: Booking) -> dict[str, DataPoint | None]:
    """Build a map of field_name -> most recent DataPoint for provenance display."""
    result: dict[str, DataPoint | None] = {f: None for f in PROVENANCE_FIELDS}
    for dp in sorted(booking.data_points, key=lambda d: d.recorded_at):
        if dp.field_name in result:
            result[dp.field_name] = dp  # last write wins = most recent
    return result


# ---------------------------------------------------------------------------
# Routes  (auth via require_user — Google OIDC session, see app/routers/auth.py)
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def booking_list(
    request: Request,
    _: str = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    today = today_et()
    thirty_days_ago_dt = datetime.now(timezone.utc) - timedelta(days=30)

    # selectinload: the list view reads booking.tasks for the status/task
    # summary, and async SQLAlchemy cannot lazy-load it mid-render.
    active_result = await db.execute(
        select(Booking)
        # A future release may add a booking status. An older rollback build
        # must keep that row visible rather than silently omitting it.
        .where(
            Booking.status.not_in(
                (BookingStatus.CANCELLED, BookingStatus.COMPLETED)
            )
        )
        .options(selectinload(Booking.tasks))
        .order_by(Booking.check_in_date.asc())
    )
    active = active_result.scalars().all()

    cancelled_result = await db.execute(
        select(Booking)
        .where(
            Booking.status == BookingStatus.CANCELLED,
            Booking.updated_at >= thirty_days_ago_dt,
        )
        .order_by(Booking.updated_at.desc())
    )
    cancelled = cancelled_result.scalars().all()

    # F15: COMPLETED bookings fall out of both queries above — without this
    # they'd be invisible on the dashboard the day after complete_past_bookings
    # runs. Mirrors the "recent cancellations" window.
    completed_result = await db.execute(
        select(Booking)
        .where(
            Booking.status == BookingStatus.COMPLETED,
            Booking.check_out_date >= today - timedelta(days=30),
        )
        .order_by(Booking.check_out_date.desc())
    )
    completed = completed_result.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="list.html",
        context={
            "active_bookings": active,
            "cancelled_bookings": cancelled,
            "completed_bookings": completed,
            "task_summaries": {b.id: _summarize_tasks(b) for b in active},
            "platform_label": _platform_label,
            "booking_status_label": _booking_status_label,
            "enum_badge": _enum_badge,
            "today": today,
        },
    )


@router.get("/bookings/{booking_id}", response_class=HTMLResponse)
async def booking_detail(
    booking_id: uuid.UUID,
    request: Request,
    _: str = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = result.scalar_one_or_none()
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found")

    await db.refresh(booking, attribute_names=["tasks", "data_points"])

    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            **_detail_context(booking),
            "errors": {},
            "submitted_phone": None,
            "submitted_email": None,
        },
    )


@router.post("/bookings/{booking_id}/contact")
async def save_contact(
    booking_id: uuid.UUID,
    request: Request,
    guest_phone: str | None = Form(None),
    guest_email: str | None = Form(None),
    confirm_overwrite: str | None = Form(None),
    _: str = Depends(require_user),
    db: AsyncSession = Depends(get_db),
):
    # Pitfall 7: coerce empty string to None before validation
    guest_phone = guest_phone or None
    guest_email = guest_email or None
    confirmed = confirm_overwrite is not None

    result = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = result.scalar_one_or_none()
    if booking is None:
        log.warning("Dashboard: booking %s not found in save_contact", booking_id)
        raise HTTPException(status_code=404, detail="Booking not found")

    # F5 (bug hunt 2026-07-22): contact entry on a cancelled booking would
    # dispatch real side effects (envelope, door code, cleaner row) for a stay
    # that isn't happening. The template hides the form; this guards direct
    # POSTs and the stale-tab race (cancellation lands while the form is open).
    if booking.status != BookingStatus.ACTIVE:
        log.warning(
            "Dashboard: refusing contact save for non-active booking %s (%s)",
            booking_id,
            booking.status.value,
        )
        raise HTTPException(
            status_code=409,
            detail="Booking is not active — contact entry is disabled",
        )

    await db.refresh(booking, attribute_names=["tasks", "data_points"])

    errors: dict[str, str] = {}
    phone_clean: str | None = None
    email_clean: str | None = None

    if guest_phone is not None:
        phone_clean = _validate_phone(guest_phone)
        if phone_clean is None:
            errors["guest_phone"] = "Enter a valid phone number (at least 10 digits)."

    if guest_email is not None:
        email_clean = _validate_email(guest_email)
        if email_clean is None:
            errors["guest_email"] = "Enter a valid email address."

    # F13 (bug hunt 2026-07-22): contact fields are editable, not write-once.
    # Overwriting an already-set value needs the confirm checkbox — a typo'd
    # resubmit shouldn't silently re-trigger the door-code/envelope automations.
    phone_changed = phone_clean is not None and phone_clean != booking.guest_phone
    email_changed = email_clean is not None and email_clean != booking.guest_email
    overwrite_needed = (phone_changed and booking.guest_phone is not None) or (
        email_changed and booking.guest_email is not None
    )
    if overwrite_needed and not confirmed:
        errors["confirm_overwrite"] = (
            'Check "confirm change" to overwrite the previously saved value.'
        )

    if errors:
        return templates.TemplateResponse(
            request=request,
            name="detail.html",
            context={
                **_detail_context(booking),
                "errors": errors,
                "submitted_phone": guest_phone,
                "submitted_email": guest_email,
                "confirm_overwrite": confirmed,
            },
        )

    # D-13: all writes in a single transaction
    email_saved = email_changed
    phone_saved = phone_changed

    if email_saved:
        booking.guest_email = email_clean
        db.add(DataPoint(
            booking_id=booking.id,
            field_name="guest_email",
            value=email_clean,
            source=DataPointSource.MANUAL_ENTRY,
        ))
    if phone_saved:
        booking.guest_phone = phone_clean
        db.add(DataPoint(
            booking_id=booking.id,
            field_name="guest_phone",
            value=phone_clean,
            source=DataPointSource.MANUAL_ENTRY,
        ))

    _flip_waiting_tasks(booking, email_saved=email_saved, phone_saved=phone_saved)

    # F13: a correction after the dependent automation already completed
    # invalidates its external side effect — void/delete it and reset the
    # task so the dispatcher (fired below) creates a fresh one instead of
    # skipping via the external_ref idempotency guard.
    if phone_saved:
        _reset_access_code_for_recreate(booking)
    if email_saved:
        _reset_docusign_for_resend(booking)

    await db.commit()  # MUST be before RedirectResponse (Pitfall 4)

    # D-14: fire background dispatch — pass only UUID, not the ORM object (Pitfall 3)
    # Hold a strong reference to prevent GC before completion (Python asyncio docs).
    task = asyncio.create_task(_dispatch_pending_tasks(booking_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    log.info(
        "Dashboard: saved contact for booking %s (email_saved=%s, phone_saved=%s)",
        booking_id,
        email_saved,
        phone_saved,
    )

    return RedirectResponse(url=f"/bookings/{booking_id}", status_code=303)
