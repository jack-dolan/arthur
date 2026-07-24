"""Gmail send isolation test — offline (recorded) by default, live with --run-live.

Consolidates ``scripts/manual/test_email_send.py``. Confirms the alerts account
profile, then sends one clearly-labelled test message via the Gmail API (not
SMTP) and confirms the API returned a message id. Runs against:

  * a recorded fake Gmail service by default (no credentials), and
  * the real alerts Gmail account under ``--run-live`` / ``RUN_LIVE=1``.

The live send goes to the alerts account's OWN address (self-send), so a live
run never emails an external recipient. Under ``--run-live`` this test is
explicitly ``live``-marked, so the integration ``_block_live_gmail`` safety net
steps aside for it (and only it).
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.mime.text import MIMEText

import pytest

from tests.integration.fakes import FakeGmailService, load_recorded


def _build_raw_message(sender: str, recipient: str) -> dict:
    stamp = datetime.now(timezone.utc).isoformat()
    msg = MIMEText(f"Rental-automation Gmail isolation test — {stamp}. Safe to ignore.")
    msg["To"] = recipient
    msg["From"] = sender
    msg["Subject"] = "ZZ TEST — Gmail isolation test (safe to ignore)"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": raw}


def _gmail_send_roundtrip(service):
    profile = service.users().getProfile(userId="me").execute()
    sender = profile["emailAddress"]
    assert "@" in sender

    body = _build_raw_message(sender, sender)  # self-send
    result = service.users().messages().send(userId="me", body=body).execute()
    assert result.get("id"), "Gmail send did not return a message id"
    return sender, result["id"]


def test_gmail_offline_roundtrip():
    """Offline: profile + send against the recorded fake service (no creds)."""
    rec = load_recorded("external_responses.json")["gmail"]
    service = FakeGmailService(rec["alerts_profile_email"], rec["sent_message_id"])

    sender, message_id = _gmail_send_roundtrip(service)
    assert sender == rec["alerts_profile_email"]
    assert message_id == rec["sent_message_id"]
    assert len(service.sent) == 1  # exactly one message handed to the API


@pytest.mark.live
def test_gmail_live_roundtrip():
    """Live: real profile + self-send via the alerts Gmail account."""
    from app.integrations.gmail.oauth import get_alerts_service

    service = get_alerts_service()
    _gmail_send_roundtrip(service)
