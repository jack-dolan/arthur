"""Wave 0 failing tests for DocuSign envelope task handlers (DOCUSIGN-01, 03, 04, HOA-03).

All imports are inside test functions (project convention). These tests are
intentionally RED — the target modules do not exist yet. Wave 2 will create
app/tasks/handlers/docusign.py and app/integrations/docusign/client.py.

Requirements covered:
  DOCUSIGN-01: Envelope sent from template with guest name/email; 7-day reminder; status='sent'
  DOCUSIGN-03: On completion, PDF retrieved and stored; HOA window check triggered
  DOCUSIGN-04: Void is idempotent on already-complete/voided envelopes
  HOA-03: Immediate HOA trigger if date is in window; skip if too early
"""
# Wave 2 additions: DocuSign client (refresh token + get_envelope_api) tests
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from app.db.models import (
    Booking,
    BookingStatus,
    BookingTask,
    Platform,
    TaskState,
    TaskType,
)


def _make_booking(*, guest_email: str | None = "guest@example.com"):
    return Booking(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id="HMDOC01",
        property_id="test_property",
        guest_first_name="Doc",
        guest_last_name="Usign",
        guest_phone="+15551234567",
        guest_email=guest_email,
        check_in_date=date(2026, 8, 1),
        check_out_date=date(2026, 8, 5),
        status=BookingStatus.ACTIVE,
        source_email_message_id="msg-doc-1",
    )


def _make_task(task_type: TaskType = TaskType.DOCUSIGN_SEND, state: TaskState = TaskState.PENDING):
    return BookingTask(
        id=uuid.uuid4(),
        task_type=task_type,
        state=state,
    )


def _claim_emulator(booking):
    """Async stand-in for app.tasks.claim.claim_task over in-memory tasks.

    Emulates the atomic ``UPDATE booking_tasks SET state=to WHERE id=? AND
    state=expect RETURNING id``: flips the matching task iff it is currently in
    ``expect`` state, returning whether the claim was won. These unit tests mock
    the DB session so the real SQL cannot run; this preserves the state
    semantics the handler control flow depends on. The real atomic serialisation
    is covered by the integration race test against Postgres.
    """

    async def _claim(session, task_id, *, expect, to):
        for t in booking.tasks:
            if t.id == task_id and t.state == expect:
                t.state = to
                return True
        return False

    return _claim


def _make_property_config(*, signer_role: str = "signer", template_id: str = "tmpl-001"):
    prop = MagicMock()
    prop.docusign_signer_role = signer_role
    prop.docusign_template_id = template_id
    prop.hoa.open_days = [1, 2, 3, 4, 5, 6]
    prop.hoa.email_window_days_min = 2
    prop.hoa.email_window_days_max = 7
    return prop


# ---------------------------------------------------------------------------
# handle_docusign_send — envelope creation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_docusign_send_creates_envelope_and_stores_envelope_id():
    """Envelope created; task reaches COMPLETE with external_ref = envelope_id."""
    from app.tasks.handlers.docusign import handle_docusign_send

    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch(
            "app.tasks.handlers.docusign._get_property_config",
            return_value=_make_property_config(),
        ),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.create_envelope.return_value = MagicMock(envelope_id="env-123")
        mock_api.return_value = (mock_envelopes_api, "acct-1")

        await handle_docusign_send(booking, task, session)

    assert task.state == TaskState.COMPLETE
    assert task.external_ref == "env-123"


@pytest.mark.asyncio
async def test_handle_docusign_send_uses_template_role_from_config():
    """TemplateRole is built with role_name == property_config.docusign_signer_role."""
    from app.tasks.handlers.docusign import handle_docusign_send

    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch("app.tasks.handlers.docusign.load_config") as mock_load_config,
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.create_envelope.return_value = MagicMock(envelope_id="env-456")
        mock_api.return_value = (mock_envelopes_api, "acct-1")

        prop = _make_property_config(signer_role="Guest")
        mock_config = MagicMock()
        mock_config.properties = [prop]
        mock_load_config.return_value = mock_config

        # Patch property lookup to return our test prop
        with patch("app.tasks.handlers.docusign._get_property_config", return_value=prop):
            await handle_docusign_send(booking, task, session)

    call_args = mock_envelopes_api.create_envelope.call_args
    # Inspect the envelope_definition passed to create_envelope
    envelope_def = (
        call_args.kwargs.get("envelope_definition")
        or call_args[1].get("envelope_definition")
        or call_args[0][1]
    )
    role_names = [r.role_name for r in envelope_def.template_roles]
    assert "Guest" in role_names


