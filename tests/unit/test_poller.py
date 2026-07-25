"""Unit tests for the Gmail booking-feed poller.

The poller's job:
  1. List unprocessed messages from the booking feed inbox via Gmail API.
  2. Fetch each message, classify it.
  3. For booking emails: parse + persist a Booking row, initial BookingTask rows,
     and DataPoint provenance rows.
  4. For cancellation emails: call the cancellation handler (stubbed in these tests).
  5. Skip messages that are already in the DB (idempotency via source_email_message_id).
  6. Skip unrecognised (OTHER) messages silently.

Gmail API calls are mocked throughout — no network required.
"""
from __future__ import annotations

import base64
import email as email_lib
from datetime import date
from email import policy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import recorded_email_bytes


def _eml_to_gmail_payload(filename: str) -> dict:
    """Encode a .eml fixture as the raw payload Gmail API returns."""
    raw = recorded_email_bytes(filename)
    encoded = base64.urlsafe_b64encode(raw).decode()
    return {"id": f"msg-{filename}", "raw": encoded}


# ---------------------------------------------------------------------------
# fetch_unprocessed_message_ids
# ---------------------------------------------------------------------------

def test_fetch_returns_empty_when_inbox_empty():
    from app.ingestion.poller import fetch_unprocessed_message_ids
    service = MagicMock()
    service.users().messages().list().execute.return_value = {}
    result = fetch_unprocessed_message_ids(service, already_seen=set())
    assert result == []


def test_fetch_returns_new_message_ids():
    from app.ingestion.poller import fetch_unprocessed_message_ids
    service = MagicMock()
    service.users().messages().list().execute.return_value = {
        "messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]
    }
    result = fetch_unprocessed_message_ids(service, already_seen={"m1"})
    assert set(result) == {"m2", "m3"}


def test_fetch_excludes_all_already_seen():
    from app.ingestion.poller import fetch_unprocessed_message_ids
    service = MagicMock()
    service.users().messages().list().execute.return_value = {
        "messages": [{"id": "m1"}, {"id": "m2"}]
    }
    result = fetch_unprocessed_message_ids(service, already_seen={"m1", "m2"})
    assert result == []


# ---------------------------------------------------------------------------
# fetch_raw_message
# ---------------------------------------------------------------------------

def test_fetch_raw_message_calls_api_correctly():
    from app.ingestion.poller import fetch_raw_message
    service = MagicMock()
    service.users().messages().get().execute.return_value = {"id": "m1", "raw": "abc"}
    result = fetch_raw_message(service, "m1")
    assert result == {"id": "m1", "raw": "abc"}
    service.users().messages().get.assert_called_with(userId="me", id="m1", format="raw")


# ---------------------------------------------------------------------------
# raw_payload_to_message
# ---------------------------------------------------------------------------

def test_raw_payload_to_message_decodes_eml():
    from app.ingestion.poller import raw_payload_to_message
    payload = _eml_to_gmail_payload("airbnbbooking-1.eml")
    msg = raw_payload_to_message(payload)
    assert msg.get("From", "").lower().find("airbnb") != -1


# ---------------------------------------------------------------------------
# process_message — Airbnb booking path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_airbnb_booking_creates_booking_row():
    """process_message with an Airbnb booking should persist a Booking."""
    from app.ingestion.poller import process_message

    payload = _eml_to_gmail_payload("airbnbbooking-1.eml")

    persisted = {}

    async def fake_persist_booking(msg_id, msg, parsed, platform):
        persisted["called"] = True
        persisted["msg_id"] = msg_id
        persisted["platform"] = platform

    with patch("app.ingestion.poller.persist_booking", side_effect=fake_persist_booking):
        await process_message(payload["id"], payload)

    assert persisted.get("called") is True
    assert persisted["platform"].value == "airbnb"
    assert persisted["msg_id"] == payload["id"]


@pytest.mark.asyncio
async def test_process_vrbo_booking_creates_booking_row():
    from app.ingestion.poller import process_message

    payload = _eml_to_gmail_payload("vrbo-booking-1.eml")
    persisted = {}

    async def fake_persist_booking(msg_id, msg, parsed, platform):
        persisted["platform"] = platform

    with patch("app.ingestion.poller.persist_booking", side_effect=fake_persist_booking):
        await process_message(payload["id"], payload)

    assert persisted["platform"].value == "vrbo"


