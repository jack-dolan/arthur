"""Tests for HOA email sender (HOA-02) and HOA task handler (HOA-03).

Wave 0 tests cover app/integrations/hoa/email.py (greeting + send).
Task 3 tests (added in plan 04-06 Task 3) cover app/tasks/handlers/hoa.py
(immediate-trigger path: in-window sends, out-of-window skips, missing PDF).

Requirements covered:
  HOA-02: HOA email via Gmail API with Eastern time-of-day greeting, signed PDF attached,
          sent to property_config.hoa.email with guest name in subject.
  HOA-03: Handler triggers immediately when date is in window; skips (WAITING)
          when too early (D-09 — Phase 4 scope only).

Time-of-day greeting thresholds (D-10, US/Eastern):
  morning   = 00:00–11:59 ET
  afternoon = 12:00–16:59 ET
  evening   = 17:00–23:59 ET
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch
from zoneinfo import ZoneInfo

import pytest

from app.config import load_config
from app.db.models import Booking, BookingStatus, BookingTask, Platform, TaskState, TaskType

ET = ZoneInfo("America/New_York")

FIXTURES = Path(__file__).parent.parent / "fixtures"
_CONFIG = load_config(FIXTURES / "config.test.yaml")

# The HOA recipient, alerts sender, and sign-off/display name are all
# config-supplied, so these tests derive them from the fixture config rather
# than hardcoding the operator's real values.
HOA_ADDRESS = _CONFIG.properties[0].hoa.email
ALERTS_ADDRESS = _CONFIG.email.alerts
SIGNATURE_NAME = _CONFIG.owners.signature_name


def _make_booking():
    return Booking(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id="HMHOA01",
        property_id="test_property",
        guest_first_name="Hoa",
        guest_last_name="Testguest",
        check_in_date=date(2026, 8, 1),
        check_out_date=date(2026, 8, 5),
        status=BookingStatus.ACTIVE,
        source_email_message_id="msg-hoa-1",
    )


def _make_property_config():
    """Minimal property config duck-type for HOA email tests."""
    prop = MagicMock()
    prop.hoa.email = HOA_ADDRESS
    prop.hoa.open_days = [1, 2, 3, 4, 5, 6]
    prop.hoa.email_window_days_min = 2
    prop.hoa.email_window_days_max = 7
    prop.id = "test_property"
    return prop


def _make_hoa_task(state: TaskState = TaskState.WAITING) -> BookingTask:
    """Minimal BookingTask for HOA_EMAIL tests."""
    task = BookingTask(
        id=uuid.uuid4(),
        booking_id=uuid.uuid4(),
        task_type=TaskType.HOA_EMAIL,
        state=state,
    )
    return task


def _claim_emulator(task):
    """Async stand-in for app.tasks.claim.claim_task over a single in-memory task.

    Emulates the atomic ``UPDATE ... WHERE id=? AND state=expect RETURNING id``:
    flips the task iff it is currently in ``expect`` state. The DB session is
    mocked here; the real atomic serialisation is covered by the integration
    race test against Postgres.
    """

    async def _claim(session, task_id, *, expect, to):
        if task.id == task_id and task.state == expect:
            task.state = to
            return True
        return False

    return _claim


# ---------------------------------------------------------------------------
# _time_of_day_greeting — boundary value tests
# ---------------------------------------------------------------------------

def test_time_of_day_greeting_morning_at_midnight():
    """00:00 ET → 'Good morning'."""
    from app.integrations.hoa.email import _time_of_day_greeting

    now = datetime(2026, 8, 1, 0, 0, 0, tzinfo=ET)
    assert _time_of_day_greeting(now) == "Good morning"


def test_time_of_day_greeting_morning_at_1159():
    """11:59 ET → 'Good morning'."""
    from app.integrations.hoa.email import _time_of_day_greeting

    now = datetime(2026, 8, 1, 11, 59, 0, tzinfo=ET)
    assert _time_of_day_greeting(now) == "Good morning"


def test_time_of_day_greeting_afternoon_at_noon():
    """12:00 ET → 'Good afternoon'."""
    from app.integrations.hoa.email import _time_of_day_greeting

    now = datetime(2026, 8, 1, 12, 0, 0, tzinfo=ET)
    assert _time_of_day_greeting(now) == "Good afternoon"


def test_time_of_day_greeting_afternoon_at_1659():
    """16:59 ET → 'Good afternoon'."""
    from app.integrations.hoa.email import _time_of_day_greeting

    now = datetime(2026, 8, 1, 16, 59, 0, tzinfo=ET)
    assert _time_of_day_greeting(now) == "Good afternoon"


def test_time_of_day_greeting_evening_at_1700():
    """17:00 ET → 'Good evening'."""
    from app.integrations.hoa.email import _time_of_day_greeting

    now = datetime(2026, 8, 1, 17, 0, 0, tzinfo=ET)
    assert _time_of_day_greeting(now) == "Good evening"


def test_time_of_day_greeting_evening_at_2359():
    """23:59 ET → 'Good evening'."""
    from app.integrations.hoa.email import _time_of_day_greeting

    now = datetime(2026, 8, 1, 23, 59, 0, tzinfo=ET)
    assert _time_of_day_greeting(now) == "Good evening"


# ---------------------------------------------------------------------------
# send_hoa_email — integration with Gmail API mock
# ---------------------------------------------------------------------------

def test_send_hoa_email_attaches_pdf_from_signed_pdf_path():
    """PDF bytes read from signed_pdf_path are attached as MIMEApplication with subtype pdf."""
    from app.integrations.hoa.email import send_hoa_email

    booking = _make_booking()
    booking.signed_pdf_path = "/app/data/pdfs/test-booking.pdf"
    property_config = _make_property_config()
    alerts_service = MagicMock()
    pdf_bytes = b"%PDF-1.4 fake pdf content"

    captured_parts = []

    def fake_attach(part):
        captured_parts.append(part)

    with patch("builtins.open", mock_open(read_data=pdf_bytes)):
        with patch("app.integrations.hoa.email.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 1, 10, 0, 0, tzinfo=ET)
            send_hoa_email(
                booking,
                pdf_path=booking.signed_pdf_path,
                property_config=property_config,
                alerts_service=alerts_service,
            )

    # Gmail send must have been called
    alerts_service.users().messages().send.assert_called_once()
    send_call = alerts_service.users().messages().send.call_args
    body_arg = send_call.kwargs.get("body") or send_call[1].get("body") or send_call[0][0]
    # The raw field must be present (base64-encoded email)
    assert "raw" in body_arg


def test_send_hoa_email_to_property_hoa_email():
    """The 'To' header of the sent email equals property_config.hoa.email."""
    import base64
    from email import message_from_bytes

    from app.integrations.hoa.email import send_hoa_email

    booking = _make_booking()
    booking.signed_pdf_path = "/app/data/pdfs/test-booking.pdf"
    property_config = _make_property_config()
    property_config.hoa.email = HOA_ADDRESS
    alerts_service = MagicMock()
    pdf_bytes = b"%PDF-1.4 fake pdf content"

    with patch("builtins.open", mock_open(read_data=pdf_bytes)):
        with patch("app.integrations.hoa.email.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 1, 10, 0, 0, tzinfo=ET)
            send_hoa_email(
                booking,
                pdf_path=booking.signed_pdf_path,
                property_config=property_config,
                alerts_service=alerts_service,
            )

    send_call = alerts_service.users().messages().send.call_args
    body_arg = send_call.kwargs.get("body") or send_call[1].get("body") or send_call[0][0]
    raw_bytes = base64.urlsafe_b64decode(body_arg["raw"] + "==")
    parsed = message_from_bytes(raw_bytes)
    assert parsed["To"] == HOA_ADDRESS


def test_send_hoa_email_subject_is_registration_form_with_date():
    """Subject is 'Guest Registration Form - Arriving M/D/YY' (check-in date); no guest name."""
    import base64
    from email import message_from_bytes

    from app.integrations.hoa.email import send_hoa_email

    booking = _make_booking()  # check_in_date = 2026-08-01
    booking.signed_pdf_path = "/app/data/pdfs/test-booking.pdf"
    property_config = _make_property_config()
    alerts_service = MagicMock()
    pdf_bytes = b"%PDF-1.4 fake pdf content"

    with patch("builtins.open", mock_open(read_data=pdf_bytes)):
        with patch("app.integrations.hoa.email.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 1, 10, 0, 0, tzinfo=ET)
            send_hoa_email(
                booking,
                pdf_path=booking.signed_pdf_path,
                property_config=property_config,
                alerts_service=alerts_service,
            )

    send_call = alerts_service.users().messages().send.call_args
    body_arg = send_call.kwargs.get("body") or send_call[1].get("body") or send_call[0][0]
    raw_bytes = base64.urlsafe_b64decode(body_arg["raw"] + "==")
    parsed = message_from_bytes(raw_bytes)
    assert parsed["Subject"] == "Guest Registration Form - Arriving 8/1/26"
    # Guest name must NOT be in the subject anymore
    assert "Hoa" not in parsed["Subject"]
    assert "Testguest" not in parsed["Subject"]


def test_send_hoa_email_body_greeting_signoff_and_from_display_name():
    """Body uses the ET time-of-day greeting, the registration-form sentence with
    M/D/YY, 'Thank you,' and the owner's full sign-off name; the From header carries
    a personal display name (deliverability)."""
    import base64
    from email import message_from_bytes

    from app.integrations.hoa.email import send_hoa_email

    booking = _make_booking()  # check_in_date = 2026-08-01
    booking.signed_pdf_path = "/app/data/pdfs/test-booking.pdf"
    property_config = _make_property_config()
    alerts_service = MagicMock()

    with patch("builtins.open", mock_open(read_data=b"%PDF-1.4")):
        with patch("app.integrations.hoa.email.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 1, 10, 0, 0, tzinfo=ET)  # morning
            send_hoa_email(
                booking,
                pdf_path=booking.signed_pdf_path,
                property_config=property_config,
                alerts_service=alerts_service,
                owners_signature_name=SIGNATURE_NAME,
                from_address=ALERTS_ADDRESS,
            )

    send_call = alerts_service.users().messages().send.call_args
    body_arg = send_call.kwargs.get("body") or send_call[1].get("body") or send_call[0][0]
    raw_bytes = base64.urlsafe_b64decode(body_arg["raw"] + "==")
    parsed = message_from_bytes(raw_bytes)

    assert parsed["From"] == f"{SIGNATURE_NAME} <{ALERTS_ADDRESS}>"

    payload = parsed.get_payload()[0].get_payload(decode=True).decode()
    assert payload.startswith("Good morning,")
    assert "Attached, please find the registration form for my guest, arriving 8/1/26." in payload
    assert "Thank you," in payload
    assert SIGNATURE_NAME in payload
    # Old copy must be gone
    assert "A new guest will be arriving" not in payload
    assert "signed agreement" not in payload


# ---------------------------------------------------------------------------
# handle_hoa_email — immediate-trigger task handler (HOA-03)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_hoa_email_sends_when_today_in_window():
    """When today is inside the HOA window, send_hoa_email is called and task → COMPLETE."""
    from app.tasks.handlers.hoa import handle_hoa_email

    today = date.today()
    booking = _make_booking()
    booking.check_in_date = date(today.year, today.month, today.day)
    booking.signed_pdf_path = "/app/data/pdfs/test.pdf"
    booking.property_id = "test_property"
    task = _make_hoa_task(TaskState.WAITING)
    session = AsyncMock()

    # Make hoa_window return a range that includes today
    window_earliest = (
        date(today.year, today.month, today.day)
        - __import__("datetime").timedelta(days=2)
    )
    window_latest = (
        date(today.year, today.month, today.day)
        + __import__("datetime").timedelta(days=5)
    )

    with patch("app.tasks.handlers.hoa.hoa_window", return_value=(window_earliest, window_latest)):
        with patch("app.tasks.handlers.hoa.send_hoa_email") as mock_send:
            with patch("app.tasks.handlers.hoa.get_alerts_service", return_value=MagicMock()):
                with patch("app.tasks.handlers.hoa.claim_task", new=_claim_emulator(task)):
                    with patch("app.tasks.handlers.hoa.load_config") as mock_cfg:
                        mock_prop = _make_property_config()
                        mock_cfg.return_value.properties = [mock_prop]
                        mock_cfg.return_value.owners.primary_name = "Owner"
                        mock_cfg.return_value.email.alerts = ALERTS_ADDRESS
                        with patch("builtins.open", mock_open(read_data=b"%PDF")):
                            await handle_hoa_email(booking, task, session)

    mock_send.assert_called_once()
    assert task.state == TaskState.COMPLETE


@pytest.mark.asyncio
async def test_handle_hoa_email_skips_when_too_early():
    """When today is before the window opens, send_hoa_email is NOT called;
    task stays WAITING (not FAILED)."""
    from app.tasks.handlers.hoa import handle_hoa_email

    today = date.today()
    booking = _make_booking()
    booking.signed_pdf_path = "/app/data/pdfs/test.pdf"
    booking.property_id = "test_property"
    task = _make_hoa_task(TaskState.WAITING)
    session = AsyncMock()

    # Window starts 3 days in the future
    window_earliest = (
        date(today.year, today.month, today.day)
        + __import__("datetime").timedelta(days=3)
    )
    window_latest = (
        date(today.year, today.month, today.day)
        + __import__("datetime").timedelta(days=10)
    )

    with patch("app.tasks.handlers.hoa.hoa_window", return_value=(window_earliest, window_latest)):
        with patch("app.tasks.handlers.hoa.send_hoa_email") as mock_send:
            with patch("app.tasks.handlers.hoa.load_config") as mock_cfg:
                mock_prop = _make_property_config()
                mock_cfg.return_value.properties = [mock_prop]
                await handle_hoa_email(booking, task, session)

    mock_send.assert_not_called()
    # Task must NOT be FAILED — D-09 dictates Phase 5 will handle it
    assert task.state != TaskState.FAILED
    assert task.state == TaskState.WAITING


@pytest.mark.asyncio
async def test_handle_hoa_email_sends_late_when_past_latest():
    """A signature landing after the last acceptable send day still sends
    IMMEDIATELY (owner decision 2026-07-22, bug hunt F3): `latest` is a
    scheduling deadline, not a send-blocker — late is better than never. The
    old behavior (return, leave WAITING) stranded the task forever: the hourly
    scan re-selected it every hour and the HOA never got the form."""
    from app.tasks.handlers.hoa import handle_hoa_email

    today = date.today()
    booking = _make_booking()
    booking.check_in_date = date(today.year, today.month, today.day)
    booking.signed_pdf_path = "/app/data/pdfs/test.pdf"
    booking.property_id = "test_property"
    task = _make_hoa_task(TaskState.WAITING)
    session = AsyncMock()

    # Window entirely in the past
    window_earliest = (
        date(today.year, today.month, today.day)
        - __import__("datetime").timedelta(days=10)
    )
    window_latest = (
        date(today.year, today.month, today.day)
        - __import__("datetime").timedelta(days=1)
    )

    with patch("app.tasks.handlers.hoa.hoa_window", return_value=(window_earliest, window_latest)):
        with patch("app.tasks.handlers.hoa.send_hoa_email") as mock_send:
            with patch("app.tasks.handlers.hoa.get_alerts_service", return_value=MagicMock()):
                with patch("app.tasks.handlers.hoa.claim_task", new=_claim_emulator(task)):
                    with patch("app.tasks.handlers.hoa.load_config") as mock_cfg:
                        mock_prop = _make_property_config()
                        mock_cfg.return_value.properties = [mock_prop]
                        mock_cfg.return_value.owners.primary_name = "Owner"
                        mock_cfg.return_value.email.alerts = ALERTS_ADDRESS
                        with patch("builtins.open", mock_open(read_data=b"%PDF")):
                            await handle_hoa_email(booking, task, session)

    mock_send.assert_called_once()
    assert task.state == TaskState.COMPLETE


@pytest.mark.asyncio
async def test_handle_hoa_email_consults_eastern_date_not_server_date():
    """The window comparison must use today_et(), never date.today() — the
    production container runs UTC, where the server date is already tomorrow
    from 8 PM ET to midnight (bug hunt F2). Pin today_et to a sentinel date the
    window brackets exactly; if the handler consulted the server clock instead,
    it would classify the date as out-of-window and never send."""
    from app.tasks.handlers.hoa import handle_hoa_email

    sentinel = date(2030, 6, 5)
    booking = _make_booking()
    booking.signed_pdf_path = "/app/data/pdfs/test.pdf"
    booking.property_id = "test_property"
    task = _make_hoa_task(TaskState.WAITING)
    session = AsyncMock()

    with patch("app.tasks.handlers.hoa.hoa_window", return_value=(sentinel, sentinel)):
        with patch("app.tasks.handlers.hoa.today_et", return_value=sentinel):
            with patch("app.tasks.handlers.hoa.send_hoa_email") as mock_send:
                with patch("app.tasks.handlers.hoa.get_alerts_service", return_value=MagicMock()):
                    with patch("app.tasks.handlers.hoa.claim_task", new=_claim_emulator(task)):
                        with patch("app.tasks.handlers.hoa.load_config") as mock_cfg:
                            mock_prop = _make_property_config()
                            mock_cfg.return_value.properties = [mock_prop]
                            mock_cfg.return_value.owners.primary_name = "Owner"
                            mock_cfg.return_value.email.alerts = ALERTS_ADDRESS
                            with patch("builtins.open", mock_open(read_data=b"%PDF")):
                                await handle_hoa_email(booking, task, session)

    mock_send.assert_called_once()
    assert task.state == TaskState.COMPLETE


@pytest.mark.asyncio
async def test_handle_hoa_email_skips_when_signed_pdf_path_missing():
    """When booking.signed_pdf_path is None, send_hoa_email is NOT called; warning logged."""
    from app.tasks.handlers.hoa import handle_hoa_email

    booking = _make_booking()
    booking.signed_pdf_path = None  # missing PDF
    booking.property_id = "test_property"
    task = _make_hoa_task(TaskState.WAITING)
    session = AsyncMock()

    with patch("app.tasks.handlers.hoa.send_hoa_email") as mock_send:
        with patch("app.tasks.handlers.hoa.log") as mock_log:
            await handle_hoa_email(booking, task, session)

    mock_send.assert_not_called()
    # A warning must have been logged about the missing PDF
    assert mock_log.warning.called
    call_args = mock_log.warning.call_args[0]
    assert booking.id in call_args or any(str(booking.id) in str(a) for a in call_args)


@pytest.mark.asyncio
async def test_handle_hoa_email_send_does_not_block_the_event_loop():
    """F11 (bug hunt 2026-07-22): the Gmail send is synchronous network I/O.
    Run directly on the event loop, a slow/hung send freezes the WHOLE app
    (webhooks, dashboard, scheduler misfires). The handler must run it in a
    worker thread. Canary: a concurrent coroutine must complete while the
    (deliberately slow) send is in flight."""
    import asyncio
    import time

    from app.tasks.handlers.hoa import handle_hoa_email

    today = date.today()
    booking = _make_booking()
    booking.check_in_date = date(today.year, today.month, today.day)
    booking.signed_pdf_path = "/app/data/pdfs/test.pdf"
    booking.property_id = "test_property"
    task = _make_hoa_task(TaskState.WAITING)
    session = AsyncMock()

    window_earliest = today - __import__("datetime").timedelta(days=2)
    window_latest = today + __import__("datetime").timedelta(days=5)

    def slow_send(*args, **kwargs):
        time.sleep(0.4)  # a stand-in for a slow Gmail POST

    canary = {"latency": None}

    async def canary_coro():
        start = time.monotonic()
        await asyncio.sleep(0.05)
        canary["latency"] = time.monotonic() - start

    with patch("app.tasks.handlers.hoa.hoa_window", return_value=(window_earliest, window_latest)):
        with patch("app.tasks.handlers.hoa.send_hoa_email", side_effect=slow_send):
            with patch("app.tasks.handlers.hoa.get_alerts_service", return_value=MagicMock()):
                with patch("app.tasks.handlers.hoa.claim_task", new=_claim_emulator(task)):
                    with patch("app.tasks.handlers.hoa.load_config") as mock_cfg:
                        mock_prop = _make_property_config()
                        mock_cfg.return_value.properties = [mock_prop]
                        mock_cfg.return_value.owners.primary_name = "Owner"
                        mock_cfg.return_value.email.alerts = ALERTS_ADDRESS
                        with patch("builtins.open", mock_open(read_data=b"%PDF")):
                            # Canary FIRST so it is mid-await when the
                            # handler's send runs.
                            await asyncio.gather(
                                canary_coro(),
                                handle_hoa_email(booking, task, session),
                            )

    assert task.state == TaskState.COMPLETE
    # If the send blocked the loop, the 0.05s canary couldn't finish until the
    # 0.4s sleep released it.
    assert canary["latency"] is not None and canary["latency"] < 0.3
