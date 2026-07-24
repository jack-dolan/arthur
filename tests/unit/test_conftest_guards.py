"""Self-test for the fail-loud external-API guard in tests/conftest.py.

The test environment carries the real production .env, so an unmocked path to
any external client performs REAL side effects (live email, real lock codes,
real envelopes, real sheet rows). That leaked twice — Step 14 (integration
scope) and 2026-07-22 (unit scope, which the old integration-only guard never
covered). These tests pin the guard itself: every client constructor must
raise loudly in a non-live test. If a refactor ever renames a guarded binding
(e.g. moves the httpx import or the `build` call), these fail immediately
instead of the guard silently no-longer-applying.
"""
from __future__ import annotations

import pytest


def test_gmail_guard_trips_on_unmocked_service():
    from app.integrations.gmail.oauth import get_alerts_service, get_booking_feed_service

    with pytest.raises(RuntimeError, match="blocked by the test guard"):
        get_alerts_service()
    with pytest.raises(RuntimeError, match="blocked by the test guard"):
        get_booking_feed_service()


def test_sheets_guard_trips_on_unmocked_service():
    from app.integrations.sheets.client import get_sheets_service

    with pytest.raises(RuntimeError, match="blocked by the test guard"):
        get_sheets_service()


def test_seam_guard_trips_on_unmocked_client():
    from app.integrations.seam.client import get_seam_client

    with pytest.raises(RuntimeError, match="blocked by the test guard"):
        get_seam_client()


def test_docusign_guard_trips_on_unmocked_token_exchange():
    """Every DocuSign call path starts with the httpx refresh-token exchange,
    so blocking it means no authenticated SDK client can ever be built."""
    from unittest.mock import patch

    # Synthetic credentials so the guard is what stops the call, not the
    # "no refresh token configured" precondition — this test must fail for
    # exactly one reason, and it must not depend on a real .env being present.
    with patch("app.integrations.docusign.client.settings") as mock_settings:
        mock_settings.docusign_sandbox = True
        mock_settings.docusign_refresh_token = "rt"
        mock_settings.docusign_client_id = "cid"
        mock_settings.docusign_client_secret = "csec"
        from app.integrations.docusign.client import _refresh_access_token

        with pytest.raises(RuntimeError, match="blocked by the test guard"):
            _refresh_access_token()