@pytest.mark.asyncio
async def test_process_other_email_is_dead_lettered_not_persisted():
    """An unrecognised (OTHER) email must not persist a booking, but IS recorded
    as processed so the poller does not re-fetch it every poll (Step 19 / R3b).
    """
    from unittest.mock import AsyncMock

    from app.ingestion.poller import process_message

    raw = (
        "From: noreply@example.com\r\nSubject: Newsletter\r\n"
        "Content-Type: text/plain\r\n\r\nHello world"
    )
    encoded = base64.urlsafe_b64encode(raw.encode()).decode()
    payload = {"id": "noise-1", "raw": encoded}

    with (
        patch("app.ingestion.poller.persist_booking") as mock_persist,
        patch(
            "app.ingestion.poller._record_processed_message", new_callable=AsyncMock
        ) as mock_record,
    ):
        await process_message(payload["id"], payload)

    mock_persist.assert_not_called()
    # sender/subject are recorded so the weekly classifier-drift digest can
    # surface platform-domain OTHER dead-letters for human review
    # (sustainability audit 2026-07-23, item 4).
    mock_record.assert_awaited_once_with(
        "noise-1", "other", sender="noreply@example.com", subject="Newsletter"
    )


@pytest.mark.asyncio
async def test_process_airbnb_cancellation_calls_cancellation_handler():
    from app.ingestion.poller import process_message

    payload = _eml_to_gmail_payload("airbnb-cancellation-1.eml")
    handled = {}

    async def fake_handle_cancellation(msg_id, msg, platform):
        handled["platform"] = platform
        handled["msg_id"] = msg_id

    with patch("app.ingestion.poller.handle_cancellation", side_effect=fake_handle_cancellation):
        await process_message(payload["id"], payload)

    assert handled.get("platform").value == "airbnb"


@pytest.mark.asyncio
async def test_process_vrbo_cancellation_calls_cancellation_handler():
    from app.ingestion.poller import process_message

    payload = _eml_to_gmail_payload("vrbo-cancellation-1.eml")
    handled = {}

    async def fake_handle_cancellation(msg_id, msg, platform):
        handled["platform"] = platform

    with patch("app.ingestion.poller.handle_cancellation", side_effect=fake_handle_cancellation):
        await process_message(payload["id"], payload)

    assert handled.get("platform").value == "vrbo"


