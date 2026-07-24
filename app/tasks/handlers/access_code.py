from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Booking, BookingTask, DataPoint, DataPointSource, TaskState
from app.integrations.seam.client import get_seam_client

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def _last_four_digits(phone: str) -> str:
    """Return the last 4 digits of a phone number string.

    Strips all non-digit characters first, then takes the last 4.
    Raises ValueError if fewer than 4 digits remain.
    """
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 4:
        raise ValueError(f"Phone '{phone}' has fewer than 4 digits: got {len(digits)}")
    return digits[-4:]


def _checkin_window(
    check_in_date: date,
    check_out_date: date,
) -> tuple[datetime, datetime]:
    """Return (starts_at_utc, ends_at_utc) for the access code activation window.

    - starts_at: 4:00 PM US/Eastern on check-in day
    - ends_at:  11:00 AM US/Eastern on check-out day

    The ZoneInfo library handles DST transitions automatically so spring-forward
    and fall-back dates yield the correct UTC offsets.
    """
    starts_local = datetime(
        check_in_date.year, check_in_date.month, check_in_date.day,
        16, 0, 0,
        tzinfo=ET,
    )
    ends_local = datetime(
        check_out_date.year, check_out_date.month, check_out_date.day,
        11, 0, 0,
        tzinfo=ET,
    )
    return (
        starts_local.astimezone(timezone.utc),
        ends_local.astimezone(timezone.utc),
    )


async def handle_access_code_create(
    booking: Booking,
    task: BookingTask,
    session: AsyncSession,
) -> None:
    """Create a time-bound Seam access code for the booking.

    SEAM-03: If booking.guest_phone is None, sets task.state = WAITING and
    returns without calling the Seam SDK. The dispatcher will retry once the
    owner enters the phone number.
    """
    # Idempotency guard (Step 17): if a code was already created for this task,
    # never create a second one — just mark COMPLETE. Defends against a
    # re-dispatch of a task whose external side effect already happened.
    if task.external_ref:
        log.info(
            "Seam access code already exists for booking %s (%s); skipping re-create",
            booking.id, task.external_ref,
        )
        task.state = TaskState.COMPLETE
        if task.completed_at is None:
            task.completed_at = datetime.now(timezone.utc)
        return

    if booking.guest_phone is None:
        task.state = TaskState.WAITING
        log.info(
            "Booking %s missing phone; access code task -> WAITING", booking.id
        )
        return

    task.state = TaskState.IN_PROGRESS

    from app.config import load_config

    config = load_config()
    prop = next(
        (p for p in config.properties if p.id == booking.property_id), None
    )
    if prop is None:
        raise ValueError(
            f"No property config found for property_id={booking.property_id!r}"
        )

    code = _last_four_digits(booking.guest_phone)
    starts_at, ends_at = _checkin_window(booking.check_in_date, booking.check_out_date)

    client = get_seam_client()
    result = await asyncio.to_thread(
        client.access_codes.create,
        device_id=prop.seam_device_id,
        code=code,
        name=f"{booking.guest_first_name} {booking.guest_last_name}",
        starts_at=starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ends_at=ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    task.external_ref = result.access_code_id
    task.state = TaskState.COMPLETE
    task.completed_at = datetime.now(timezone.utc)

    # Record the code Seam ACTUALLY set (from the create response, never our
    # requested value) so the dashboard can display the real device code.
    seam_code = getattr(result, "code", None)
    if seam_code:
        session.add(
            DataPoint(
                booking_id=booking.id,
                field_name="access_code",
                value=str(seam_code),
                source=DataPointSource.SEAM_API,
                notes=f"Set by Seam (access_code_id {result.access_code_id})",
            )
        )

    log.info(
        "Seam access code %s created for booking %s",
        result.access_code_id,
        booking.id,
    )
