from __future__ import annotations

import uuid
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text


async def test_future_enum_values_load_and_render_as_unknown(db_session):
    """An older build must visibly tolerate rows written by the next release."""
    from app.db.models import Booking, UnknownEnumValue
    from app.db.session import get_db
    from app.main import app
    from app.routers.auth import require_user

    future_values = {
        "platform": "future_platform",
        "booking_status": "future_booking_status",
        "task_type": "future_task_type",
        "task_state": "future_task_state",
        "data_point_source": "future_data_source",
    }
    for enum_name, value in future_values.items():
        await db_session.execute(
            text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'")
        )
    await db_session.commit()

    booking_id = uuid.uuid4()
    task_id = uuid.uuid4()
    data_point_id = uuid.uuid4()
    await db_session.execute(
        text(
            """
            INSERT INTO bookings (
                id, platform, external_id, property_id,
                guest_first_name, guest_last_name,
                check_in_date, check_out_date, status,
                source_email_message_id
            ) VALUES (
                :id, :platform, 'HMFUTURE01', 'property_1',
                'Future', 'Example',
                DATE '2026-09-01', DATE '2026-09-05', :status,
                'message-future-enum'
            )
            """
        ),
        {
            "id": booking_id,
            "platform": future_values["platform"],
            "status": future_values["booking_status"],
        },
    )
    await db_session.execute(
        text(
            """
            INSERT INTO booking_tasks (id, booking_id, task_type, state)
            VALUES (:id, :booking_id, :task_type, :state)
            """
        ),
        {
            "id": task_id,
            "booking_id": booking_id,
            "task_type": future_values["task_type"],
            "state": future_values["task_state"],
        },
    )
    await db_session.execute(
        text(
            """
            INSERT INTO data_points (
                id, booking_id, field_name, value, source
            ) VALUES (
                :id, :booking_id, 'guest_first_name', 'Future', :source
            )
            """
        ),
        {
            "id": data_point_id,
            "booking_id": booking_id,
            "source": future_values["data_point_source"],
        },
    )
    await db_session.commit()
    db_session.expire_all()

    booking = (
        await db_session.execute(select(Booking).where(Booking.id == booking_id))
    ).scalar_one()
    await db_session.refresh(booking, attribute_names=["tasks", "data_points"])

    assert isinstance(booking.platform, UnknownEnumValue)
    assert isinstance(booking.status, UnknownEnumValue)
    assert isinstance(booking.tasks[0].task_type, UnknownEnumValue)
    assert isinstance(booking.tasks[0].state, UnknownEnumValue)
    assert isinstance(booking.data_points[0].source, UnknownEnumValue)

    async def override_db():
        yield db_session

    app.dependency_overrides[require_user] = lambda: "owner@example.com"
    app.dependency_overrides[get_db] = override_db
    try:
        with patch(
            "app.routers.dashboard.load_config",
            side_effect=OSError("test config deliberately unavailable"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                list_response = await client.get("/")
                detail_response = await client.get(f"/bookings/{booking_id}")
    finally:
        app.dependency_overrides.clear()

    assert list_response.status_code == 200
    assert "Future Example" in list_response.text
    assert "unknown (future booking status)" in list_response.text.lower()

    assert detail_response.status_code == 200
    for value in future_values.values():
        assert value.replace("_", " ") in detail_response.text.lower()
    assert detail_response.text.lower().count("unknown") >= len(future_values)