@pytest.mark.asyncio
async def test_handle_docusign_send_sets_7_day_reminder():
    """Notification.reminders.reminder_delay == '7'."""
    from app.tasks.handlers.docusign import handle_docusign_send

    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch(
            "app.tasks.handlers.docusign._get_property_config",
            return_value=_make_property_config(),
        ),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.create_envelope.return_value = MagicMock(envelope_id="env-789")
        mock_api.return_value = (mock_envelopes_api, "acct-1")

        await handle_docusign_send(booking, task, session)

    call_args = mock_envelopes_api.create_envelope.call_args
    envelope_def = (
        call_args.kwargs.get("envelope_definition")
        or call_args[1].get("envelope_definition")
        or call_args[0][1]
    )
    assert envelope_def.notification.reminders.reminder_delay == "7"


@pytest.mark.asyncio
async def test_handle_docusign_send_sets_status_sent_not_created():
    """EnvelopeDefinition.status == 'sent' (triggers immediate send, not draft)."""
    from app.tasks.handlers.docusign import handle_docusign_send

    booking = _make_booking()
    task = _make_task()
    session = AsyncMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch(
            "app.tasks.handlers.docusign._get_property_config",
            return_value=_make_property_config(),
        ),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.create_envelope.return_value = MagicMock(envelope_id="env-abc")
        mock_api.return_value = (mock_envelopes_api, "acct-1")

        await handle_docusign_send(booking, task, session)

    call_args = mock_envelopes_api.create_envelope.call_args
    envelope_def = (
        call_args.kwargs.get("envelope_definition")
        or call_args[1].get("envelope_definition")
        or call_args[0][1]
    )
    assert envelope_def.status == "sent"


# ---------------------------------------------------------------------------
# handle_envelope_completed — PDF retrieval, storage, HOA trigger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_envelope_completed_downloads_pdf_to_booking_id_path():
    """PDF written to /app/data/pdfs/{booking.id}.pdf."""
    from app.tasks.handlers.docusign import handle_envelope_completed

    booking = _make_booking()
    task = _make_task(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.COMPLETE)
    task.external_ref = "env-completed"
    session = AsyncMock()

    m = mock_open()
    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch("builtins.open", m),
        patch("app.tasks.handlers.docusign.hoa_window") as mock_hoa_window,
        patch(
            "app.tasks.handlers.docusign._get_property_config",
            return_value=_make_property_config(),
        ),
        patch("pathlib.Path.mkdir"),
        patch("app.tasks.handlers.docusign.send_hoa_email"),
        patch("app.tasks.handlers.docusign.get_alerts_service"),
        patch("app.tasks.handlers.docusign.load_config"),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.get_document.return_value = b"%PDF-1.4 test"
        mock_api.return_value = (mock_envelopes_api, "acct-1")
        # Put HOA window in the past so the immediate trigger fires
        mock_hoa_window.return_value = (date(2020, 1, 1), date(2099, 12, 31))

        await handle_envelope_completed(booking, task, "env-completed", session)

    # The open() call should target a path with the booking id
    open_calls = m.call_args_list
    assert any(str(booking.id) in str(c) for c in open_calls)


