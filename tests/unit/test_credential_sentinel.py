"""Unit tests for the daily credential sentinel (sustainability audit item 3).

verify_credentials actively proves every integration credential still works
(read-only probes), then pings the external heartbeat ONLY when all checks
pass — so a dead credential shows up as heartbeat silence at the external
monitor, independent of the (possibly dead) alerts Gmail token.

All external boundaries are mocked at the app.tasks.scheduled module bindings
(the module imports them at top level — patching the source module would
silently do nothing). Side-effect seams (alert send, heartbeat ping) are
dead-ended explicitly so RED runs cannot leak.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _patch_all_boundaries(stack: ExitStack) -> dict:
    """Patch every sentinel boundary; return the mocks by name."""
    mocks = {}
    for name in (
        "get_booking_feed_service",
        "get_alerts_service",
        "get_sheets_service",
        "get_seam_client",
        "get_anthropic_client",
        "load_config",
    ):
        mocks[name] = stack.enter_context(
            patch(f"app.tasks.scheduled.{name}", MagicMock())
        )
    mocks["httpx"] = stack.enter_context(patch("app.tasks.scheduled.httpx", MagicMock()))
    # The web-client probe reads the JSON error field; a real "invalid_grant"
    # (bogus code, valid secret) means the client credentials are fine.
    mocks["httpx"].post.return_value.json.return_value = {"error": "invalid_grant"}
    mocks["send_alert"] = stack.enter_context(
        patch("app.tasks.scheduled.send_credential_sentinel_alert", MagicMock())
    )
    mocks["ping"] = stack.enter_context(
        patch("app.tasks.scheduled.ping_heartbeat_async", new_callable=AsyncMock)
    )
    return mocks


@pytest.mark.asyncio
async def test_all_checks_pass_pings_heartbeat_and_sends_no_alert():
    from app.tasks.scheduled import verify_credentials

    with ExitStack() as stack:
        mocks = _patch_all_boundaries(stack)
        await verify_credentials()

    assert mocks["ping"].await_count == 1
    assert mocks["ping"].await_args.kwargs.get("label") == "credential-sentinel"
    mocks["send_alert"].assert_not_called()


@pytest.mark.asyncio
async def test_failed_check_skips_heartbeat_and_sends_alert():
    from app.tasks.scheduled import verify_credentials

    with ExitStack() as stack:
        mocks = _patch_all_boundaries(stack)
        mocks["get_sheets_service"].side_effect = RuntimeError("invalid_grant")
        await verify_credentials()

    mocks["ping"].assert_not_awaited()
    mocks["send_alert"].assert_called_once()
    failures = mocks["send_alert"].call_args.kwargs["failures"]
    assert any(f["credential"] == "google_sheets" for f in failures)


@pytest.mark.asyncio
async def test_invalid_client_on_web_oauth_probe_is_a_failure():
    """Google answering invalid_client means the dashboard OAuth secret is
    dead; invalid_grant (bogus code, valid secret) means it is fine."""
    from app.tasks.scheduled import verify_credentials

    with ExitStack() as stack:
        mocks = _patch_all_boundaries(stack)
        mocks["httpx"].post.return_value.json.return_value = {"error": "invalid_client"}
        await verify_credentials()

    mocks["ping"].assert_not_awaited()
    failures = mocks["send_alert"].call_args.kwargs["failures"]
    assert any(f["credential"] == "dashboard_oauth_client" for f in failures)


@pytest.mark.asyncio
async def test_one_failure_does_not_stop_other_checks():
    from app.tasks.scheduled import verify_credentials

    with ExitStack() as stack:
        mocks = _patch_all_boundaries(stack)
        mocks["get_booking_feed_service"].side_effect = RuntimeError("boom")
        await verify_credentials()

    # The later checks still ran despite the first failing.
    assert mocks["get_seam_client"].called
    failures = mocks["send_alert"].call_args.kwargs["failures"]
    assert len(failures) == 1


def test_build_credential_sentinel_alert_content():
    from app.ingestion.alerts import build_credential_sentinel_alert

    failures = [
        {"credential": "gmail_alerts", "error": "invalid_grant"},
        {"credential": "seam", "error": "401 Unauthorized"},
    ]
    subject, body = build_credential_sentinel_alert(
        failures=failures, dashboard_base_url="https://arthur.example"
    )
    assert "2" in subject and "credential" in subject.lower()
    assert "gmail_alerts" in body and "invalid_grant" in body
    assert "seam" in body and "401 Unauthorized" in body
    assert "recovering-credentials" in body  # points the owner at the fix skill


# ---------------------------------------------------------------------------
# Transient-failure retry (2026-07-30)
# ---------------------------------------------------------------------------
#
# A dropped connection to an external API is not a dead credential. Before the
# retry existed, one blip turned into heartbeat silence plus an alert email
# naming a credential that was fine. The same defect produced a false
# "check the lock" alert from verify_access_codes on 2026-07-29.


@pytest.fixture
def no_backoff():
    """Zero the retry delays so tests don't actually sleep."""
    with patch("app.tasks.scheduled.TRANSIENT_RETRY_BACKOFF_SECONDS", (0.0, 0.0)):
        yield


