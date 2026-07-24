"""DocuSign isolation test — offline (recorded) by default, live with --run-live.

Consolidates ``scripts/manual/test_docusign.py`` (send + status). Builds an
envelope from the configured template, confirms the returned envelope id and a
``sent`` status, then voids it in teardown. Runs against:

  * a recorded fake envelopes API by default (no credentials), and
  * the real sandbox (refresh-token exchange + demo host) under ``--run-live``.

The inbound Connect webhook / signed-PDF loop is out of scope here (covered by
the E2E gate and Phase 4); this mirrors the manual script's send+status check.
"""
from __future__ import annotations

import base64

import docusign_esign as ds
import pytest
from docusign_esign.client.api_exception import ApiException

from app.integrations.docusign.client import _refresh_access_token, get_envelope_api
from tests.integration.fakes import FakeEnvelopesApi, load_recorded

RECIPIENT = "docusign-isolation-test@example.com"
RECIPIENT_NAME = "ZZ TEST — delete me"


def _docusign_roundtrip(envelopes_api, account_id, *, template_id, signer_role):
    signer = ds.TemplateRole(email=RECIPIENT, name=RECIPIENT_NAME, role_name=signer_role)
    envelope_def = ds.EnvelopeDefinition(
        status="sent",
        template_id=template_id,
        template_roles=[signer],
    )

    created = envelopes_api.create_envelope(account_id, envelope_definition=envelope_def)
    envelope_id = created.envelope_id
    assert envelope_id

    try:
        envelope = envelopes_api.get_envelope(account_id, envelope_id)
        assert envelope.status == "sent"
    finally:
        # Void the envelope so the sandbox account stays clean.
        env = ds.Envelope()
        env.status = "voided"
        env.voided_reason = "isolation test cleanup"
        try:
            envelopes_api.update(account_id, envelope_id, envelope=env)
        except ApiException as exc:
            # Already terminal (400/409) is fine; anything else is a real error.
            if exc.status not in (400, 409):
                raise

    return envelope_id


def test_docusign_offline_roundtrip():
    """Offline: send + status + void against the recorded fake (no creds)."""
    rec = load_recorded("external_responses.json")["docusign"]
    pdf_bytes = base64.b64decode(rec["combined_pdf_b64_prefix"])
    api = FakeEnvelopesApi(rec["envelope_id"], rec["status_after_send"], pdf_bytes)

    envelope_id = _docusign_roundtrip(
        api, "fake-account-id", template_id="test-template-id", signer_role="signer"
    )
    assert envelope_id == rec["envelope_id"]
    assert envelope_id in api.voided  # teardown void reached the API

    # The recorded combined-PDF bytes are a real PDF header.
    assert pdf_bytes.startswith(b"%PDF")


@pytest.mark.live
def test_docusign_live_roundtrip():
    """Live: refresh-token exchange + real send + status against the sandbox."""
    from app.config import load_config

    prop = load_config().properties[0]

    access_token = _refresh_access_token()
    assert access_token and len(access_token) > 100

    envelopes_api, account_id = get_envelope_api()
    _docusign_roundtrip(
        envelopes_api,
        account_id,
        template_id=prop.docusign_template_id,
        signer_role=prop.docusign_signer_role,
    )
