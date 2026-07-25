"""Integration tests for the booking schema against real PostgreSQL."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


async def test_can_insert_and_fetch_booking(db_session):
    from app.db.models import Booking, BookingStatus, Platform

    booking = Booking(
        platform=Platform.AIRBNB,
        external_id="HMABC123",
        property_id="property_1",
        guest_first_name="Alice",
        guest_last_name="Sample",
        check_in_date=date(2026, 6, 1),
        check_out_date=date(2026, 6, 5),
        source_email_message_id="msg-1",
    )
    db_session.add(booking)
    await db_session.commit()

    result = await db_session.execute(select(Booking).where(Booking.external_id == "HMABC123"))
    fetched = result.scalar_one()
    assert fetched.guest_first_name == "Alice"
    assert fetched.status == BookingStatus.ACTIVE
    assert fetched.guest_phone is None
    assert fetched.guest_email is None


async def test_unique_platform_external_id(db_session):
    from app.db.models import Booking, Platform

    base = dict(
        property_id="property_1",
        guest_first_name="A", guest_last_name="B",
        check_in_date=date(2026, 6, 1), check_out_date=date(2026, 6, 5),
    )
    db_session.add(
        Booking(platform=Platform.AIRBNB, external_id="DUP", source_email_message_id="m1", **base)
    )
    await db_session.commit()

    db_session.add(
        Booking(platform=Platform.AIRBNB, external_id="DUP", source_email_message_id="m2", **base)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    # Same external_id is fine across platforms.
    db_session.add(
        Booking(platform=Platform.VRBO, external_id="DUP", source_email_message_id="m3", **base)
    )
    await db_session.commit()


async def test_unique_source_email_message_id(db_session):
    from app.db.models import Booking, Platform

    base = dict(
        property_id="property_1",
        guest_first_name="A", guest_last_name="B",
        check_in_date=date(2026, 6, 1), check_out_date=date(2026, 6, 5),
    )
    db_session.add(
        Booking(
            platform=Platform.AIRBNB, external_id="E1", source_email_message_id="same", **base
        )
    )
    await db_session.commit()

    db_session.add(
        Booking(
            platform=Platform.AIRBNB, external_id="E2", source_email_message_id="same", **base
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_booking_tasks_cascade_delete(db_session):
    from app.db.models import Booking, BookingTask, Platform, TaskState, TaskType

    booking = Booking(
        platform=Platform.VRBO, external_id="V1", property_id="property_1",
        guest_first_name="Carl", guest_last_name="Doe",
        check_in_date=date(2026, 7, 1), check_out_date=date(2026, 7, 4),
        source_email_message_id="vmsg-1",
    )
    booking.tasks.append(BookingTask(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.PENDING))
    booking.tasks.append(BookingTask(task_type=TaskType.CLEANER_SHEET_ADD, state=TaskState.PENDING))
    db_session.add(booking)
    await db_session.commit()

    await db_session.refresh(booking, attribute_names=["tasks"])
    assert len(booking.tasks) == 2

    await db_session.delete(booking)
    await db_session.commit()

    remaining = (await db_session.execute(select(BookingTask))).scalars().all()
    assert remaining == []


async def test_booking_task_unique_on_booking_and_task_type(db_session):
    from app.db.models import Booking, BookingTask, Platform, TaskState, TaskType

    booking = Booking(
        platform=Platform.AIRBNB, external_id="UQ1", property_id="property_1",
        guest_first_name="X", guest_last_name="Y",
        check_in_date=date(2026, 8, 1), check_out_date=date(2026, 8, 2),
        source_email_message_id="uqmsg",
    )
    db_session.add(booking)
    await db_session.commit()

    db_session.add(
        BookingTask(booking_id=booking.id, task_type=TaskType.HOA_EMAIL, state=TaskState.PENDING)
    )
    await db_session.commit()

    db_session.add(
        BookingTask(booking_id=booking.id, task_type=TaskType.HOA_EMAIL, state=TaskState.PENDING)
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_data_points_record_provenance(db_session):
    from app.db.models import Booking, DataPoint, DataPointSource, Platform

    booking = Booking(
        platform=Platform.AIRBNB, external_id="DP1", property_id="property_1",
        guest_first_name="Eve", guest_last_name="Smith",
        check_in_date=date(2026, 9, 1), check_out_date=date(2026, 9, 3),
        source_email_message_id="dpmsg",
    )
    booking.data_points.extend([
        DataPoint(field_name="guest_first_name", value="Eve", source=DataPointSource.EMAIL_PARSE),
        DataPoint(field_name="guest_phone", value="+15551234567",
                  source=DataPointSource.MANUAL_ENTRY,
                  notes="entered by primary owner"),
    ])
    db_session.add(booking)
    await db_session.commit()

    await db_session.refresh(booking, attribute_names=["data_points"])
    by_field = {dp.field_name: dp for dp in booking.data_points}
    assert by_field["guest_first_name"].source == DataPointSource.EMAIL_PARSE
    assert by_field["guest_phone"].source == DataPointSource.MANUAL_ENTRY
    assert by_field["guest_phone"].notes == "entered by primary owner"
