"""Unit tests for the DocuSign environment/host banner (Step 22, item 5).

At go-live the single most consequential setting is DOCUSIGN_SANDBOX: false
routes every call at real envelopes that cost real money. The app must state,
at startup, which environment and hosts it will target, so the cutover can be
confirmed from `docker compose logs app` rather than inferred from .env.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from app.integrations.docusign.client import log_docusign_target


def _settings(sandbox: bool, api_base_uri: str = "") -> MagicMock:
    mock = MagicMock()
    mock.docusign_sandbox = sandbox
    mock.docusign_api_base_uri = api_base_uri
    return mock


def test_log_docusign_target_reports_production_hosts(caplog):
    """DOCUSIGN_SANDBOX=false with no region override falls back to the www host."""
    with patch("app.integrations.docusign.client.settings", _settings(False)):
        with caplog.at_level(logging.INFO, logger="app.integrations.docusign.client"):
            oauth_host, api_host = log_docusign_target()

    assert oauth_host == "account.docusign.com"
    assert api_host == "https://www.docusign.net/restapi"
    message = caplog.text
    assert "PRODUCTION" in message
    assert "account.docusign.com" in message
    assert "https://www.docusign.net/restapi" in message
    # Must not claim sandbox when it is live.
    assert "demo.docusign.net" not in message
    assert "account-d.docusign.com" not in message


def test_log_docusign_target_uses_configured_region_base_uri(caplog):
    """DOCUSIGN_API_BASE_URI overrides the API host for the account's region.

    DocuSign is multi-region: an account on na4 must have its REST calls sent to
    na4.docusign.net, not the global www host. The account's base_uri comes from
    OAuth userinfo at go-live; hardcoding www would 401/misroute a non-www account.
    """
    region = "https://na4.docusign.net/restapi"
    with patch("app.integrations.docusign.client.settings", _settings(False, region)):
        with caplog.at_level(logging.INFO, logger="app.integrations.docusign.client"):
            oauth_host, api_host = log_docusign_target()

    assert oauth_host == "account.docusign.com"
    assert api_host == region
    assert region in caplog.text
    # The wrong (global) host must not be what we target.
    assert "www.docusign.net" not in caplog.text


def test_region_base_uri_ignored_in_sandbox(caplog):
    """A stray DOCUSIGN_API_BASE_URI must not leak into sandbox routing."""
    with patch(
        "app.integrations.docusign.client.settings",
        _settings(True, "https://na4.docusign.net/restapi"),
    ):
        oauth_host, api_host = log_docusign_target()
    assert oauth_host == "account-d.docusign.com"
    assert api_host == "https://demo.docusign.net/restapi"


def test_log_docusign_target_reports_sandbox_hosts(caplog):
    """DOCUSIGN_SANDBOX=true must announce SANDBOX and the demo hosts."""
    with patch("app.integrations.docusign.client.settings", _settings(True)):
        with caplog.at_level(logging.INFO, logger="app.integrations.docusign.client"):
            oauth_host, api_host = log_docusign_target()

    assert oauth_host == "account-d.docusign.com"
    assert api_host == "https://demo.docusign.net/restapi"
    message = caplog.text
    assert "SANDBOX" in message
    assert "account-d.docusign.com" in message
    assert "demo.docusign.net" in message