@pytest.mark.asyncio
async def test_read_with_retry_returns_first_result_without_retrying(no_backoff):
    from app.tasks.scheduled import _read_with_retry

    call = MagicMock(return_value="ok")
    assert await _read_with_retry(call, label="probe") == "ok"
    assert call.call_count == 1


@pytest.mark.asyncio
async def test_read_with_retry_recovers_from_a_transient_failure(no_backoff):
    from app.tasks.scheduled import _read_with_retry

    call = MagicMock(
        side_effect=[
            ConnectionError("remote end closed connection without response"),
            "ok",
        ]
    )
    assert await _read_with_retry(call, label="probe") == "ok"
    assert call.call_count == 2


@pytest.mark.asyncio
async def test_read_with_retry_reraises_the_original_error_after_all_attempts(no_backoff):
    from app.tasks.scheduled import _read_with_retry

    boom = ConnectionError("remote end closed connection without response")
    call = MagicMock(side_effect=boom)

    with pytest.raises(ConnectionError) as exc:
        await _read_with_retry(call, label="probe")

    assert exc.value is boom, "the caller must see the real error, not a wrapper"
    assert call.call_count == 3, "one initial attempt plus one per configured delay"


@pytest.mark.asyncio
async def test_read_with_retry_passes_arguments_through(no_backoff):
    from app.tasks.scheduled import _read_with_retry

    call = MagicMock(return_value="ok")
    await _read_with_retry(call, "pos", label="probe", kw="value")
    call.assert_called_once_with("pos", kw="value")


@pytest.mark.asyncio
async def test_sentinel_survives_a_transient_probe_failure(no_backoff):
    """The bug: one dropped connection reported a healthy credential as dead."""
    from app.tasks.scheduled import verify_credentials

    with ExitStack() as stack:
        mocks = _patch_all_boundaries(stack)
        service = MagicMock()
        service.spreadsheets.return_value.get.return_value.execute.side_effect = [
            ConnectionError("remote end closed connection without response"),
            {"spreadsheetId": "sheet-id"},
        ]
        mocks["get_sheets_service"].return_value = service

        await verify_credentials()

    mocks["send_alert"].assert_not_called()
    assert mocks["ping"].await_count == 1, (
        "a recovered probe must still ping the heartbeat — silence is the alarm"
    )


@pytest.mark.asyncio
async def test_sentinel_still_reports_a_credential_that_fails_every_attempt(no_backoff):
    from app.tasks.scheduled import verify_credentials

    with ExitStack() as stack:
        mocks = _patch_all_boundaries(stack)
        mocks["get_seam_client"].side_effect = RuntimeError("Unauthorized")

        await verify_credentials()

    mocks["ping"].assert_not_awaited()
    failures = mocks["send_alert"].call_args.kwargs["failures"]
    assert any(f["credential"] == "seam" for f in failures)


@pytest.mark.asyncio
async def test_dead_anthropic_key_is_reported_like_any_other_credential():
    """The weekly inbox reviewer is only as alive as its key. Without this the
    key could die and the reviewer would go quiet with nothing saying so."""
    from app.tasks.scheduled import verify_credentials

    with ExitStack() as stack:
        mocks = _patch_all_boundaries(stack)
        mocks["get_anthropic_client"].return_value.models.retrieve.side_effect = (
            RuntimeError("authentication_error: invalid x-api-key")
        )
        await verify_credentials()

    mocks["ping"].assert_not_awaited()
    failures = mocks["send_alert"].call_args.kwargs["failures"]
    assert any(f["credential"] == "anthropic" for f in failures)


@pytest.mark.asyncio
async def test_unset_anthropic_key_counts_as_a_dead_credential():
    """An absent key and a deleted key are indistinguishable from here, and
    both stop the reviewer working. Treating absent as fine is how a safety net
    goes quiet."""
    from app.integrations.claude.client import AnthropicNotConfigured
    from app.tasks.scheduled import verify_credentials

    with ExitStack() as stack:
        mocks = _patch_all_boundaries(stack)
        mocks["get_anthropic_client"].side_effect = AnthropicNotConfigured(
            "ANTHROPIC_API_KEY is not set."
        )
        await verify_credentials()

    mocks["ping"].assert_not_awaited()
    failures = mocks["send_alert"].call_args.kwargs["failures"]
    assert any(f["credential"] == "anthropic" for f in failures)


@pytest.mark.asyncio
async def test_anthropic_probe_costs_no_tokens():
    """The probe is a metadata read. A probe that sent a message would bill the
    owner once a day, forever, to learn something a free GET already answers."""
    from app.tasks.scheduled import verify_credentials

    with ExitStack() as stack:
        mocks = _patch_all_boundaries(stack)
        await verify_credentials()

    client = mocks["get_anthropic_client"].return_value
    client.models.retrieve.assert_called_once()
    client.messages.create.assert_not_called()
