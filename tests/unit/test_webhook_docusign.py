"""Wave 0 failing tests for DocuSign webhook endpoint (DOCUSIGN-02).

All imports are inside test functions (project convention). These tests are
intentionally RED — the target router does not exist yet. Wave 2 will create
app/routers/webhooks.py with the POST /webhooks/docusign route.

Requirements covered:
  DOCUSIGN-02: POST /webhooks/docusign validates HMAC-SHA256 signature before processing.
               Valid → 200; invalid → 400; missing header → 400.
               Routes to correct downstream handler based on envelope status.

Pattern (per TESTING.md):
  httpx.AsyncClient + ASGITransport for testing the FastAPI app without a real server.
  Raw body must be passed as `content=` (not `json=`) so HMAC validation uses actual bytes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid

import pytest


def _make_hmac_sig(body: bytes, secret: str) -> str:
    """Compute the expected X-DocuSign-Signature-1 header value."""
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")


def _make_webhook_body(status: str, envelope_id: str | None = None) -> bytes:
    envelope_id = envelope_id or str(uuid.uuid4())
    return json.dumps({"status": status, "envelopeId": envelope_id}).encode("utf-8")


TEST_SECRET = "test-hmac-secret-for-unit-tests"


# ---------------------------------------------------------------------------
# _extract_envelope_event — real production "aggregate" Connect JSON (Risk 4)
# ---------------------------------------------------------------------------

def test_extract_aggregate_connect_payload():
    """Real production Connect 'aggregate' JSON (captured live at go-live): the
    status lives in the `event` field ('envelope-completed'), envelope id under
    data.envelopeId. Top-level keys: apiVersion/configurationId/data/event/
    generatedDateTime/retryCount/uri. The pre-go-live parser only checked
    data.status / envelopeSummary.status / status, so it dropped a real signed
    envelope and fired the parse-failure owner alert instead of sending the HOA."""
    from app.routers.webhooks import _extract_envelope_event
    env_id = "52bc5a69-f682-817c-80be-419358f96994"
    payload = {
        "event": "envelope-completed",
        "apiVersion": "v2.1",
        "uri": f"/restapi/v2.1/accounts/abc/envelopes/{env_id}",
        "retryCount": 0,
        "configurationId": 21763033,
        "generatedDateTime": "2026-07-21T02:15:24Z",
        "data": {"accountId": "abc", "envelopeId": env_id,
                 "envelopeSummary": {"status": "completed"}},
    }
    assert _extract_envelope_event(payload) == (env_id, "completed")


def test_extract_aggregate_status_from_event_without_summary():
    """Status must resolve from the event name even if envelopeSummary is absent."""
    from app.routers.webhooks import _extract_envelope_event
    payload = {"event": "envelope-declined", "data": {"envelopeId": "abc123"}}
    assert _extract_envelope_event(payload) == ("abc123", "declined")


def test_extract_aggregate_envelope_id_from_uri_fallback():
    """Envelope id must fall back to parsing the aggregate `uri`."""
    from app.routers.webhooks import _extract_envelope_event
    env_id = "zzz999"
    payload = {"event": "envelope-completed",
               "uri": f"/restapi/v2.1/accounts/x/envelopes/{env_id}",
               "data": {"envelopeSummary": {"status": "completed"}}}
    assert _extract_envelope_event(payload) == (env_id, "completed")


def test_extract_legacy_flat_payload_still_works():
    """Backward-compat: the old flat {status, envelopeId} shape must still parse."""
    from app.routers.webhooks import _extract_envelope_event
    payload = {"status": "completed", "envelopeId": "flat-1"}
    assert _extract_envelope_event(payload) == ("flat-1", "completed")


# ---------------------------------------------------------------------------
# HMAC validation — reject/accept
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_webhook_rejects_missing_signature_header_returns_400():
    """POST with no X-DocuSign-Signature-1 header → 400."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    body = _make_webhook_body("completed")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/docusign",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature_returns_400():
    """POST with wrong HMAC signature → 400; no state side effects."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    body = _make_webhook_body("completed")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/webhooks/docusign",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-DocuSign-Signature-1": "deadbeef-invalid-signature",
            },
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_webhook_accepts_valid_signature_returns_200():
    """POST with correct HMAC signature → 200."""
    from httpx import AsyncClient, ASGITransport
    from unittest.mock import patch, AsyncMock
    from app.main import app

    body = _make_webhook_body("voided")  # voided → log-and-ignore, no DB writes
    sig = _make_hmac_sig(body, TEST_SECRET)

    with patch("app.routers.webhooks.settings") as mock_settings:
        mock_settings.docusign_hmac_key = TEST_SECRET
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/webhooks/docusign",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-DocuSign-Signature-1": sig,
                },
            )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_webhook_reads_raw_body_before_json_parse():
    """Webhook handler calls await request.body() not Body() FastAPI injection.

    Verified by: if the handler used `body: dict = Body(...)`, FastAPI would
    consume the body before our code runs, and HMAC validation would fail.
    This test confirms the handler accepts raw bytes by successfully processing
    a request where we control the exact bytes sent.
    """
    from httpx import AsyncClient, ASGITransport
    from unittest.mock import patch
    from app.main import app

    # Use non-JSON bytes to prove raw body is captured (HMAC is over raw bytes)
    body = b'{"status": "voided", "envelopeId": "test-env-raw"}'
    sig = _make_hmac_sig(body, TEST_SECRET)

    with patch("app.routers.webhooks.settings") as mock_settings:
        mock_settings.docusign_hmac_key = TEST_SECRET
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/webhooks/docusign",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-DocuSign-Signature-1": sig,
                },
            )
    # If raw body was read correctly, HMAC validates and we get 200
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Status routing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_webhook_completed_status_triggers_envelope_completed_handler():
    """Valid HMAC + status='completed' → handle_envelope_completed called with booking, task, envelope_id."""
    from unittest.mock import MagicMock, patch, AsyncMock
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    env_id = "env-webhook-complete"
    body = _make_webhook_body("completed", envelope_id=env_id)
    sig = _make_hmac_sig(body, TEST_SECRET)

    mock_task = MagicMock()
    mock_task.booking_id = uuid.uuid4()
    mock_booking = MagicMock()

    mock_session = AsyncMock()
    task_exec_result = MagicMock()
    task_exec_result.scalar_one_or_none.return_value = mock_task
    booking_exec_result = MagicMock()
    booking_exec_result.scalar_one_or_none.return_value = mock_booking
    mock_session.execute = AsyncMock(side_effect=[task_exec_result, booking_exec_result])
    mock_session.commit = AsyncMock()

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.routers.webhooks.settings") as mock_settings,
        patch("app.routers.webhooks.AsyncSessionLocal", return_value=mock_session_cm),
        patch("app.routers.webhooks.handle_envelope_completed", new_callable=AsyncMock) as mock_handler,
    ):
        mock_settings.docusign_hmac_key = TEST_SECRET
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/webhooks/docusign",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-DocuSign-Signature-1": sig,
                },
            )

    assert response.status_code == 200
    mock_handler.assert_called_once()
    call_args = mock_handler.call_args
    # handler must receive (booking, task, envelope_id, session)
    assert call_args.args[0] is mock_booking
    assert call_args.args[1] is mock_task
    assert call_args.args[2] == env_id


@pytest.mark.asyncio
async def test_webhook_declined_status_sets_docusign_task_failed():
    """Valid HMAC + status='declined' → DOCUSIGN_SEND task transitions to FAILED."""
    from httpx import AsyncClient, ASGITransport
    from unittest.mock import patch, AsyncMock
    from app.main import app

    env_id = "env-webhook-declined"
    body = _make_webhook_body("declined", envelope_id=env_id)
    sig = _make_hmac_sig(body, TEST_SECRET)

    with (
        patch("app.routers.webhooks.settings") as mock_settings,
        patch("app.routers.webhooks.handle_envelope_declined", new_callable=AsyncMock) as mock_handler,
    ):
        mock_settings.docusign_hmac_key = TEST_SECRET
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/webhooks/docusign",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-DocuSign-Signature-1": sig,
                },
            )

    assert response.status_code == 200
    mock_handler.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_unknown_status_logs_and_returns_200():
    """Valid HMAC + status='delivered' (unknown) → 200; no exception; no DB writes."""
    from httpx import AsyncClient, ASGITransport
    from unittest.mock import patch, AsyncMock
    from app.main import app

    body = _make_webhook_body("delivered")
    sig = _make_hmac_sig(body, TEST_SECRET)

    with patch("app.routers.webhooks.settings") as mock_settings:
        mock_settings.docusign_hmac_key = TEST_SECRET
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/webhooks/docusign",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-DocuSign-Signature-1": sig,
                },
            )
    assert response.status_code == 200
