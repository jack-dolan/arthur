"""Unit tests for app/main.py lifespan scheduler registration.

Strategy: patch AsyncIOScheduler in app.main before entering the lifespan context
manager, then inspect the mock's add_job call_args_list. This verifies registration
configuration without actually starting the scheduler or hitting any external services.

Plan 04 (05-04) — TDD RED gate.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.ingestion.poller import poll_booking_feed
from app.main import lifespan
from app.tasks.scheduled import check_daily_reminders, check_hoa_window

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_settings(**overrides):
    """Return a MagicMock that looks like a valid settings object."""
    mock = MagicMock()
    mock.dashboard_password = "secure-password"
    mock.secret_key = "secure-secret-key"
    mock.database_url = "postgresql+asyncpg://user:realpass@localhost/db"
    mock.log_level = "INFO"
    for k, v in overrides.items():
        setattr(mock, k, v)
    return mock


async def _run_lifespan(mock_scheduler_cls):
    """Enter and immediately exit the lifespan context manager."""
    mock_app = MagicMock()
    async with lifespan(mock_app):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lifespan_registers_check_daily_reminders_cron():
    """check_daily_reminders must be registered as a cron job at hour=8 US/Eastern."""
    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    mock_settings = _make_mock_settings()

    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    calls = mock_scheduler.add_job.call_args_list
    # Find the call for check_daily_reminders
    daily_call = None
    for c in calls:
        args = c.args if c.args else ()
        if args and args[0] is check_daily_reminders:
            daily_call = c
            break

    assert daily_call is not None, (
        "scheduler.add_job was never called with check_daily_reminders. "
        f"Actual calls: {calls}"
    )
    args = daily_call.args
    kwargs = daily_call.kwargs

    # Positional: (check_daily_reminders, "cron")
    assert args[0] is check_daily_reminders
    assert args[1] == "cron"

    # Required kwargs
    assert kwargs.get("hour") == 8, f"hour should be 8, got {kwargs.get('hour')}"
    assert kwargs.get("timezone") == "US/Eastern", (
        f"timezone should be 'US/Eastern', got {kwargs.get('timezone')}"
    )
    assert kwargs.get("id") == "check_daily_reminders", (
        f"id should be 'check_daily_reminders', got {kwargs.get('id')}"
    )
    assert kwargs.get("misfire_grace_time") is None, (
        f"misfire_grace_time should be None, got {kwargs.get('misfire_grace_time')}"
    )
    assert kwargs.get("coalesce") is True, (
        f"coalesce should be True, got {kwargs.get('coalesce')}"
    )


@pytest.mark.asyncio
async def test_lifespan_registers_check_hoa_window_interval():
    """check_hoa_window must be registered as an interval job with hours=1."""
    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    mock_settings = _make_mock_settings()

    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    calls = mock_scheduler.add_job.call_args_list
    hoa_call = None
    for c in calls:
        args = c.args if c.args else ()
        if args and args[0] is check_hoa_window:
            hoa_call = c
            break

    assert hoa_call is not None, (
        "scheduler.add_job was never called with check_hoa_window. "
        f"Actual calls: {calls}"
    )
    args = hoa_call.args
    kwargs = hoa_call.kwargs

    # Positional: (check_hoa_window, "interval")
    assert args[0] is check_hoa_window
    assert args[1] == "interval"

    # Required kwargs
    assert kwargs.get("hours") == 1, f"hours should be 1, got {kwargs.get('hours')}"
    assert kwargs.get("id") == "check_hoa_window", (
        f"id should be 'check_hoa_window', got {kwargs.get('id')}"
    )


@pytest.mark.asyncio
async def test_lifespan_still_registers_existing_poll_booking_feed():
    """Regression guard: poll_booking_feed must still be registered after Phase 5 wiring."""
    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    mock_settings = _make_mock_settings()

    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    calls = mock_scheduler.add_job.call_args_list
    poll_call = None
    for c in calls:
        args = c.args if c.args else ()
        if args and args[0] is poll_booking_feed:
            poll_call = c
            break

    assert poll_call is not None, (
        "scheduler.add_job was never called with poll_booking_feed (regression). "
        f"Actual calls: {calls}"
    )
    kwargs = poll_call.kwargs
    assert kwargs.get("id") == "poll_booking_feed"


@pytest.mark.asyncio
async def test_lifespan_starts_scheduler():
    """scheduler.start() must be called exactly once."""
    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    mock_settings = _make_mock_settings()

    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    mock_scheduler.start.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_shutdown_called_on_exit():
    """scheduler.shutdown(wait=False) must be called after the lifespan exits."""
    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    mock_settings = _make_mock_settings()

    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    mock_scheduler.shutdown.assert_called_once_with(wait=False)


# ---------------------------------------------------------------------------
# RED tests: credential validation (11 fields + database_url + dead-field removal)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lifespan_raises_if_google_client_id_empty():
    mock_settings = _make_mock_settings(google_client_id="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="google_client_id"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_google_client_secret_empty():
    mock_settings = _make_mock_settings(google_client_secret="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="google_client_secret"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_gmail_booking_feed_refresh_token_empty():
    mock_settings = _make_mock_settings(gmail_booking_feed_refresh_token="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="gmail_booking_feed_refresh_token"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_gmail_alerts_refresh_token_empty():
    mock_settings = _make_mock_settings(gmail_alerts_refresh_token="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="gmail_alerts_refresh_token"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_google_sheets_refresh_token_empty():
    mock_settings = _make_mock_settings(google_sheets_refresh_token="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="google_sheets_refresh_token"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_docusign_account_id_empty():
    mock_settings = _make_mock_settings(docusign_account_id="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="docusign_account_id"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_docusign_client_id_empty():
    mock_settings = _make_mock_settings(docusign_client_id="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="docusign_client_id"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_docusign_client_secret_empty():
    mock_settings = _make_mock_settings(docusign_client_secret="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="docusign_client_secret"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_docusign_refresh_token_empty():
    mock_settings = _make_mock_settings(docusign_refresh_token="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="docusign_refresh_token"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_docusign_hmac_key_empty():
    mock_settings = _make_mock_settings(docusign_hmac_key="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="docusign_hmac_key"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_seam_api_key_empty():
    mock_settings = _make_mock_settings(seam_api_key="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="seam_api_key"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_google_oauth_client_id_empty():
    mock_settings = _make_mock_settings(google_oauth_client_id="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="google_oauth_client_id"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_google_oauth_client_secret_empty():
    mock_settings = _make_mock_settings(google_oauth_client_secret="")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="google_oauth_client_secret"),
    ):
        async with lifespan(MagicMock()):
            pass


@pytest.mark.asyncio
async def test_lifespan_raises_if_database_url_contains_change_me():
    mock_settings = _make_mock_settings(database_url="postgresql+asyncpg://user:change_me@localhost/db")
    mock_scheduler = MagicMock()
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
        pytest.raises(ValueError, match="change_me"),
    ):
        async with lifespan(MagicMock()):
            pass


@contextmanager
def _isolated_app_logger():
    """Run with a clean "app" logger, restoring global logging state afterwards."""
    import logging as _logging

    app_logger = _logging.getLogger("app")
    saved_handlers = app_logger.handlers[:]
    saved_level = app_logger.level
    saved_propagate = app_logger.propagate
    app_logger.handlers = []
    try:
        yield app_logger
    finally:
        app_logger.handlers = saved_handlers
        app_logger.level = saved_level
        app_logger.propagate = saved_propagate


def test_configure_logging_emits_app_info_records():
    """App-module INFO logs must actually reach stdout.

    Regression (found at Step 22): nothing in the app configured logging, so the
    root logger had no handler and Python's `logging.lastResort` fallback emitted
    WARNING+ only. Every log.info() in the codebase was silently dropped — in 9
    days of production the poller's ingest lines, the dispatcher's task lines and
    the webhook's "DocuSign webhook received: envelope_id=... status=..." line
    never appeared once. That webhook line is the primary evidence used to confirm
    a real Connect delivery parsed, so it must be visible.
    """
    import io
    import logging as _logging
    import sys as _sys

    from app.main import _configure_logging

    buf = io.StringIO()
    with _isolated_app_logger():
        with patch.object(_sys, "stdout", buf):
            _configure_logging()
        _logging.getLogger("app.some.module").info("hello-info-record")

    assert "hello-info-record" in buf.getvalue()


def test_configure_logging_does_not_double_print_warnings():
    """WARNING+ must appear once, not twice (handler + root/lastResort)."""
    import io
    import logging as _logging
    import sys as _sys

    from app.main import _configure_logging

    buf = io.StringIO()
    with _isolated_app_logger():
        with patch.object(_sys, "stdout", buf):
            _configure_logging()
        _logging.getLogger("app.some.module").warning("single-warning")

    assert buf.getvalue().count("single-warning") == 1


def test_resolve_log_level_falls_back_to_info_on_garbage():
    """A malformed LOG_LEVEL must degrade to INFO, never crash the boot."""
    import logging as _logging

    from app.main import _resolve_log_level

    assert _resolve_log_level("DEBUG") == _logging.DEBUG
    assert _resolve_log_level("warning") == _logging.WARNING
    assert _resolve_log_level(" Info ") == _logging.INFO
    assert _resolve_log_level("NOT_A_LEVEL") == _logging.INFO
    assert _resolve_log_level(None) == _logging.INFO
    assert _resolve_log_level(object()) == _logging.INFO


def test_configure_logging_is_idempotent():
    """Re-running configuration must not duplicate handlers (one line, not two).

    The lifespan runs per process, but tests and reloads can call this repeatedly;
    a duplicated handler would double every operational log line.
    """
    import io
    import logging as _logging
    import sys as _sys

    from app.main import _LOG_HANDLER_NAME, _configure_logging

    buf = io.StringIO()
    with _isolated_app_logger() as app_logger:
        with patch.object(_sys, "stdout", buf):
            _configure_logging()
            _configure_logging()
        _logging.getLogger("app.some.module").info("only-once-please")

        named = [h for h in app_logger.handlers if getattr(h, "name", None) == _LOG_HANDLER_NAME]
        assert len(named) == 1

    assert buf.getvalue().count("only-once-please") == 1


@pytest.mark.asyncio
async def test_lifespan_logs_docusign_target():
    """Startup must announce which DocuSign environment/hosts it will call."""
    mock_scheduler = MagicMock()
    mock_log_target = MagicMock(return_value=("account.docusign.com", "https://www.docusign.net/restapi"))
    with (
        patch("app.main.AsyncIOScheduler", MagicMock(return_value=mock_scheduler)),
        patch("app.main.settings", _make_mock_settings()),
        patch("pathlib.Path.mkdir"),
        patch("app.integrations.docusign.client.log_docusign_target", mock_log_target),
    ):
        async with lifespan(MagicMock()):
            pass

    mock_log_target.assert_called_once()


def test_settings_has_no_dead_scraping_fields():
    """Settings class must not contain dead scraping credential fields (D-11)."""
    from app.settings import Settings
    dead_fields = [
        "airbnb_username", "airbnb_password",
        "vrbo_username", "vrbo_password",
        "anthropic_api_key",
    ]
    defined = set(Settings.model_fields.keys())
    for field in dead_fields:
        assert field not in defined, (
            f"Dead field '{field}' still present in Settings — remove it per D-11."
        )


@pytest.mark.asyncio
async def test_lifespan_registers_requeue_stalled_automations_cron():
    """F17 (bug hunt 2026-07-22): the daily requeue/redispatch job must be
    registered — without it FAILED tasks stay FAILED forever and a PENDING
    task whose dispatch was lost (restart between commit and create_task)
    never runs."""
    from app.tasks.scheduled import requeue_stalled_automations

    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    mock_settings = _make_mock_settings()

    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    requeue_call = None
    for c in mock_scheduler.add_job.call_args_list:
        if c.args and c.args[0] is requeue_stalled_automations:
            requeue_call = c
            break

    assert requeue_call is not None, (
        "scheduler.add_job was never called with requeue_stalled_automations. "
        f"Actual calls: {mock_scheduler.add_job.call_args_list}"
    )
    assert requeue_call.args[1] == "cron"
    assert requeue_call.kwargs.get("hour") == 8
    assert requeue_call.kwargs.get("minute") == 30
    assert requeue_call.kwargs.get("timezone") == "US/Eastern"
    assert requeue_call.kwargs.get("id") == "requeue_stalled_automations"
    assert requeue_call.kwargs.get("coalesce") is True


@pytest.mark.asyncio
async def test_lifespan_registers_verify_access_codes_cron():
    """F10 (bug hunt 2026-07-22): Seam provisions codes asynchronously — the
    create call succeeding does not mean the code reached the lock. The daily
    verification job must be registered."""
    from app.tasks.scheduled import verify_access_codes

    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    mock_settings = _make_mock_settings()

    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    verify_call = None
    for c in mock_scheduler.add_job.call_args_list:
        if c.args and c.args[0] is verify_access_codes:
            verify_call = c
            break

    assert verify_call is not None, (
        "scheduler.add_job was never called with verify_access_codes. "
        f"Actual calls: {mock_scheduler.add_job.call_args_list}"
    )
    assert verify_call.args[1] == "cron"
    assert verify_call.kwargs.get("hour") == 9
    assert verify_call.kwargs.get("timezone") == "US/Eastern"
    assert verify_call.kwargs.get("id") == "verify_access_codes"


async def test_lifespan_registers_complete_past_bookings_cron():
    """F15 (bug hunt 2026-07-22): without a terminal booking state the
    dashboard's active list and every daily scan grow forever. The daily
    auto-complete job must be registered."""
    from app.tasks.scheduled import complete_past_bookings

    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    mock_settings = _make_mock_settings()

    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", mock_settings),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    complete_call = None
    for c in mock_scheduler.add_job.call_args_list:
        if c.args and c.args[0] is complete_past_bookings:
            complete_call = c
            break

    assert complete_call is not None, (
        "scheduler.add_job was never called with complete_past_bookings. "
        f"Actual calls: {mock_scheduler.add_job.call_args_list}"
    )
    assert complete_call.args[1] == "cron"
    assert complete_call.kwargs.get("hour") == 2
    assert complete_call.kwargs.get("timezone") == "US/Eastern"
    assert complete_call.kwargs.get("id") == "complete_past_bookings"


# ---------------------------------------------------------------------------
# Sustainability audit 2026-07-23 — monitoring jobs & keep-alive trigger fix
# ---------------------------------------------------------------------------

def _job_call(mock_scheduler, func):
    for c in mock_scheduler.add_job.call_args_list:
        if c.args and c.args[0] is func:
            return c
    return None


@pytest.mark.asyncio
async def test_lifespan_registers_refresh_docusign_token_as_cron_not_interval():
    """The keep-alive must be a cron job: APScheduler interval jobs restart
    their countdown on every container restart, so frequent deploys could
    starve the keep-alive indefinitely (sustainability audit item 2)."""
    from app.tasks.scheduled import refresh_docusign_token

    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", _make_mock_settings()),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    c = _job_call(mock_scheduler, refresh_docusign_token)
    assert c is not None, "refresh_docusign_token not registered"
    assert c.args[1] == "cron", f"expected cron trigger, got {c.args[1]}"
    assert c.kwargs.get("day_of_week") == "mon"
    assert c.kwargs.get("timezone") == "US/Eastern"
    assert c.kwargs.get("id") == "refresh_docusign_token"
    assert c.kwargs.get("coalesce") is True
    assert c.kwargs.get("misfire_grace_time") is None


@pytest.mark.asyncio
async def test_lifespan_registers_verify_credentials_daily_cron():
    from app.tasks.scheduled import verify_credentials

    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", _make_mock_settings()),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    c = _job_call(mock_scheduler, verify_credentials)
    assert c is not None, "verify_credentials not registered"
    assert c.args[1] == "cron"
    assert c.kwargs.get("hour") == 7
    assert c.kwargs.get("timezone") == "US/Eastern"
    assert c.kwargs.get("id") == "verify_credentials"
    assert c.kwargs.get("coalesce") is True
    assert c.kwargs.get("misfire_grace_time") is None


@pytest.mark.asyncio
async def test_lifespan_registers_check_classifier_drift_weekly_cron():
    from app.tasks.scheduled import check_classifier_drift

    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", _make_mock_settings()),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    c = _job_call(mock_scheduler, check_classifier_drift)
    assert c is not None, "check_classifier_drift not registered"
    assert c.args[1] == "cron"
    assert c.kwargs.get("day_of_week") == "sun"
    assert c.kwargs.get("hour") == 9
    assert c.kwargs.get("timezone") == "US/Eastern"
    assert c.kwargs.get("id") == "check_classifier_drift"
    assert c.kwargs.get("coalesce") is True
    assert c.kwargs.get("misfire_grace_time") is None


@pytest.mark.asyncio
async def test_lifespan_registers_monthly_status_report_cron():
    from app.tasks.scheduled import send_monthly_status_email

    mock_scheduler = MagicMock()
    mock_scheduler_cls = MagicMock(return_value=mock_scheduler)
    with (
        patch("app.main.AsyncIOScheduler", mock_scheduler_cls),
        patch("app.main.settings", _make_mock_settings()),
        patch("pathlib.Path.mkdir"),
    ):
        await _run_lifespan(mock_scheduler_cls)

    c = _job_call(mock_scheduler, send_monthly_status_email)
    assert c is not None, "send_monthly_status_email not registered"
    assert c.args[1] == "cron"
    assert c.kwargs.get("day") == 1
    assert c.kwargs.get("hour") == 8
    assert c.kwargs.get("timezone") == "US/Eastern"
    assert c.kwargs.get("id") == "send_monthly_status_email"
    assert c.kwargs.get("coalesce") is True
    assert c.kwargs.get("misfire_grace_time") is None