@pytest.mark.asyncio
async def test_handle_envelope_completed_stores_signed_pdf_path_on_booking():
    """booking.signed_pdf_path is set to the new path string after PDF download."""
    from app.tasks.handlers.docusign import handle_envelope_completed

    booking = _make_booking()
    task = _make_task(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.COMPLETE)
    task.external_ref = "env-completed"
    session = AsyncMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch("builtins.open", mock_open()),
        patch("app.tasks.handlers.docusign.hoa_window") as mock_hoa_window,
        patch(
            "app.tasks.handlers.docusign._get_property_config",
            return_value=_make_property_config(),
        ),
        patch("pathlib.Path.mkdir"),
        patch("app.tasks.handlers.docusign.send_hoa_email"),
        patch("app.tasks.handlers.docusign.get_alerts_service"),
        patch("app.tasks.handlers.docusign.load_config"),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.get_document.return_value = b"%PDF-1.4 test"
        mock_api.return_value = (mock_envelopes_api, "acct-1")
        mock_hoa_window.return_value = (date(2020, 1, 1), date(2099, 12, 31))

        await handle_envelope_completed(booking, task, "env-completed", session)

    assert booking.signed_pdf_path is not None
    assert str(booking.id) in booking.signed_pdf_path


@pytest.mark.asyncio
async def test_handle_envelope_completed_triggers_hoa_immediately_if_in_window():
    """HOA-03: If current date is within HOA window, HOA send is called immediately."""
    from app.tasks.handlers.docusign import handle_envelope_completed

    booking = _make_booking()
    booking.tasks.append(_make_task(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING))
    task = _make_task(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.COMPLETE)
    task.external_ref = "env-completed"
    session = AsyncMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch("builtins.open", mock_open()),
        patch("app.tasks.handlers.docusign.hoa_window") as mock_hoa_window,
        patch("app.tasks.handlers.docusign.send_hoa_email") as mock_hoa_send,
        patch("app.tasks.handlers.docusign.claim_task", new=_claim_emulator(booking)),
        patch(
            "app.tasks.handlers.docusign._get_property_config",
            return_value=_make_property_config(),
        ),
        patch("pathlib.Path.mkdir"),
        patch("app.tasks.handlers.docusign.get_alerts_service"),
        patch("app.tasks.handlers.docusign.load_config"),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.get_document.return_value = b"%PDF-1.4 test"
        mock_api.return_value = (mock_envelopes_api, "acct-1")
        # Window brackets today — immediate trigger should fire
        mock_hoa_window.return_value = (date(2020, 1, 1), date(2099, 12, 31))

        await handle_envelope_completed(booking, task, "env-completed", session)

    mock_hoa_send.assert_called_once()


@pytest.mark.asyncio
async def test_handle_envelope_completed_hoa_trigger_skipped_if_too_early():
    """HOA-03: If current date is BEFORE HOA window, HOA send is NOT called."""
    from app.tasks.handlers.docusign import handle_envelope_completed

    booking = _make_booking()
    hoa_task = _make_task(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING)
    booking.tasks.append(hoa_task)
    task = _make_task(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.COMPLETE)
    task.external_ref = "env-completed"
    session = AsyncMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch("builtins.open", mock_open()),
        patch("app.tasks.handlers.docusign.hoa_window") as mock_hoa_window,
        patch("app.tasks.handlers.docusign.send_hoa_email") as mock_hoa_send,
        patch(
            "app.tasks.handlers.docusign._get_property_config",
            return_value=_make_property_config(),
        ),
        patch("pathlib.Path.mkdir"),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.get_document.return_value = b"%PDF-1.4 test"
        mock_api.return_value = (mock_envelopes_api, "acct-1")
        # Window is in the future — should NOT trigger HOA email
        mock_hoa_window.return_value = (date(2099, 1, 1), date(2099, 12, 31))

        await handle_envelope_completed(booking, task, "env-completed", session)

    mock_hoa_send.assert_not_called()
    assert hoa_task.state == TaskState.WAITING


