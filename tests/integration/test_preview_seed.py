"""Preview-mode demo seeding against a real database.

The shape of the seeded rows is pinned by unit tests
(tests/unit/test_preview_mode.py). What needs a real database is the part that
cannot be faked: that the rows insert against the actual schema and enum types,
and that a restart does not duplicate them — a preview container boots as often
as the platform restarts it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Booking, BookingTask, DataPoint
from app.preview import seed_demo_bookings
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture
def preview_session_factory(db_session):
    """A session factory on the test database whose schema db_session built."""
    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.mark.asyncio
async def test_seeding_inserts_bookings_tasks_and_data_points(preview_session_factory):
    created = await seed_demo_bookings(session_factory=preview_session_factory)

    assert created > 0
    assert await _count(preview_session_factory, Booking) == created
    assert await _count(preview_session_factory, BookingTask) > 0
    assert await _count(preview_session_factory, DataPoint) > 0


@pytest.mark.asyncio
async def test_seeding_twice_creates_nothing_the_second_time(preview_session_factory):
    """Idempotent by (platform, external_id) — a restart must not duplicate."""
    first = await seed_demo_bookings(session_factory=preview_session_factory)
    bookings_after_first = await _count(preview_session_factory, Booking)

    second = await seed_demo_bookings(session_factory=preview_session_factory)

    assert second == 0
    assert await _count(preview_session_factory, Booking) == bookings_after_first == first


@pytest.mark.asyncio
async def test_seeded_bookings_read_back_through_the_orm(preview_session_factory):
    """Proves the enum values and relationships round-trip, not just insert."""
    await seed_demo_bookings(session_factory=preview_session_factory)

    async with preview_session_factory() as session:
        bookings = (await session.execute(select(Booking))).scalars().all()

    for booking in bookings:
        assert booking.platform.value in {"airbnb", "vrbo"}
        assert booking.status.value in {"active", "cancelled", "completed"}
