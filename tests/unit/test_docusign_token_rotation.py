"""Step 19 / R5 — DocuSign refresh-token rotation + free keep-alive.

DocuSign rotates the refresh token on every refresh-token grant and gives the
new one a fresh 30-day life. The old client discarded it, so the original
token's clock never reset and it died 30 days after minting regardless of
activity. These tests lock in: (1) the rotated token is persisted, (2) the
persistence rewrites only the DOCUSIGN_REFRESH_TOKEN line in .env, and (3) the
weekly keep-alive job refreshes the token WITHOUT sending an envelope.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_refresh_access_token_persists_rotated_token():
    from app.integrations.docusign import client

    with (
        patch.object(client.settings, "docusign_refresh_token", "OLD-TOKEN"),
        patch.object(client.settings, "docusign_client_id", "cid"),
        patch.object(client.settings, "docusign_client_secret", "secret"),
        patch(
            "app.integrations.docusign.client.httpx.post",
            return_value=_mock_response(
                {"access_token": "AT-123", "refresh_token": "NEW-TOKEN"}
            ),
        ),
        patch(
            "app.integrations.docusign.client._persist_refresh_token"
        ) as mock_persist,
    ):
        token = client._refresh_access_token()

    assert token == "AT-123"
    mock_persist.assert_called_once_with("NEW-TOKEN")


def test_refresh_access_token_no_persist_when_token_unchanged():
    from app.integrations.docusign import client

    with (
        patch.object(client.settings, "docusign_refresh_token", "SAME"),
        patch(
            "app.integrations.docusign.client.httpx.post",
            return_value=_mock_response(
                {"access_token": "AT-123", "refresh_token": "SAME"}
            ),
        ),
        patch(
            "app.integrations.docusign.client._persist_refresh_token"
        ) as mock_persist,
    ):
        token = client._refresh_access_token()

    assert token == "AT-123"
    mock_persist.assert_not_called()


def test_refresh_access_token_backward_compatible_without_refresh_token():
    """If DocuSign omits refresh_token from a response, still return the access
    token and never crash (no persistence)."""
    from app.integrations.docusign import client

    with (
        patch.object(client.settings, "docusign_refresh_token", "OLD"),
        patch(
            "app.integrations.docusign.client.httpx.post",
            return_value=_mock_response({"access_token": "AT-123"}),
        ),
        patch(
            "app.integrations.docusign.client._persist_refresh_token"
        ) as mock_persist,
    ):
        token = client._refresh_access_token()

    assert token == "AT-123"
    mock_persist.assert_not_called()


def test_persist_refresh_token_rewrites_only_its_line(tmp_path):
    from app.integrations.docusign import client

    env = tmp_path / ".env"
    env.write_text(
        "# comment line\n"
        "DATABASE_URL=postgres://x\n"
        "DOCUSIGN_REFRESH_TOKEN=OLD-TOKEN\n"
        "DOCUSIGN_SANDBOX=true\n"
    )

    with (
        patch.object(client, "_ENV_PATH", env),
        patch.object(client.settings, "docusign_refresh_token", "OLD-TOKEN"),
    ):
        client._persist_refresh_token("NEW-TOKEN")
        # In-memory settings updated so the running process uses the new token.
        assert client.settings.docusign_refresh_token == "NEW-TOKEN"

    text = env.read_text()
    assert "DOCUSIGN_REFRESH_TOKEN=NEW-TOKEN" in text
    assert "OLD-TOKEN" not in text
    # Every other line preserved verbatim.
    assert "# comment line" in text
    assert "DATABASE_URL=postgres://x" in text
    assert "DOCUSIGN_SANDBOX=true" in text


def test_persist_refresh_token_appends_when_line_absent(tmp_path):
    from app.integrations.docusign import client

    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=postgres://x\n")

    with (
        patch.object(client, "_ENV_PATH", env),
        patch.object(client.settings, "docusign_refresh_token", ""),
    ):
        client._persist_refresh_token("NEW-TOKEN")

    assert "DOCUSIGN_REFRESH_TOKEN=NEW-TOKEN" in env.read_text()


@pytest.mark.asyncio
async def test_keepalive_job_refreshes_token_without_sending_envelope():
    """The weekly keep-alive exchanges the refresh token (free OAuth call) and
    never touches the EnvelopesApi — so it costs no envelope (R5)."""
    from app.tasks import scheduled

    with (
        patch(
            "app.integrations.docusign.client._refresh_access_token",
            return_value="AT",
        ) as mock_refresh,
        patch(
            "app.integrations.docusign.client.get_envelope_api"
        ) as mock_envelope_api,
    ):
        await scheduled.refresh_docusign_token()

    mock_refresh.assert_called_once()
    mock_envelope_api.assert_not_called()


# ---------------------------------------------------------------------------
# F4 (bug hunt 2026-07-22) — restart-durable token store.
#
# In the container, .env does not exist (creds arrive as env vars via compose
# env_file), so rotations lived only in process memory: the HOST .env token —
# reloaded on every restart — kept its original 30-day clock from the last
# manual mint, and any restart after that clock ran out bricked DocuSign.
# Rotations are now ALSO persisted to /app/data (the mounted volume), which
# the exchange prefers on the next run, surviving restarts.
# ---------------------------------------------------------------------------


def _mock_ok(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _mock_invalid_grant() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 400
    resp.text = '{"error":"invalid_grant"}'
    return resp


def test_persist_writes_rotated_token_to_durable_store(tmp_path):
    from app.integrations.docusign import client

    store = tmp_path / "docusign_refresh_token"
    env = tmp_path / ".env"
    env.write_text("DOCUSIGN_REFRESH_TOKEN=OLD-TOKEN\n")

    with (
        patch.object(client, "_TOKEN_STORE", store),
        patch.object(client, "_ENV_PATH", env),
        patch.object(client.settings, "docusign_refresh_token", "OLD-TOKEN"),
    ):
        client._persist_refresh_token("NEW-TOKEN")

    assert store.read_text().strip() == "NEW-TOKEN"


def test_exchange_prefers_store_token_over_settings(tmp_path):
    """The store always holds the most recent rotation, so it wins over the
    (potentially stale) env-var token loaded at boot."""
    from app.integrations.docusign import client

    store = tmp_path / "docusign_refresh_token"
    store.write_text("STORE-TOKEN\n")

    with (
        patch.object(client, "_TOKEN_STORE", store),
        patch.object(client, "_ENV_PATH", tmp_path / ".env"),
        patch.object(client.settings, "docusign_refresh_token", "ENV-TOKEN"),
        patch(
            "app.integrations.docusign.client.httpx.post",
            return_value=_mock_ok({"access_token": "AT-1"}),
        ) as mock_post,
    ):
        token = client._refresh_access_token()

    assert token == "AT-1"
    assert mock_post.call_args.kwargs["data"]["refresh_token"] == "STORE-TOKEN"


def test_exchange_falls_back_to_settings_token_on_invalid_grant(tmp_path):
    """A stale store (e.g. after an operator re-mints the host .env token
    because everything expired) must not brick the exchange: on invalid_grant
    the settings token is tried next, and the fresh rotation overwrites the
    stale store."""
    from app.integrations.docusign import client

    store = tmp_path / "docusign_refresh_token"
    store.write_text("DEAD-STORE-TOKEN\n")
    env = tmp_path / ".env"
    env.write_text("DOCUSIGN_REFRESH_TOKEN=FRESH-ENV-TOKEN\n")

    with (
        patch.object(client, "_TOKEN_STORE", store),
        patch.object(client, "_ENV_PATH", env),
        patch.object(client.settings, "docusign_refresh_token", "FRESH-ENV-TOKEN"),
        patch(
            "app.integrations.docusign.client.httpx.post",
            side_effect=[
                _mock_invalid_grant(),
                _mock_ok({"access_token": "AT-2", "refresh_token": "ROTATED"}),
            ],
        ) as mock_post,
    ):
        token = client._refresh_access_token()
        # Checked inside the patch context — patch.object restores the real
        # settings value on exit.
        assert client.settings.docusign_refresh_token == "ROTATED"

    assert token == "AT-2"
    tried = [c.kwargs["data"]["refresh_token"] for c in mock_post.call_args_list]
    assert tried == ["DEAD-STORE-TOKEN", "FRESH-ENV-TOKEN"]
    assert store.read_text().strip() == "ROTATED"


def test_exchange_raises_clearly_when_no_token_configured(tmp_path):
    from app.integrations.docusign import client

    with (
        patch.object(client, "_TOKEN_STORE", tmp_path / "missing"),
        patch.object(client.settings, "docusign_refresh_token", ""),
    ):
        with pytest.raises(RuntimeError, match="refresh token"):
            client._refresh_access_token()


@pytest.mark.asyncio
async def test_keepalive_failure_sends_owner_alert():
    """A failed weekly keep-alive is the early warning before the 30-day token
    death — it must alert the owner, not just log (the job runs unattended)."""
    from app.tasks import scheduled

    with (
        patch(
            "app.integrations.docusign.client._refresh_access_token",
            side_effect=RuntimeError("invalid_grant"),
        ),
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
        patch(
            "app.tasks.scheduled.send_docusign_keepalive_failure_alert"
        ) as mock_alert,
        patch("app.tasks.scheduled.load_config") as mock_cfg,
    ):
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        await scheduled.refresh_docusign_token()  # must not raise

    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_keepalive_success_pings_heartbeat():
    """A successful keep-alive pings its external heartbeat (sustainability
    audit item 2): heartbeat silence means the keep-alive stopped running or
    stopped succeeding, caught by the monitor even if alert email is dead."""
    from app.tasks import scheduled

    with (
        patch(
            "app.integrations.docusign.client._refresh_access_token",
            return_value="AT",
        ),
        patch(
            "app.tasks.scheduled.ping_heartbeat_async", new_callable=AsyncMock
        ) as mock_ping,
    ):
        await scheduled.refresh_docusign_token()

    assert mock_ping.await_count == 1
    assert mock_ping.await_args.kwargs.get("label") == "docusign-keepalive"


@pytest.mark.asyncio
async def test_keepalive_failure_does_not_ping_heartbeat():
    from app.tasks import scheduled

    with (
        patch(
            "app.integrations.docusign.client._refresh_access_token",
            side_effect=RuntimeError("invalid_grant"),
        ),
        patch("app.tasks.scheduled.load_config"),
        patch("app.tasks.scheduled.get_alerts_service"),
        patch("app.tasks.scheduled.send_docusign_keepalive_failure_alert"),
        patch(
            "app.tasks.scheduled.ping_heartbeat_async", new_callable=AsyncMock
        ) as mock_ping,
    ):
        await scheduled.refresh_docusign_token()

    mock_ping.assert_not_awaited()
