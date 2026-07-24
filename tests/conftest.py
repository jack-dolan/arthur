import email as email_lib
import os
from email import policy as email_policy
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Recorded platform emails (gitignored)
# ---------------------------------------------------------------------------

RECORDED_EMAILS = Path(__file__).parent / "fixtures" / "emails"


def recorded_email_bytes(filename: str) -> bytes:
    """Read a recorded Airbnb/VRBO email, or SKIP if it isn't on this machine.

    ``tests/fixtures/emails/`` is **gitignored**: the files are genuine platform
    mail carrying real guest names, phone numbers and confirmation codes, so
    they can never be committed and are absent on a fresh clone.

    Tests that replay one therefore skip rather than fail. What they add over
    the suite's inline synthetic-message tests is *fidelity to real mail* — the
    exact property that cannot ship — and that value is real: the VRBO parser
    once passed against synthetic fixtures while dropping every real booking
    (Session Log, 2026-07-05). Every format lesson learned that way is pinned
    separately by an inline regression test built from a sanitized message, so
    a clone without these files still covers the behaviour, just not the
    provenance.
    """
    path = RECORDED_EMAILS / filename
    if not path.is_file():
        pytest.skip(
            f"recorded email fixture is gitignored and not present: "
            f"tests/fixtures/emails/{filename}"
        )
    return path.read_bytes()


def recorded_email_message(filename: str):
    """`recorded_email_bytes` parsed with the same policy the poller uses."""
    return email_lib.message_from_bytes(
        recorded_email_bytes(filename), policy=email_policy.default
    )


DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://rental_automation:devpassword@127.0.0.1:5432/rental_automation_test"
)

# The one knob. Point TEST_DATABASE_URL at any throwaway Postgres and the whole
# suite follows — see TESTING.md for the one-time setup.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)

# The fixtures create the schema on TEST_DATABASE_URL, but code under test opens
# sessions through app.db.session's global engine, which is built from
# settings.database_url. Those two MUST be the same database: if they diverge the
# fixtures create tables the app never sees and every DB-backed test fails with
# `relation "bookings" does not exist`. Defaulting one from the other makes that
# impossible to get wrong by setting only half of it. (`setdefault`, so an
# explicit DATABASE_URL in the environment still wins — and it also keeps
# app.settings from resolving the docker-internal hostname "db" from the host.)
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)


# ---------------------------------------------------------------------------
# Offline / live selection (Step 18)
# ---------------------------------------------------------------------------
#
# The suite is OFFLINE BY DEFAULT: every test that would hit a real external
# API (Google Sheets, Gmail, Seam, DocuSign) is marked ``@pytest.mark.live``
# and is *skipped* unless the run is explicitly opted into live mode. Offline,
# those same scenarios replay hand-written recorded responses (see
# ``tests/integration/fakes.py``) so no credentials are required.
#
# Live mode is enabled by EITHER:
#   * ``pytest --run-live``     (the flag), or
#   * ``RUN_LIVE=1`` in the env (what the Makefile's live targets export).
#
# Everything under ``tests/e2e/`` is treated as live regardless of marker: the
# full end-to-end gate always talks to the real sandbox and hard-fails if any
# of the 11 sandbox credentials are missing (see tests/e2e/conftest.py).


def _live_requested(config) -> bool:
    """True when the run has opted into live external calls (flag or env var)."""
    return bool(config.getoption("--run-live")) or os.environ.get("RUN_LIVE") == "1"


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help=(
            "Run tests marked @pytest.mark.live against the real sandbox APIs "
            "(requires sandbox credentials in .env). Equivalent to RUN_LIVE=1. "
            "Default: offline (live tests are skipped, recorded responses replay)."
        ),
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live: test hits a real external sandbox API; skipped unless --run-live "
        "(or RUN_LIVE=1). Offline runs replay recorded responses instead.",
    )


def pytest_collection_modifyitems(config, items):
    """Skip live-marked (and all e2e) tests unless live mode is requested.

    Anything under ``tests/e2e/`` is auto-tagged ``live`` so the offline default
    never triggers the E2E credential hard-fail.
    """
    live = _live_requested(config)
    skip_live = pytest.mark.skip(
        reason="live external-API test — run with --run-live (or RUN_LIVE=1)"
    )
    for item in items:
        is_e2e = "/tests/e2e/" in item.nodeid or item.nodeid.startswith("tests/e2e/")
        if is_e2e and "live" not in item.keywords:
            item.add_marker(pytest.mark.live)
        if ("live" in item.keywords or is_e2e) and not live:
            item.add_marker(skip_live)


@pytest.fixture
def run_live(request) -> bool:
    """True when the current run has opted into live external calls.

    Live tests use this to choose a real client; offline replay uses it to
    choose a recorded-response fake. See tests/integration/fakes.py.
    """
    return _live_requested(request.config)


# ---------------------------------------------------------------------------
# Fail-loud external-API guard — EVERY test scope (incident 2026-07-22)
# ---------------------------------------------------------------------------
#
# The test environment carries the real production .env, so ANY unmocked path
# to an external client sends real email / creates real lock codes / real
# envelopes / real sheet rows. This has now bitten twice:
#   - Step 14 (2026-07-02): an integration test leaked a real cancellation
#     alert → the _block_live_gmail guard was added, but only to
#     tests/integration/conftest.py.
#   - 2026-07-22: a unit-test RED run (tests/unit/test_poller.py) leaked a
#     real new-booking alert — unit tests were never covered by the guard.
#
# So the guard now lives HERE, covers every test in the suite, and blocks all
# four external integrations at their construction choke points:
#   Gmail / Sheets  — the googleapiclient `build` binding in each module
#   Seam            — the `Seam` SDK constructor binding
#   DocuSign        — the module's `httpx` binding (every DocuSign call starts
#                     with the httpx token exchange in _refresh_access_token,
#                     so no authenticated SDK client can ever be built)
#
# Escape hatch: a test explicitly marked @pytest.mark.live, running under
# --run-live / RUN_LIVE=1, is opting into the real sandbox on purpose. Offline,
# live-marked tests are skipped at collection so the guard never applies to
# them. Everything under tests/e2e/ is auto-tagged live (above), so the E2E
# gate keeps its live-by-design behavior.
#
# Tests that need a client mock their own boundary at the module that CALLS
# it; nested `patch(...)` inside a test overrides these guard bindings.


def _blocked_call(name: str):
    def _blocked(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError(
            f"Live {name} call blocked by the test guard (tests/conftest.py). "
            f"Mock the {name} boundary at the module that CALLS it."
        )

    return _blocked


@pytest.fixture(autouse=True)
def _block_live_external_apis(request):
    from unittest.mock import MagicMock, patch

    if _live_requested(request.config) and "live" in request.keywords:
        yield
        return

    blocked_httpx = MagicMock(name="guarded-docusign-httpx")
    blocked_httpx.post = MagicMock(
        side_effect=_blocked_call("DocuSign OAuth (httpx.post)")
    )

    with (
        patch(
            "app.integrations.gmail.oauth.build",
            side_effect=_blocked_call("Gmail API"),
        ),
        patch(
            "app.integrations.sheets.client.build",
            side_effect=_blocked_call("Google Sheets API"),
        ),
        patch(
            "app.integrations.seam.client.Seam",
            side_effect=_blocked_call("Seam SDK"),
        ),
        patch("app.integrations.docusign.client.httpx", blocked_httpx),
    ):
        yield