# ---------------------------------------------------------------------------
# persist_booking (logic-level, with fake DB session)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_booking_writes_booking_and_data_points():
    """persist_booking should add() a Booking with data_points attached."""
    from app.db.models import Platform
    from app.ingestion.parsers.airbnb import AirbnbBookingData
    from app.ingestion.poller import persist_booking

    parsed = AirbnbBookingData(
        confirmation_code="HMTEST01",
        guest_first_name="Test",
        guest_last_name="Guest",
        check_in_date=date(2026, 8, 1),
        check_out_date=date(2026, 8, 5),
    )

    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock(  # duplicate pre-check finds nothing
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    msg = email_lib.message_from_string(
        "From: Airbnb <automated@airbnb.com>\r\nDate: Mon, 1 Jun 2026 00:00:00 +0000\r\n\r\n",
        policy=policy.default,
    )

    with patch("app.ingestion.poller.AsyncSessionLocal", return_value=_async_ctx(session)):
        with patch("app.integrations.gmail.oauth.get_alerts_service"):
            await persist_booking("msg-test-1", msg, parsed, Platform.AIRBNB)

    session.add.assert_called_once()
    booking_arg = session.add.call_args[0][0]
    from app.db.models import Booking
    assert isinstance(booking_arg, Booking)
    assert booking_arg.external_id == "HMTEST01"
    assert booking_arg.source_email_message_id == "msg-test-1"
    assert booking_arg.platform == Platform.AIRBNB
    assert len(booking_arg.data_points) > 0
    # Every task relevant to an Airbnb booking should be initialised.
    task_types = {t.task_type.value for t in booking_arg.tasks}
    assert "owner_alert_new_booking" in task_types
    assert "docusign_send" in task_types
    assert "cleaner_sheet_add" in task_types


def _async_ctx(obj):
    """Return an async context manager that yields *obj*."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=obj)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _persist_args():
    """(parsed, msg) pair for the persist_booking alert-tracking tests."""
    from app.ingestion.parsers.airbnb import AirbnbBookingData

    parsed = AirbnbBookingData(
        confirmation_code="HMTEST02",
        guest_first_name="Test",
        guest_last_name="Guest",
        check_in_date=date(2026, 8, 1),
        check_out_date=date(2026, 8, 5),
    )
    msg = email_lib.message_from_string(
        "From: Airbnb <automated@airbnb.com>\r\nDate: Mon, 1 Jun 2026 00:00:00 +0000\r\n\r\n",
        policy=policy.default,
    )
    return parsed, msg


async def test_persist_booking_completes_alert_task_on_send_success():
    """R7 fix: after the new-booking alert email sends, the OWNER_ALERT_NEW_BOOKING
    task row must flip to COMPLETE (it used to stay PENDING forever)."""
    from app.db.models import Platform, TaskState, TaskType
    from app.ingestion.poller import persist_booking

    parsed, msg = _persist_args()
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(  # duplicate pre-check finds nothing
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    with (
        patch("app.ingestion.poller.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch("app.integrations.gmail.oauth.get_alerts_service"),
        patch("app.ingestion.alerts.send_new_booking_alert") as mock_send,
        patch("app.config.load_config") as mock_cfg,
    ):
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        mock_cfg.return_value.properties = [MagicMock(id="property_1")]
        await persist_booking("msg-alert-ok", msg, parsed, Platform.AIRBNB)

    mock_send.assert_called_once()
    booking_arg = session.add.call_args[0][0]
    alert_task = next(
        t for t in booking_arg.tasks if t.task_type == TaskType.OWNER_ALERT_NEW_BOOKING
    )
    assert alert_task.state == TaskState.COMPLETE
    assert alert_task.completed_at is not None
    # The flip must be persisted: one commit for the booking, one for the flip.
    assert session.commit.await_count >= 2


async def test_persist_booking_records_alert_failure_and_stays_pending():
    """A failed alert send is non-fatal, leaves the task PENDING (visible gap)
    and records the error on the task row."""
    from app.db.models import Platform, TaskState, TaskType
    from app.ingestion.poller import persist_booking

    parsed, msg = _persist_args()
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(  # duplicate pre-check finds nothing
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    with (
        patch("app.ingestion.poller.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch("app.integrations.gmail.oauth.get_alerts_service"),
        patch(
            "app.ingestion.alerts.send_new_booking_alert",
            side_effect=RuntimeError("gmail boom"),
        ),
        patch("app.config.load_config") as mock_cfg,
    ):
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        mock_cfg.return_value.properties = [MagicMock(id="property_1")]
        # Must not raise — the alert is a non-fatal side effect.
        await persist_booking("msg-alert-fail", msg, parsed, Platform.AIRBNB)

    booking_arg = session.add.call_args[0][0]
    alert_task = next(
        t for t in booking_arg.tasks if t.task_type == TaskType.OWNER_ALERT_NEW_BOOKING
    )
    assert alert_task.state == TaskState.PENDING
    assert alert_task.last_error is not None
    assert "gmail boom" in alert_task.last_error


# ---------------------------------------------------------------------------
# F1 (bug hunt 2026-07-22): poller infinite-replay fixes.
# Every message must reach a terminal outcome (booking row or dead-letter);
# these paths used to escape both and be re-fetched every 5 minutes forever.
# ---------------------------------------------------------------------------


def _inline_payload(msg_id: str, raw: str) -> dict:
    encoded = base64.urlsafe_b64encode(raw.encode()).decode()
    return {"id": msg_id, "raw": encoded}


@pytest.mark.asyncio
async def test_process_cancellation_without_external_id_dead_letters_and_alerts():
    """F1a: a cancellation whose external id cannot be extracted can never be
    applied by retrying — dead-letter it AND alert the owner (the booking stays
    ACTIVE in the system, so every cleanup is now manual)."""
    from app.ingestion.poller import process_message

    payload = _inline_payload(
        "m-cancel-noid",
        "From: Airbnb <automated@airbnb.com>\r\n"
        "Subject: Your reservation is canceled\r\n"
        "Content-Type: text/plain\r\n\r\nJane canceled the trip.\r\n",
    )

    with (
        patch("app.ingestion.poller.handle_cancellation") as mock_handle,
        patch(
            "app.ingestion.poller._record_processed_message", new_callable=AsyncMock
        ) as mock_record,
        patch("app.ingestion.poller.get_alerts_service"),
        patch(
            "app.ingestion.poller.send_cancellation_parse_failure_alert"
        ) as mock_alert,
        patch("app.config.load_config") as mock_cfg,
    ):
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        await process_message(payload["id"], payload)

    mock_handle.assert_not_called()
    mock_record.assert_awaited_once()
    args, kwargs = mock_record.await_args
    assert args[0] == "m-cancel-noid"
    assert args[1] == "cancellation_parse_error"
    assert kwargs.get("classified_as") == "airbnb_cancellation"
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_process_alteration_email_dead_letters_and_alerts():
    """F9: an alteration email must not fall into the silent OTHER path — the
    booking's original (now possibly stale) dates keep driving the door code,
    HOA window and cleaner row, so this has to be loud."""
    from app.ingestion.poller import process_message

    payload = _inline_payload(
        "m-alteration",
        "From: Airbnb <automated@airbnb.com>\r\n"
        "Subject: Reservation updated: Jane's trip dates changed\r\n"
        "Content-Type: text/plain\r\n\r\nDetails changed.\r\n",
    )

    with (
        patch(
            "app.ingestion.poller._record_processed_message", new_callable=AsyncMock
        ) as mock_record,
        patch("app.ingestion.poller.get_alerts_service"),
        patch("app.ingestion.poller.send_booking_alteration_alert") as mock_alert,
        patch("app.config.load_config") as mock_cfg,
    ):
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        await process_message(payload["id"], payload)

    mock_record.assert_awaited_once()
    args, kwargs = mock_record.await_args
    assert args[0] == "m-alteration"
    assert args[1] == "alteration"
    assert kwargs.get("classified_as") == "airbnb_alteration"
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_process_message_classify_error_dead_letters_and_alerts():
    """F1d: an exception while decoding/classifying a message must dead-letter
    it (and alert — it might have been a booking) instead of replaying forever."""
    from app.ingestion.poller import process_message

    payload = _inline_payload(
        "m-classify-boom",
        "From: x@example.com\r\nSubject: whatever\r\n"
        "Content-Type: text/plain\r\n\r\nbody\r\n",
    )

    with (
        patch("app.ingestion.poller.classify", side_effect=RuntimeError("boom")),
        patch(
            "app.ingestion.poller._record_processed_message", new_callable=AsyncMock
        ) as mock_record,
        patch("app.ingestion.poller.get_alerts_service"),
        patch("app.ingestion.poller.send_unparseable_email_alert") as mock_alert,
        patch("app.config.load_config") as mock_cfg,
    ):
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        await process_message(payload["id"], payload)  # must not raise

    mock_record.assert_awaited_once()
    args, _kwargs = mock_record.await_args
    assert args[0] == "m-classify-boom"
    assert args[1] == "classify_error"
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_persist_booking_duplicate_external_id_is_dead_lettered_not_raised():
    """F1c: a second confirmation email for an existing (platform, external_id)
    must dead-letter instead of raising IntegrityError every poll forever."""
    import uuid as uuid_lib

    from app.db.models import Platform
    from app.ingestion.poller import persist_booking

    parsed, msg = _persist_args()
    session = AsyncMock()
    session.add = MagicMock()
    # The existing-booking pre-check finds a row.
    session.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=uuid_lib.uuid4())
        )
    )

    with (
        patch("app.ingestion.poller.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch(
            "app.ingestion.poller._record_processed_message", new_callable=AsyncMock
        ) as mock_record,
        # Explicitly dead-end the alert path: this test must be incapable of a
        # live send even without the conftest guard (2026-07-22 incident).
        patch("app.integrations.gmail.oauth.get_alerts_service") as mock_alerts,
        patch("app.ingestion.alerts.send_new_booking_alert") as mock_send_alert,
    ):
        await persist_booking("msg-dup-ext", msg, parsed, Platform.AIRBNB)

    mock_send_alert.assert_not_called()  # duplicate path must not re-alert
    mock_alerts.assert_not_called()

    session.add.assert_not_called()
    mock_record.assert_awaited_once()
    args, _ = mock_record.await_args
    assert args[0] == "msg-dup-ext"
    assert args[1] == "duplicate"


@pytest.mark.asyncio
async def test_persist_booking_integrity_error_on_commit_is_dead_lettered():
    """F1c backstop: if two polls race past the pre-check, the UNIQUE-violation
    commit failure is caught and dead-lettered, not raised."""
    from sqlalchemy.exc import IntegrityError

    from app.db.models import Platform
    from app.ingestion.poller import persist_booking

    parsed, msg = _persist_args()
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )
    session.commit = AsyncMock(
        side_effect=IntegrityError("stmt", {}, Exception("duplicate key"))
    )

    with (
        patch("app.ingestion.poller.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch(
            "app.ingestion.poller._record_processed_message", new_callable=AsyncMock
        ) as mock_record,
        # Explicitly dead-end the alert path (2026-07-22 incident).
        patch("app.integrations.gmail.oauth.get_alerts_service"),
        patch("app.ingestion.alerts.send_new_booking_alert") as mock_send_alert,
    ):
        await persist_booking("msg-race-dup", msg, parsed, Platform.AIRBNB)  # no raise

    mock_send_alert.assert_not_called()  # failed insert must not alert success

    session.rollback.assert_awaited()
    mock_record.assert_awaited_once()
    args, _ = mock_record.await_args
    assert args[1] == "duplicate"


@pytest.mark.asyncio
async def test_process_booking_with_implausible_dates_dead_letters_and_alerts():
    """F6/F7 backstop: a parsed booking whose dates are impossible (check-in
    far in the past / >2 years out / check-out before check-in) is a parser
    bug or format drift — treat it exactly like a parse failure (dead-letter +
    owner alert) instead of silently persisting garbage that drives a real
    lock window."""
    from datetime import date, timedelta

    from app.ingestion.parsers.airbnb import AirbnbBookingData
    from app.ingestion.poller import process_message

    payload = _eml_to_gmail_payload("airbnbbooking-1.eml")

    bad = AirbnbBookingData(
        confirmation_code="HMPASTDATE",
        guest_first_name="Past",
        guest_last_name="Guest",
        check_in_date=date.today() - timedelta(days=400),
        check_out_date=date.today() - timedelta(days=396),
    )

    with (
        patch("app.ingestion.poller.parse_airbnb_booking", return_value=bad),
        patch("app.ingestion.poller.persist_booking") as mock_persist,
        patch(
            "app.ingestion.poller._record_processed_message", new_callable=AsyncMock
        ) as mock_record,
        patch("app.ingestion.poller.get_alerts_service"),
        patch("app.ingestion.poller.send_unparseable_email_alert") as mock_alert,
        patch("app.config.load_config") as mock_cfg,
    ):
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        await process_message(payload["id"], payload)

    mock_persist.assert_not_called()
    mock_record.assert_awaited_once()
    args, _ = mock_record.await_args
    assert args[1] == "parse_error"
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_process_booking_with_inverted_dates_dead_letters():
    """check_out <= check_in can never be a real stay."""
    from datetime import date, timedelta

    from app.ingestion.parsers.vrbo import VrboBookingData
    from app.ingestion.poller import process_message

    payload = _eml_to_gmail_payload("vrbo-booking-1.eml")

    bad = VrboBookingData(
        reservation_id="HA-INVERT1",
        guest_first_name="Invert",
        guest_last_name="Guest",
        guest_phone="+15551234567",
        check_in_date=date.today() + timedelta(days=40),
        check_out_date=date.today() + timedelta(days=38),
    )

    with (
        patch("app.ingestion.poller.parse_vrbo_booking", return_value=bad),
        patch("app.ingestion.poller.persist_booking") as mock_persist,
        patch(
            "app.ingestion.poller._record_processed_message", new_callable=AsyncMock
        ) as mock_record,
        patch("app.ingestion.poller.get_alerts_service"),
        patch("app.ingestion.poller.send_unparseable_email_alert") as mock_alert,
        patch("app.config.load_config") as mock_cfg,
    ):
        mock_cfg.return_value.email.alerts = "alerts@example.com"
        await process_message(payload["id"], payload)

    mock_persist.assert_not_called()
    mock_record.assert_awaited_once()
    assert mock_record.await_args[0][1] == "parse_error"
    mock_alert.assert_called_once()


def test_gmail_services_built_with_socket_timeout():
    """F11: httplib2's default timeout is None — one hung Gmail socket would
    wedge its worker thread forever. The service must be built over an HTTP
    transport with an explicit timeout."""
    from app.integrations.gmail import oauth

    with patch("app.integrations.gmail.oauth.build") as mock_build:
        oauth.get_alerts_service()
        oauth.get_booking_feed_service()

    for call in mock_build.call_args_list:
        http = call.kwargs.get("http")
        assert http is not None, "service must be built with an explicit http"
        assert http.http.timeout == 30


def test_sheets_service_built_with_socket_timeout():
    from app.integrations.sheets import client as sheets_client

    with patch("app.integrations.sheets.client.build") as mock_build:
        sheets_client.get_sheets_service()

    http = mock_build.call_args.kwargs.get("http")
    assert http is not None
    assert http.http.timeout == 30


def test_fetch_unprocessed_message_ids_paginates():
    """F16 (bug hunt 2026-07-22): one default page (100 ids, newest first)
    silently orphaned any backlog beyond it — a multi-day outage on a noisy
    inbox lost the oldest messages forever."""
    from app.ingestion.poller import fetch_unprocessed_message_ids

    service = MagicMock()
    list_mock = service.users.return_value.messages.return_value.list
    list_mock.return_value.execute.side_effect = [
        {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "tok-2"},
        {"messages": [{"id": "m3"}]},
    ]

    result = fetch_unprocessed_message_ids(service, already_seen={"m2"})

    assert set(result) == {"m1", "m3"}
    # Second page requested with the token from the first.
    tokens = [c.kwargs.get("pageToken") for c in list_mock.call_args_list]
    assert tokens == [None, "tok-2"]


# ---------------------------------------------------------------------------
# Success heartbeat (sustainability audit 2026-07-23, item 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_poll_success_pings_heartbeat_when_no_new_messages():
    """A completed poll cycle (even with nothing new) pings the external
    dead-man's-switch heartbeat. Silence at the monitor == the poller is dead."""
    from app.ingestion.poller import poll_booking_feed

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[[], []])

    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            return_value=MagicMock(),
        ),
        patch("app.ingestion.poller.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch("app.ingestion.poller.fetch_unprocessed_message_ids", return_value=[]),
        patch(
            "app.ingestion.poller.ping_heartbeat_async", new_callable=AsyncMock
        ) as mock_ping,
    ):
        await poll_booking_feed()

    assert mock_ping.await_count == 1
    assert mock_ping.await_args.kwargs.get("label") == "poller"