@pytest.mark.asyncio
async def test_handle_envelope_completed_calls_real_hoa_integration_in_window(tmp_path):
    """REGRESSION (go-live Step 2): the webhook-completed in-window path must
    call the REAL app.integrations.hoa.email.send_hoa_email with its real
    signature (pdf_path / property_config / alerts_service / owners_signature_name
    / from_address).

    Critically, this test does NOT patch app.tasks.handlers.docusign.send_hoa_email
    — patching that name is exactly what masked the signature-mismatch TypeError.
    Only the Gmail boundary (get_alerts_service) is mocked. Before the fix the
    local wrapper forwarded hoa_task=/prop= and this raised TypeError; after the
    fix the real integration is invoked and reaches the Gmail send call.
    """
    from app.tasks.handlers.docusign import handle_envelope_completed

    booking = _make_booking()
    hoa_task = _make_task(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING)
    booking.tasks.append(hoa_task)
    task = _make_task(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.COMPLETE)
    task.external_ref = "env-completed"
    session = AsyncMock()

    # Real PDF written to a writable tmp path so the real integration can read it.
    pdf_target = tmp_path / f"{booking.id}.pdf"

    prop = _make_property_config()
    prop.hoa.email = "hoa@example.com"

    mock_config = MagicMock()
    mock_config.owners.signature_name = "Pat Owner"
    mock_config.email.alerts = "alerts@example.com"

    mock_alerts_service = MagicMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch("app.tasks.handlers.docusign._pdf_path_for_booking", return_value=pdf_target),
        patch(
            "app.tasks.handlers.docusign.hoa_window",
            return_value=(date(2020, 1, 1), date(2099, 12, 31)),
        ),
        patch("app.tasks.handlers.docusign._get_property_config", return_value=prop),
        patch("app.tasks.handlers.docusign.load_config", return_value=mock_config),
        patch("app.tasks.handlers.docusign.claim_task", new=_claim_emulator(booking)),
        # get_alerts_service does not exist on the module before the fix; create=True
        # lets the patch install cleanly so the TypeError (not an AttributeError at
        # patch-setup) is what surfaces on the unfixed code.
        patch(
            "app.tasks.handlers.docusign.get_alerts_service",
            return_value=mock_alerts_service,
            create=True,
        ),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.get_document.return_value = b"%PDF-1.4 test"
        mock_api.return_value = (mock_envelopes_api, "acct-1")

        await handle_envelope_completed(booking, task, "env-completed", session)

    # The real integration ran end-to-end and reached the Gmail send boundary.
    mock_alerts_service.users.return_value.messages.return_value.send.assert_called_once()
    assert hoa_task.state == TaskState.COMPLETE


@pytest.mark.asyncio
async def test_handle_envelope_completed_get_document_arg_order():
    """get_document must be called as (account_id, 'combined', envelope_id).

    The SDK signature is get_document(account_id, document_id, envelope_id).
    'combined' is the document_id; the envelope GUID is the third argument.
    Reversing them produces a 400 "Invalid value specified for envelopeId" from
    the live API — discovered during Step 11 sandbox isolation test.
    """
    from app.tasks.handlers.docusign import handle_envelope_completed

    booking = _make_booking()
    task = _make_task(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.COMPLETE)
    task.external_ref = "env-xyz"
    session = AsyncMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch("builtins.open", mock_open()),
        patch(
            "app.tasks.handlers.docusign.hoa_window",
            return_value=(date(2099, 1, 1), date(2099, 12, 31)),
        ),
        patch(
            "app.tasks.handlers.docusign._get_property_config",
            return_value=_make_property_config(),
        ),
        patch("pathlib.Path.mkdir"),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.get_document.return_value = b"%PDF-1.4 test"
        mock_api.return_value = (mock_envelopes_api, "acct-1")

        await handle_envelope_completed(booking, task, "env-xyz", session)

    # document_id ('combined') must come BEFORE the envelope GUID
    mock_envelopes_api.get_document.assert_called_once_with("acct-1", "combined", "env-xyz")


