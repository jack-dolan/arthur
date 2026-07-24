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
