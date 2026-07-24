"""Unit tests for app/monitoring.py — external heartbeat pings.

The heartbeat URLs point at an external dead-man's-switch monitor
(healthchecks.io). The contract:

  - empty URL (unconfigured) → no network call at all
  - configured URL → a GET with a timeout
  - any failure is swallowed and logged — a broken monitor must never
    break the job that pings it

All httpx use is mocked at the module binding (app.monitoring.httpx).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_ping_heartbeat_noop_when_url_empty():
    from app.monitoring import ping_heartbeat

    with patch("app.monitoring.httpx") as mock_httpx:
        ping_heartbeat("", label="poller")

    mock_httpx.get.assert_not_called()


def test_ping_heartbeat_gets_url_with_timeout():
    from app.monitoring import ping_heartbeat

    with patch("app.monitoring.httpx") as mock_httpx:
        mock_httpx.get.return_value = MagicMock(status_code=200)
        ping_heartbeat("https://hc-ping.example/abc", label="poller")

    mock_httpx.get.assert_called_once()
    args, kwargs = mock_httpx.get.call_args
    assert args[0] == "https://hc-ping.example/abc"
    assert kwargs.get("timeout") is not None


def test_ping_heartbeat_swallows_failures():
    from app.monitoring import ping_heartbeat

    with patch("app.monitoring.httpx") as mock_httpx:
        mock_httpx.get.side_effect = ConnectionError("monitor down")
        # Must not raise
        ping_heartbeat("https://hc-ping.example/abc", label="poller")


@pytest.mark.asyncio
async def test_ping_heartbeat_async_delegates():
    from app import monitoring

    with patch("app.monitoring.ping_heartbeat") as mock_ping:
        await monitoring.ping_heartbeat_async("https://hc-ping.example/x", label="sentinel")

    mock_ping.assert_called_once_with("https://hc-ping.example/x", label="sentinel")
