"""Integration tests for the dashboard routes.

Auth is Google-OIDC session based (app/routers/auth.py). These tests bypass the
Google flow by overriding the ``require_user`` dependency with a stub signed-in
user; the OIDC flow itself is unit-tested in tests/unit/test_auth.py. Routes
with no auth override must redirect (303) to /login.

asyncio_mode = "auto" is set in pyproject.toml — no @pytest.mark.asyncio needed.
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient

from app.config import load_config
from app.db.models import Booking, BookingStatus, Platform
from app.db.session import get_db
from app.main import app
from app.routers.auth import require_user

FIXTURES = Path(__file__).parent.parent / "fixtures"

# The stub user must be an allowlisted address; take it from the fixture config
# rather than hardcoding one.
STUB_USER = load_config(FIXTURES / "config.test.yaml").dashboard.allowed_emails[0]


def _auth_override():
    """Install a stub signed-in, allowlisted user for require_user."""
    app.dependency_overrides[require_user] = lambda: STUB_USER


def _make_booking(
    *,
    guest_phone: str | None = None,
    guest_email: str | None = None,
) -> Booking:
    """Create an in-memory Booking for integration tests — no DB required."""
    booking = Booking(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id="HMTEST001",
        property_id="property_1",
        guest_first_name="Test",
        guest_last_name="Guest",
        guest_phone=guest_phone,
        guest_email=guest_email,
        check_in_date=date(2026, 8, 1),
        check_out_date=date(2026, 8, 5),
        status=BookingStatus.ACTIVE,
        source_email_message_id="msg-1",
    )
    return booking


def _make_mock_session_for_list() -> AsyncMock:
    """Create a mock DB session for booking list route (two execute() calls)."""
    mock_session = AsyncMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = []
    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_result
    mock_session.execute = AsyncMock(return_value=execute_result)
    return mock_session


async def _override_get_db_factory(mock_session):
    """Return a FastAPI dependency override that yields mock_session."""
    async def _override():
        yield mock_session
    return _override


# ---------------------------------------------------------------------------
# Unauthenticated → redirect to /login (303)
# ---------------------------------------------------------------------------

async def test_list_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_detail_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/bookings/{uuid.uuid4()}")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_contact_save_requires_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/bookings/{uuid.uuid4()}/contact",
            data={"guest_phone": "5551234567"},
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Authenticated (require_user overridden)
# ---------------------------------------------------------------------------

async def test_list_returns_html_with_auth():
    mock_session = _make_mock_session_for_list()
    override = await _override_get_db_factory(mock_session)
    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


async def test_list_shows_recently_completed_bookings():
    """F15: a COMPLETED booking must still be visible somewhere on the
    dashboard (it's in neither the active nor the cancelled query) — mirrors
    the existing 'Recent Cancellations' section."""
    booking = _make_booking(guest_phone="5551234567", guest_email="g@example.com")
    booking.status = BookingStatus.COMPLETED
    booking.check_out_date = date(2026, 7, 20)

    active_result = MagicMock()
    active_result.scalars.return_value.all.return_value = []
    cancelled_result = MagicMock()
    cancelled_result.scalars.return_value.all.return_value = []
    completed_result = MagicMock()
    completed_result.scalars.return_value.all.return_value = [booking]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        side_effect=[active_result, cancelled_result, completed_result]
    )
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Recently Completed" in response.text
    assert booking.guest_first_name in response.text


async def test_detail_not_found():
    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=execute_result)
    override = await _override_get_db_factory(mock_session)
    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/bookings/{uuid.uuid4()}")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 404
    assert response.json().get("detail") == "Booking not found"


async def test_contact_save_redirects_303():
    from unittest.mock import patch

    booking = _make_booking(guest_email=None, guest_phone=None)

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        with patch("app.routers.dashboard._dispatch_pending_tasks", new_callable=AsyncMock):
            with patch("app.routers.dashboard.asyncio.create_task"):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    response = await client.post(
                        f"/bookings/{booking.id}/contact",
                        data={"guest_phone": "5551234567"},
                    )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 303
    assert response.headers["location"] == f"/bookings/{booking.id}"


async def test_contact_save_invalid_phone_rerenders_200():
    booking = _make_booking(guest_email=None, guest_phone=None)

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.post(
                f"/bookings/{booking.id}/contact",
                data={"guest_phone": "abc"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Enter a valid phone number" in response.text
    assert "abc" in response.text


def _full_task_booking(**kwargs) -> Booking:
    """Booking with a realistic full task set for detail-page rendering."""
    from app.db.models import BookingTask, TaskState, TaskType

    booking = _make_booking(**kwargs)
    states = {
        TaskType.OWNER_ALERT_NEW_BOOKING: TaskState.COMPLETE,
        TaskType.CLEANER_SHEET_ADD: TaskState.COMPLETE,
        TaskType.DOCUSIGN_SEND: TaskState.COMPLETE,
        TaskType.ACCESS_CODE_CREATE: TaskState.COMPLETE,
        TaskType.HOA_EMAIL: TaskState.WAITING,
        TaskType.OWNER_ALERT_MISSING_PHONE_7D: TaskState.SKIPPED,
        TaskType.OWNER_ALERT_MISSING_PHONE_4D: TaskState.SKIPPED,
        TaskType.OWNER_ALERT_MISSING_EMAIL_7D: TaskState.SKIPPED,
        TaskType.OWNER_ALERT_MISSING_EMAIL_4D: TaskState.SKIPPED,
        TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D: TaskState.PENDING,
        TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D: TaskState.PENDING,
    }
    booking.tasks = [
        BookingTask(id=uuid.uuid4(), booking_id=booking.id, task_type=tt, state=s)
        for tt, s in states.items()
    ]
    return booking


def _detail_pipeline_config() -> MagicMock:
    prop = MagicMock()
    prop.id = "property_1"
    prop.hoa.open_days = [1, 2, 3, 4, 5, 6, 7]
    prop.hoa.email_window_days_min = 2
    prop.hoa.email_window_days_max = 7
    cfg = MagicMock()
    cfg.properties = [prop]
    return cfg


async def test_detail_renders_hoa_pipeline_and_labels():
    """The detail page shows the HOA pipeline panel, human task labels, and
    owner-facing reminder state labels."""
    from datetime import timedelta
    from unittest.mock import patch

    booking = _full_task_booking(
        guest_phone="5551234567", guest_email="g@example.com"
    )
    booking.check_in_date = date.today() + timedelta(days=30)
    booking.check_out_date = booking.check_in_date + timedelta(days=3)

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        with patch(
            "app.routers.dashboard.load_config",
            return_value=_detail_pipeline_config(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(f"/bookings/{booking.id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    html = response.text
    # HOA pipeline panel with the computed window dates — asserted against
    # hoa_window itself (the math is pinned in tests/unit/test_hoa_window.py).
    from app.integrations.hoa.window import hoa_window

    # open_days must match _detail_pipeline_config's stub, or this assertion
    # becomes weekday-dependent (see tests/unit/test_dashboard.py).
    earliest, latest = hoa_window(booking.check_in_date, [1, 2, 3, 4, 5, 6, 7], 2, 7)
    assert "HOA Registration" in html
    assert str(earliest) in html
    assert str(latest) in html
    assert "window opens" in html
    # Human task labels (not raw enum names)
    assert "New-booking alert" in html
    assert "owner alert new booking" not in html
    # Reminder state labels: SKIPPED renders as "not needed",
    # PENDING renders as "scheduled"
    assert "not needed" in html
    assert "scheduled" in html
    # Derived status badge in the header (form sent, unsigned)
    assert "waiting on guest" in html


async def test_detail_renders_without_pipeline_when_config_missing():
    """Config unavailable → the panel is omitted, page still renders."""
    from unittest.mock import patch

    booking = _full_task_booking(
        guest_phone="5551234567", guest_email="g@example.com"
    )

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        with patch(
            "app.routers.dashboard.load_config", side_effect=OSError("no config")
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(f"/bookings/{booking.id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "HOA Registration" not in response.text


async def test_detail_shows_actual_seam_code_on_complete_access_task():
    """The access-code row renders "complete: <code>" using the value recorded
    from the Seam API response — never derived from the phone number."""
    from datetime import datetime
    from datetime import timezone as tz
    from unittest.mock import patch

    from app.db.models import DataPoint, DataPointSource

    booking = _full_task_booking(
        guest_phone="5551234999", guest_email="g@example.com"  # last 4 = 4999
    )
    booking.data_points = [
        DataPoint(
            booking_id=booking.id,
            field_name="access_code",
            value="7777",  # deliberately NOT the phone's last 4
            source=DataPointSource.SEAM_API,
            recorded_at=datetime(2026, 7, 20, tzinfo=tz.utc),
        )
    ]

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        with patch(
            "app.routers.dashboard.load_config",
            return_value=_detail_pipeline_config(),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(f"/bookings/{booking.id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "complete: 7777" in response.text
    assert "complete: 4999" not in response.text


# ---------------------------------------------------------------------------
# F5 (bug hunt 2026-07-22): contact entry is refused on a cancelled booking
# ---------------------------------------------------------------------------


async def test_contact_save_rejected_on_cancelled_booking():
    from unittest.mock import patch

    from app.db.models import BookingStatus

    booking = _make_booking(guest_email=None, guest_phone=None)
    booking.status = BookingStatus.CANCELLED

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        with patch(
            "app.routers.dashboard._dispatch_pending_tasks", new_callable=AsyncMock
        ) as mock_dispatch:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"/bookings/{booking.id}/contact",
                    data={"guest_phone": "5551234567"},
                )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert booking.guest_phone is None  # nothing saved
    mock_session.commit.assert_not_awaited()
    mock_dispatch.assert_not_called()


async def test_detail_page_hides_contact_form_when_cancelled():
    from app.db.models import BookingStatus

    booking = _make_booking(guest_email=None, guest_phone=None)
    booking.status = BookingStatus.CANCELLED

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"/bookings/{booking.id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    html = response.text
    assert f"/bookings/{booking.id}/contact" not in html  # no form action
    assert "cancelled" in html.lower()  # the page says why


# ---------------------------------------------------------------------------
# F13 (bug hunt 2026-07-22): contact fields are editable, not write-once
# ---------------------------------------------------------------------------


async def test_detail_page_shows_editable_fields_prefilled_when_already_set():
    """Once phone/email are set, the form still renders (editable, not gone)
    and the inputs are pre-filled with the current values."""
    booking = _make_booking(guest_phone="5551234567", guest_email="g@example.com")

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(f"/bookings/{booking.id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    html = response.text
    assert f'action="/bookings/{booking.id}/contact"' in html
    assert 'value="5551234567"' in html
    assert 'value="g@example.com"' in html
    assert 'name="confirm_overwrite"' in html


async def test_contact_save_overwrite_without_confirm_rerenders_200():
    booking = _make_booking(guest_phone="5551234567", guest_email=None)

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.post(
                f"/bookings/{booking.id}/contact",
                data={"guest_phone": "5559876543"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "confirm" in response.text.lower()
    assert booking.guest_phone == "5551234567"  # unchanged


async def test_contact_save_overwrite_with_confirm_redirects_303():
    from unittest.mock import patch

    booking = _make_booking(guest_phone="5551234567", guest_email=None)

    mock_session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = booking
    mock_session.execute = AsyncMock(return_value=execute_result)
    mock_session.refresh = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()
    override = await _override_get_db_factory(mock_session)

    try:
        _auth_override()
        app.dependency_overrides[get_db] = override
        with patch("app.routers.dashboard._dispatch_pending_tasks", new_callable=AsyncMock):
            with patch("app.routers.dashboard.asyncio.create_task"):
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://test",
                    follow_redirects=False,
                ) as client:
                    response = await client.post(
                        f"/bookings/{booking.id}/contact",
                        data={"guest_phone": "5559876543", "confirm_overwrite": "1"},
                    )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 303
    assert booking.guest_phone == "5559876543"