@pytest.mark.asyncio
async def test_handle_envelope_completed_hoa_send_idempotent_on_duplicate(tmp_path):
    """Step 17: a duplicate/replayed DocuSign 'completed' webhook must NOT send the
    HOA email twice. handle_envelope_completed sends only when HOA_EMAIL is still
    WAITING; a second in-window delivery (HOA already COMPLETE) is a send no-op.
    """
    from app.tasks.handlers.docusign import handle_envelope_completed

    booking = _make_booking()
    hoa_task = _make_task(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING)
    booking.tasks.append(hoa_task)
    task = _make_task(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.COMPLETE)
    task.external_ref = "env-dup"
    session = AsyncMock()

    pdf_target = tmp_path / f"{booking.id}.pdf"
    prop = _make_property_config()
    prop.hoa.email = "hoa@example.com"
    mock_config = MagicMock()
    mock_config.owners.signature_name = "Owner"
    mock_config.email.alerts = "alerts@example.com"

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch("app.tasks.handlers.docusign._pdf_path_for_booking", return_value=pdf_target),
        patch(
            "app.tasks.handlers.docusign.hoa_window",
            return_value=(date(2020, 1, 1), date(2099, 12, 31)),
        ),
        patch("app.tasks.handlers.docusign._get_property_config", return_value=prop),
        patch("app.tasks.handlers.docusign.load_config", return_value=mock_config),
        patch(
            "app.tasks.handlers.docusign.get_alerts_service",
            return_value=MagicMock(),
            create=True,
        ),
        patch("app.tasks.handlers.docusign.claim_task", new=_claim_emulator(booking)),
        patch("app.tasks.handlers.docusign.send_hoa_email") as mock_send,
    ):
        mock_env_api = MagicMock()
        mock_env_api.get_document.return_value = b"%PDF-1.4 x"
        mock_api.return_value = (mock_env_api, "acct")

        # First delivery: in-window → send once, HOA → COMPLETE
        await handle_envelope_completed(booking, task, "env-dup", session)
        # Second (duplicate) delivery: HOA already COMPLETE → must NOT resend
        await handle_envelope_completed(booking, task, "env-dup", session)

    assert mock_send.call_count == 1, (
        f"HOA email sent {mock_send.call_count} times on duplicate webhook; expected 1"
    )
    assert hoa_task.state == TaskState.COMPLETE


# ---------------------------------------------------------------------------
# void_envelope_idempotent — DOCUSIGN-04
# ---------------------------------------------------------------------------

def test_void_envelope_idempotent_already_completed_returns_normally():
    """ApiException with status=400 (already in terminal state) is swallowed."""
    from docusign_esign.client.api_exception import ApiException

    from app.tasks.handlers.docusign import void_envelope_idempotent

    with patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api:
        mock_envelopes_api = MagicMock()
        exc = ApiException(status=400, reason="Envelope is already in a completed state.")
        mock_envelopes_api.update.side_effect = exc
        mock_api.return_value = (mock_envelopes_api, "acct-1")

        # Must not raise
        result = void_envelope_idempotent("env-completed")
        assert result is None


def test_void_envelope_idempotent_409_already_voided_returns_normally():
    """ApiException with status=409 (already voided) is swallowed."""
    from docusign_esign.client.api_exception import ApiException

    from app.tasks.handlers.docusign import void_envelope_idempotent

    with patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api:
        mock_envelopes_api = MagicMock()
        exc = ApiException(status=409, reason="Already voided.")
        mock_envelopes_api.update.side_effect = exc
        mock_api.return_value = (mock_envelopes_api, "acct-1")

        result = void_envelope_idempotent("env-already-voided")
        assert result is None


def test_void_envelope_propagates_500():
    """ApiException with status=500 (server error) is re-raised."""
    from docusign_esign.client.api_exception import ApiException

    from app.tasks.handlers.docusign import void_envelope_idempotent

    with patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api:
        mock_envelopes_api = MagicMock()
        exc = ApiException(status=500, reason="Internal server error.")
        mock_envelopes_api.update.side_effect = exc
        mock_api.return_value = (mock_envelopes_api, "acct-1")

        with pytest.raises(ApiException) as exc_info:
            void_envelope_idempotent("env-server-error")
        assert exc_info.value.status == 500


