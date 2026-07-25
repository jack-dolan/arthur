#!/usr/bin/env python
"""Obtain a Gmail OAuth refresh token for one of the two service accounts.

Usage (run from repo root):
    .venv/bin/python scripts/manual/get_gmail_refresh_tokens.py --account booking-feed
    .venv/bin/python scripts/manual/get_gmail_refresh_tokens.py --account alerts

Each run opens a browser-based authorization flow on port 8765.
Because the VPS is headless, forward port 8765 via SSH before running:

    ssh -L 8765:localhost:8765 <your-vps>

Then open the printed URL in your LOCAL browser, sign in as the correct
Gmail account, and approve the scopes.  The script writes the refresh
token directly to .env — it is never printed.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

ACCOUNT_MAP = {
    "booking-feed": "GMAIL_BOOKING_FEED_REFRESH_TOKEN",
    "alerts": "GMAIL_ALERTS_REFRESH_TOKEN",
}


def _load_env(path: str) -> str:
    with open(path) as f:
        return f.read()


def _set_env_var(env_path: str, key: str, value: str) -> None:
    content = _load_env(env_path)
    pattern = rf"^({re.escape(key)}=).*$"
    replacement = rf"\g<1>{value}"
    new_content, n = re.subn(pattern, replacement, content, flags=re.MULTILINE)
    if n == 0:
        new_content = content.rstrip("\n") + f"\n{key}={value}\n"
    with open(env_path, "w") as f:
        f.write(new_content)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--account",
        required=True,
        choices=list(ACCOUNT_MAP.keys()),
        help="Which Gmail account to authorize",
    )
    args = parser.parse_args()

    env_key = ACCOUNT_MAP[args.account]
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        ".env",
    )

    if not os.path.exists(env_path):
        print(f"ERROR: .env not found at {env_path}", file=sys.stderr)
        sys.exit(1)

    # Load client credentials from .env
    from app.settings import settings

    if not settings.google_client_id or not settings.google_client_secret:
        print(
            "ERROR: GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be set in .env",
            file=sys.stderr,
        )
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uris": ["http://localhost:8765"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    print(f"\nAccount role : {args.account}")
    print(f"Env variable : {env_key}")
    print("\nStarting OAuth flow on http://localhost:8765 ...")
    print("Make sure you are forwarding this port via SSH:")
    print("    ssh -L 8765:localhost:8765 <your-vps>")
    print("\nWhen the URL is printed below, open it in your LOCAL browser.")
    print(f"Sign in as the '{args.account}' Gmail account and approve both scopes.")
    print("-" * 60)

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=8765, open_browser=False)

    if not creds.refresh_token:
        print("\nERROR: no refresh_token returned. This can happen if the account already")
        print("has an active grant. Revoke access at https://myaccount.google.com/permissions")
        print("and re-run this script.")
        sys.exit(1)

    # Write to .env without echoing the value.
    _set_env_var(env_path, env_key, creds.refresh_token)
    print(f"\n{env_key} written to .env  (length: {len(creds.refresh_token)} chars)")

    # Smoke test: make one API call to verify the token works.
    print("\nVerifying token with a live API call ...")
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    print(f"Authenticated as : {profile.get('emailAddress')}")
    print(f"Messages total   : {profile.get('messagesTotal', 'n/a')}")
    print("\nDONE — token is valid and written to .env.")


if __name__ == "__main__":
    main()
