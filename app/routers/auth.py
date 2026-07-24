"""Google OIDC login for the owner dashboard.

Replaces the previous single shared-password HTTP Basic auth with per-user
"Sign in with Google". Any Google account can authenticate, but only the emails
in config.yaml ``dashboard.allowed_emails`` are authorized — everyone else gets
a 403. The signed-in user's email is kept in a signed session cookie
(Starlette SessionMiddleware, keyed by SECRET_KEY).

Routes:
  GET /login          — login page with a "Sign in with Google" button
  GET /auth/login     — redirect to Google's consent screen
  GET /auth/callback  — Google returns here; verify + allowlist + set session
  GET /logout         — clear the session
"""
from __future__ import annotations

import logging

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException

from app.config import load_config
from app.settings import settings

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"

# Authlib fetches the provider metadata lazily on first use, so registering with
# empty creds at import time does no network I/O (safe under tests).
oauth = OAuth()
oauth.register(
    name="google",
    server_metadata_url=_GOOGLE_METADATA,
    client_id=settings.google_oauth_client_id,
    client_secret=settings.google_oauth_client_secret,
    client_kwargs={"scope": "openid email profile"},
)


def _redirect_uri() -> str:
    """External callback URL — must match a redirect URI on the Google client."""
    if settings.domain in ("localhost", "127.0.0.1"):
        return "http://localhost:8000/auth/callback"
    return f"https://{settings.domain}/auth/callback"


def is_email_allowed(email: str | None) -> bool:
    """True iff *email* is in the dashboard allowlist (case-insensitive)."""
    if not email:
        return False
    return email.strip().lower() in load_config().dashboard.allowed_emails_normalized


def require_user(request: Request) -> str:
    """FastAPI dependency: return the signed-in allowlisted email, else redirect.

    Re-checks the allowlist on every request, so removing an email from
    config.yaml immediately revokes any live session.
    """
    user = request.session.get("user")
    if user and is_email_allowed(user):
        return user
    # 303 → the browser re-requests /login with GET (works for GET and POST).
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": "/login"},
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_email_allowed(request.session.get("user")):
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/auth/login")
async def auth_login(request: Request):
    return await oauth.google.authorize_redirect(request, _redirect_uri())


@router.get("/auth/callback")
async def auth_callback(request: Request):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        log.warning("OAuth callback error: %s", exc)
        return templates.TemplateResponse(
            request,
            "unauthorized.html",
            {"reason": "Sign-in did not complete. Please try again."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    userinfo = token.get("userinfo") or {}
    email = (userinfo.get("email") or "").strip().lower()
    if not userinfo.get("email_verified") or not is_email_allowed(email):
        log.warning("Rejected dashboard login for %r (unverified or not allowlisted)", email)
        who = email or "This Google account"
        return templates.TemplateResponse(
            request,
            "unauthorized.html",
            {"reason": f"{who} is not authorized to use this dashboard."},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    request.session["user"] = email
    log.info("Dashboard login: %s", email)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
