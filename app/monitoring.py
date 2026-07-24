"""Outbound heartbeat pings to the external dead-man's-switch monitor.

Sustainability audit 2026-07-23, item 1: every alert this app can send travels
through its own Gmail token, so the app cannot report its own death. The fix is
inverted monitoring — jobs ping a unique healthchecks.io URL on every
*successful* run, and the external service (with its own email/push channels)
alerts the owner when the pings stop.

Rules:
  - An empty URL means "not configured" and is a silent no-op, so the app runs
    unchanged before the monitor accounts exist.
  - A ping failure is logged and swallowed — a broken monitor must never break
    the job that pings it.
  - Jobs must ping only on success; the whole point is that silence == failure.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

_PING_TIMEOUT_SECONDS = 10.0


def ping_heartbeat(url: str, *, label: str) -> None:
    """Send a heartbeat ping (sync). No-op when *url* is empty; never raises."""
    if not url:
        return
    try:
        response = httpx.get(url, timeout=_PING_TIMEOUT_SECONDS, follow_redirects=True)
        response.raise_for_status()
        log.debug("Heartbeat ping sent (%s)", label)
    except Exception as exc:  # noqa: BLE001 — monitor failure must not break the caller
        log.warning("Heartbeat ping failed (%s): %s", label, exc)


async def ping_heartbeat_async(url: str, *, label: str) -> None:
    """Async wrapper: the sync HTTP call runs off the event loop (F11 rule)."""
    await asyncio.to_thread(ping_heartbeat, url, label=label)
