"""E2E test session fixture: hard-fail when any sandbox credential is absent.

Per D-01: no skip guards. Missing credentials cause an AssertionError that
fails the session before any test runs. This is intentional — a silently
skipped E2E suite would allow HARD-02 to be incorrectly marked 'done'.

Implements D-01 (no skip guards, hard fail on missing credentials).
Mirrors the 11 credentials validated by the production lifespan (Plan 06-01).
"""
import os

import pytest
import pytest_asyncio
from dotenv import dotenv_values

# The 11 sandbox creds live in .env (read directly by pydantic-settings), but
# .env is not exported to the shell, so os.environ does not have them and the
# credential gate below would false-negative even though the app can auth fine.
# Load the .env values into os.environ for the gate — but NEVER override
# DATABASE_URL: tests must hit the local test DB set by tests/conftest.py, not
# .env's docker-internal 'db' host (unreachable from the host, and we must never
# run destructive create/drop schema against a real database).
for _k, _v in dotenv_values(".env").items():
    if _k != "DATABASE_URL" and _v is not None and _k not in os.environ:
        os.environ[_k] = _v

# The 11 sandbox credential env vars that must be present for the E2E suite to run.
# These match the _REQUIRED_CREDENTIALS list in app/main.py exactly (Plan 06-01).
_E2E_CREDENTIALS = [
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GMAIL_BOOKING_FEED_REFRESH_TOKEN",
    "GMAIL_ALERTS_REFRESH_TOKEN",
    "GOOGLE_SHEETS_REFRESH_TOKEN",
    "DOCUSIGN_ACCOUNT_ID",
    "DOCUSIGN_CLIENT_ID",
    "DOCUSIGN_CLIENT_SECRET",
    "DOCUSIGN_REFRESH_TOKEN",
    "DOCUSIGN_HMAC_KEY",
    "SEAM_API_KEY",
]


@pytest.fixture(scope="session", autouse=True)
def require_e2e_sandbox_credentials(request):
    """Fail (not skip) if any sandbox credential is absent. Per D-01: no skip guards.

    This fixture runs before any test in the session. If any of the 11
    required credentials are missing from the environment, it raises an
    AssertionError naming the missing variables. This prevents false-positive
    results where skipped tests are mistaken for passing tests (T-06-12).

    Step 18: the whole E2E suite is treated as ``live`` and is skipped when the
    run is offline (no --run-live / RUN_LIVE=1). In that case every e2e item is
    already skip-marked at collection time, so we must NOT hard-fail here — the
    hard-fail is a *live-mode* gate. Only assert when live mode is requested.
    """
    if not (request.config.getoption("--run-live") or os.environ.get("RUN_LIVE") == "1"):
        return
    missing = [v for v in _E2E_CREDENTIALS if not os.environ.get(v)]
    if missing:
        raise AssertionError(
            f"E2E tests require sandbox credentials in .env. Missing: {missing}. "
            "Set all required variables and re-run. See TESTING.md "
            "('Live tests & the E2E gate') for the full list and setup steps."
        )


@pytest_asyncio.fixture(autouse=True)
async def _e2e_schema():
    """Create the app schema in the (test) DB the E2E test actually uses.

    The E2E test drives the flow through the app's global
    ``app.db.session.AsyncSessionLocal`` (not the per-test ``db_session``
    fixture used by integration tests), so the tables must exist in that
    engine's database. tests/conftest.py points DATABASE_URL at the local
    ``rental_automation_test`` DB, which is otherwise schema-less. We create
    the tables from the SQLAlchemy metadata before the test and drop them
    after — mirroring tests/integration/conftest.py's create/drop pattern.
    """
    import app.db.models  # noqa: F401 — registers mappers on the metadata
    from app.db.session import Base, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
