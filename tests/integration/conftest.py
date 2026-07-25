"""Database fixtures for integration tests.

Each test gets a clean schema: tables (and the enum types they depend on) are
created fresh from the SQLAlchemy metadata and dropped at the end of the test.
Tests that don't need the DB simply don't depend on the fixture.
"""
from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.conftest import TEST_DATABASE_URL

# NOTE (2026-07-22): the fail-loud live-API guard that used to live here
# (`_block_live_gmail`, added after the Step 14 leak) has been SUPERSEDED by
# `_block_live_external_apis` in the top-level tests/conftest.py. Scoping it to
# integration tests only left tests/unit/ unguarded, and a unit-test RED run
# leaked a real new-booking alert email on 2026-07-22. The top-level guard
# covers every test scope and all four integrations (Gmail, Sheets, Seam,
# DocuSign), with the same live-marker + --run-live escape hatch.


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    from app.db.models import Base  # noqa: F401  (registers mappers)
    from app.db.session import Base as SessionBase

    engine = create_async_engine(TEST_DATABASE_URL, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(SessionBase.metadata.drop_all)
        await conn.run_sync(SessionBase.metadata.create_all)

    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(SessionBase.metadata.drop_all)
    await engine.dispose()
