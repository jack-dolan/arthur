"""Seam isolation test — offline (recorded) by default, live with --run-live.

Consolidates ``scripts/manual/test_seam_create.py`` + ``test_seam_delete.py``.
Exercises the production helpers (``_last_four_digits``, ``_checkin_window``)
and the SDK create/get/list/delete round-trip against:

  * a stateful recorded fake by default (no credentials), and
  * the real sandbox device under ``--run-live`` / ``RUN_LIVE=1``.

The window math (4 PM ET check-in → 11 AM ET checkout, stored as UTC) is
verified against the value the production ``_checkin_window`` computes.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest

from app.integrations.seam.client import get_seam_client
from app.tasks.handlers.access_code import _checkin_window, _last_four_digits
from tests.integration.fakes import FakeSeamClient, load_recorded

TEST_NAME = "ZZ TEST — delete me"
TEST_PHONE = "555-000-9999"  # last 4 → "9999"
CHECK_IN = date(2026, 6, 5)
CHECK_OUT = date(2026, 6, 6)


def _coerce_utc(val) -> datetime | None:
    """Coerce a Seam starts_at/ends_at (str or datetime) to a UTC datetime."""
    if val is None:
        return None
    if hasattr(val, "astimezone"):
        return val.astimezone(timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(val).astimezone(timezone.utc)
    except ValueError:
        return None


def _is_present(client, device_id, code_id) -> bool:
    return any(c.access_code_id == code_id for c in client.access_codes.list(device_id=device_id))


def _seam_roundtrip(client, device_id, *, delete_poll_delay: float):
    code = _last_four_digits(TEST_PHONE)
    assert code == "9999"
    starts_at, ends_at = _checkin_window(CHECK_IN, CHECK_OUT)
    # 4 PM EDT (UTC-4) → 20:00Z; 11 AM EDT → 15:00Z.
    assert starts_at == datetime(2026, 6, 5, 20, 0, tzinfo=timezone.utc)
    assert ends_at == datetime(2026, 6, 6, 15, 0, tzinfo=timezone.utc)

    created = client.access_codes.create(
        device_id=device_id,
        code=code,
        name=TEST_NAME,
        starts_at=starts_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ends_at=ends_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    code_id = created.access_code_id
    assert code_id

    try:
        fetched = client.access_codes.get(access_code_id=code_id)
        assert fetched.code == code
        assert fetched.name == TEST_NAME
        assert _coerce_utc(fetched.starts_at) == starts_at
        assert _coerce_utc(fetched.ends_at) == ends_at
        assert _is_present(client, device_id, code_id)
    finally:
        client.access_codes.delete(access_code_id=code_id)

    # Seam processes deletes asynchronously; poll for removal (immediate offline).
    for _ in range(5):
        if not _is_present(client, device_id, code_id):
            break
        time.sleep(delete_poll_delay)
    assert not _is_present(client, device_id, code_id), "access code not removed"


def test_seam_offline_roundtrip():
    """Offline: create/get/delete against the recorded stateful fake (no creds)."""
    rec = load_recorded("external_responses.json")["seam"]
    client = FakeSeamClient(seed_id=rec["access_code_id"])
    _seam_roundtrip(client, rec["device_id"], delete_poll_delay=0)


@pytest.mark.live
def test_seam_live_roundtrip():
    """Live: same round-trip against the real sandbox device."""
    from app.config import load_config

    prop = load_config().properties[0]
    client = get_seam_client()
    _seam_roundtrip(client, prop.seam_device_id, delete_poll_delay=2)