# ---------------------------------------------------------------------------
# DocuSign client — _refresh_access_token and get_envelope_api
# ---------------------------------------------------------------------------

def test_refresh_access_token_posts_to_sandbox_oauth_endpoint_by_default():
    """_refresh_access_token POSTs to sandbox endpoint when docusign_sandbox=True (default)."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "tok-abc", "expires_in": 3600}
    mock_response.raise_for_status = MagicMock()

    with patch("app.integrations.docusign.client.settings") as mock_settings, \
         patch(
             "app.integrations.docusign.client.httpx.post",
             return_value=mock_response,
         ) as mock_post:
        mock_settings.docusign_sandbox = True
        mock_settings.docusign_refresh_token = "rt"
        mock_settings.docusign_client_id = "cid"
        mock_settings.docusign_client_secret = "csec"
        from app.integrations.docusign.client import _refresh_access_token
        _refresh_access_token()

    call_url = mock_post.call_args[0][0]
    assert call_url == "https://account-d.docusign.com/oauth/token"


def test_refresh_access_token_posts_to_production_oauth_endpoint_when_sandbox_false():
    """_refresh_access_token POSTs to production endpoint when docusign_sandbox=False."""

    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "tok-prod", "expires_in": 3600}
    mock_response.raise_for_status = MagicMock()

    with patch("app.integrations.docusign.client.settings") as mock_settings, \
         patch(
             "app.integrations.docusign.client.httpx.post",
             return_value=mock_response,
         ) as mock_post:
        mock_settings.docusign_sandbox = False
        mock_settings.docusign_refresh_token = "rt"
        mock_settings.docusign_client_id = "cid"
        mock_settings.docusign_client_secret = "csec"
        from app.integrations.docusign.client import _refresh_access_token
        _refresh_access_token()

    call_url = mock_post.call_args[0][0]
    assert call_url == "https://account.docusign.com/oauth/token"


def test_refresh_access_token_uses_basic_auth_with_client_id_secret():
    """_refresh_access_token uses HTTP Basic auth with (client_id, client_secret)."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "tok-abc", "expires_in": 3600}
    mock_response.raise_for_status = MagicMock()

    with patch("app.integrations.docusign.client.settings") as mock_settings, \
         patch(
             "app.integrations.docusign.client.httpx.post",
             return_value=mock_response,
         ) as mock_post:
        mock_settings.docusign_sandbox = True
        mock_settings.docusign_refresh_token = "rt"
        mock_settings.docusign_client_id = "cid"
        mock_settings.docusign_client_secret = "csec"
        from app.integrations.docusign.client import _refresh_access_token
        _refresh_access_token()

    call_kwargs = mock_post.call_args.kwargs if mock_post.call_args.kwargs else {}
    call_args = mock_post.call_args
    # Auth may be in kwargs directly or as a positional arg
    auth = call_kwargs.get("auth") or (call_args[1].get("auth") if len(call_args) > 1 else None)
    assert auth == ("cid", "csec")