@pytest.mark.asyncio
async def test_poll_success_pings_heartbeat_after_processing_messages():
    from app.ingestion.poller import poll_booking_feed

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[[], []])

    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            return_value=MagicMock(),
        ),
        patch("app.ingestion.poller.AsyncSessionLocal", return_value=_async_ctx(session)),
        patch("app.ingestion.poller.fetch_unprocessed_message_ids", return_value=["m1"]),
        patch("app.ingestion.poller.fetch_raw_message", return_value={"id": "m1", "raw": ""}),
        patch(
            "app.ingestion.poller.process_message", new_callable=AsyncMock
        ),
        patch(
            "app.ingestion.poller.ping_heartbeat_async", new_callable=AsyncMock
        ) as mock_ping,
    ):
        await poll_booking_feed()

    assert mock_ping.await_count == 1


@pytest.mark.asyncio
async def test_poll_gmail_auth_failure_does_not_ping_heartbeat():
    """The known silent-death mode: Gmail auth fails, poller logs and returns.
    The heartbeat must NOT be pinged — its absence is what alerts the owner."""
    from app.ingestion.poller import poll_booking_feed

    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            side_effect=RuntimeError("invalid_grant"),
        ),
        patch(
            "app.ingestion.poller.ping_heartbeat_async", new_callable=AsyncMock
        ) as mock_ping,
    ):
        await poll_booking_feed()

    mock_ping.assert_not_awaited()
