import google_auth_httplib2
import httplib2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.settings import settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

# httplib2's default timeout is None — a single hung Gmail socket would wedge
# its caller forever (bug hunt F11). All Gmail HTTP goes through this bound.
HTTP_TIMEOUT_SECONDS = 30


def _build_credentials(refresh_token: str) -> Credentials:
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        # S106 is a false positive: this is Google's public, documented token
        # endpoint, not a credential. The actual secrets are the two settings below.
        token_uri="https://oauth2.googleapis.com/token",  # noqa: S106
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )


def _build_gmail_service(refresh_token: str):
    creds = _build_credentials(refresh_token)
    authed_http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS)
    )
    return build("gmail", "v1", http=authed_http)


def get_booking_feed_service():
    """Returns an authenticated Gmail API client for the booking feed inbox."""
    return _build_gmail_service(settings.gmail_booking_feed_refresh_token)


def get_alerts_service():
    """Returns an authenticated Gmail API client for the alerts inbox."""
    return _build_gmail_service(settings.gmail_alerts_refresh_token)