def test_refresh_access_token_returns_access_token_string():
    """_refresh_access_token returns the access_token string from the JSON response."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "tok-abc", "expires_in": 3600}
    mock_response.raise_for_status = MagicMock()

    with patch("app.integrations.docusign.client.settings") as mock_settings, \
         patch("app.integrations.docusign.client.httpx.post", return_value=mock_response):
        mock_settings.docusign_sandbox = True
        mock_settings.docusign_refresh_token = "rt"
        mock_settings.docusign_client_id = "cid"
        mock_settings.docusign_client_secret = "csec"
        from app.integrations.docusign.client import _refresh_access_token
        result = _refresh_access_token()

    assert result == "tok-abc"


def test_get_envelope_api_returns_tuple_of_api_and_account_id():
    """get_envelope_api returns (EnvelopesApi, settings.docusign_account_id)."""
    import docusign_esign as ds

    from app.integrations.docusign.client import get_envelope_api
    from app.settings import settings

    with patch("app.integrations.docusign.client._refresh_access_token", return_value="tok-abc"):
        result = get_envelope_api()

    assert isinstance(result, tuple)
    assert len(result) == 2
    api, account_id = result
    assert isinstance(api, ds.EnvelopesApi)
    assert account_id == settings.docusign_account_id


@pytest.mark.asyncio
async def test_handle_envelope_completed_skips_unsigned_reminders():
    """Once the guest has signed, any still-pending unsigned-DocuSign reminders
    are moot and must flip PENDING→SKIPPED (dashboard truthfulness)."""
    from app.tasks.handlers.docusign import handle_envelope_completed

    booking = _make_booking()
    rem_7d = _make_task(
        task_type=TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D, state=TaskState.PENDING
    )
    rem_4d = _make_task(
        task_type=TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D, state=TaskState.PENDING
    )
    fired = _make_task(
        task_type=TaskType.OWNER_ALERT_MISSING_EMAIL_7D, state=TaskState.COMPLETE
    )
    booking.tasks.extend([rem_7d, rem_4d, fired])
    task = _make_task(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.COMPLETE)
    task.external_ref = "env-completed"
    session = AsyncMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch("builtins.open", mock_open()),
        patch("app.tasks.handlers.docusign.hoa_window") as mock_hoa_window,
        patch(
            "app.tasks.handlers.docusign._get_property_config",
            return_value=_make_property_config(),
        ),
        patch("pathlib.Path.mkdir"),
        patch("app.tasks.handlers.docusign.send_hoa_email"),
        patch("app.tasks.handlers.docusign.get_alerts_service"),
        patch("app.tasks.handlers.docusign.load_config"),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.get_document.return_value = b"%PDF-1.4 test"
        mock_api.return_value = (mock_envelopes_api, "acct-1")
        # Window in the future — reminder skipping must not depend on the HOA branch
        mock_hoa_window.return_value = (date(2099, 1, 1), date(2099, 12, 31))

        await handle_envelope_completed(booking, task, "env-completed", session)

    assert rem_7d.state == TaskState.SKIPPED
    assert rem_4d.state == TaskState.SKIPPED
    # A reminder that actually fired stays COMPLETE — history is never rewritten.
    assert fired.state == TaskState.COMPLETE



@pytest.mark.asyncio
async def test_handle_envelope_completed_sends_hoa_late_when_window_passed():
    """A guest signing after the last acceptable send day still triggers the
    immediate HOA send (owner decision 2026-07-22, bug hunt F3): `latest` is a
    scheduling deadline, not a send-blocker. Previously the webhook left the
    task WAITING forever and the HOA never received the form."""
    from app.tasks.handlers.docusign import handle_envelope_completed

    booking = _make_booking()
    hoa_task = _make_task(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING)
    booking.tasks.append(hoa_task)
    task = _make_task(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.COMPLETE)
    task.external_ref = "env-completed"
    session = AsyncMock()

    with (
        patch("app.tasks.handlers.docusign.get_envelope_api") as mock_api,
        patch("builtins.open", mock_open()),
        patch("app.tasks.handlers.docusign.hoa_window") as mock_hoa_window,
        patch("app.tasks.handlers.docusign.send_hoa_email") as mock_hoa_send,
        patch("app.tasks.handlers.docusign.claim_task", new=_claim_emulator(booking)),
        patch(
            "app.tasks.handlers.docusign._get_property_config",
            return_value=_make_property_config(),
        ),
        patch("pathlib.Path.mkdir"),
        patch("app.tasks.handlers.docusign.get_alerts_service"),
        patch("app.tasks.handlers.docusign.load_config"),
    ):
        mock_envelopes_api = MagicMock()
        mock_envelopes_api.get_document.return_value = b"%PDF-1.4 test"
        mock_api.return_value = (mock_envelopes_api, "acct-1")
        # Window entirely in the past — the send must STILL fire (late).
        mock_hoa_window.return_value = (date(2020, 1, 1), date(2020, 12, 31))

        await handle_envelope_completed(booking, task, "env-completed", session)

    mock_hoa_send.assert_called_once()
    assert hoa_task.state == TaskState.COMPLETE
