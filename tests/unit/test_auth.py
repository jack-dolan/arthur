"""Unit tests for the dashboard Google-OIDC auth (app/routers/auth.py).

The Google network boundary is mocked: we patch the Authlib client's
``authorize_access_token`` and the config loader's allowlist. No real OAuth.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse

from app.config import AppConfig, DashboardConfig, load_config
import app.routers.auth as auth

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _fake_template_response(request, name, context, status_code=200):
    """Stand-in for Jinja TemplateResponse — avoids needing a real Request
    (url_for). Preserves the status_code the route sets."""
    return PlainTextResponse(context.get("reason", name), status_code=status_code)


# The allowlist under test is config-supplied, so these tests derive it from the
# fixture config instead of hardcoding addresses.
ALLOWED = list(load_config(FIXTURES / "config.test.yaml").dashboard.allowed_emails)
OWNER, COHOST = ALLOWED[0], ALLOWED[1]
STRANGER = "stranger@example.com"
OTHER = "other@example.com"


def _config_with(emails):
    cfg = MagicMock(spec=AppConfig)
    cfg.dashboard = DashboardConfig(allowed_emails=list(emails))
    return cfg


def _request(session: dict | None = None):
    """A minimal Request-like object exposing .session."""
    return SimpleNamespace(session=session if session is not None else {})


# ---------------------------------------------------------------------------
# is_email_allowed
# ---------------------------------------------------------------------------

def test_is_email_allowed_matches_case_insensitively():
    with patch.object(auth, "load_config", return_value=_config_with(ALLOWED)):
        assert auth.is_email_allowed(OWNER.upper()) is True
        assert auth.is_email_allowed(f"  {COHOST} ") is True


def test_is_email_allowed_rejects_unknown_and_empty():
    with patch.object(auth, "load_config", return_value=_config_with(ALLOWED)):
        assert auth.is_email_allowed(STRANGER) is False
        assert auth.is_email_allowed("") is False
        assert auth.is_email_allowed(None) is False


def test_is_email_allowed_empty_allowlist_fails_closed():
    with patch.object(auth, "load_config", return_value=_config_with(set())):
        assert auth.is_email_allowed(OWNER) is False


# ---------------------------------------------------------------------------
# require_user dependency
# ---------------------------------------------------------------------------

def test_require_user_returns_allowlisted_session_email():
    with patch.object(auth, "load_config", return_value=_config_with(ALLOWED)):
        assert auth.require_user(_request({"user": OWNER})) == OWNER


def test_require_user_redirects_when_no_session():
    with patch.object(auth, "load_config", return_value=_config_with(ALLOWED)):
        with pytest.raises(HTTPException) as exc:
            auth.require_user(_request({}))
    assert exc.value.status_code == 303
    assert exc.value.headers["Location"] == "/login"


def test_require_user_redirects_when_email_no_longer_allowlisted():
    # Session says a user, but the allowlist no longer contains them.
    with patch.object(auth, "load_config", return_value=_config_with({OTHER})):
        with pytest.raises(HTTPException) as exc:
            auth.require_user(_request({"user": OWNER}))
    assert exc.value.status_code == 303


# ---------------------------------------------------------------------------
# /auth/callback
# ---------------------------------------------------------------------------

def _token(email, *, verified=True):
    return {"userinfo": {"email": email, "email_verified": verified}}


async def test_callback_sets_session_and_redirects_for_allowlisted():
    request = _request({})
    with (
        patch.object(auth, "load_config", return_value=_config_with(ALLOWED)),
        patch.object(auth.oauth, "google") as goog,
    ):
        goog.authorize_access_token = AsyncMock(return_value=_token(OWNER))
        resp = await auth.auth_callback(request)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert request.session["user"] == OWNER


async def test_callback_rejects_non_allowlisted_email():
    request = _request({})
    with (
        patch.object(auth, "load_config", return_value=_config_with(ALLOWED)),
        patch.object(auth.oauth, "google") as goog,
        patch.object(auth.templates, "TemplateResponse", side_effect=_fake_template_response),
    ):
        goog.authorize_access_token = AsyncMock(return_value=_token(STRANGER))
        resp = await auth.auth_callback(request)
    assert resp.status_code == 403
    assert "user" not in request.session


async def test_callback_rejects_unverified_email():
    request = _request({})
    with (
        patch.object(auth, "load_config", return_value=_config_with(ALLOWED)),
        patch.object(auth.oauth, "google") as goog,
        patch.object(auth.templates, "TemplateResponse", side_effect=_fake_template_response),
    ):
        goog.authorize_access_token = AsyncMock(
            return_value=_token(OWNER, verified=False)
        )
        resp = await auth.auth_callback(request)
    assert resp.status_code == 403
    assert "user" not in request.session


# ---------------------------------------------------------------------------
# /logout
# ---------------------------------------------------------------------------

async def test_logout_clears_session_and_redirects():
    request = _request({"user": OWNER})
    resp = await auth.logout(request)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert "user" not in request.session
