"""Step 14 — trigger-wiring isolation tests.

Each test fires exactly ONE trigger against a seeded booking in the real local
Postgres test DB and asserts the resulting state transition. External HTTP
boundaries (Gmail, DocuSign, Seam, Sheets) are mocked — this step verifies the
*wiring* of each trigger, not the live integrations (those were covered in
Phase 2).

Triggers exercised:
  1. poll_booking_feed          — new booking email  → Booking + initial tasks
  2. poll_booking_feed          — dedup: already-seen message id is skipped
  3. poll_booking_feed          — cancellation email → booking CANCELLED
  4. check_daily_reminders      — missing-phone within 7d → reminder task COMPLETE
  5. check_hoa_window           — in-window + signed PDF → HOA_EMAIL COMPLETE
  6. check_hoa_window           — early window → HOA_EMAIL stays WAITING (no send)
  7. check_hoa_window           — no signed PDF → not selected (no send)
  8. check_hoa_window           — per-booking failure isolation (substantiated fix)
  9. docusign webhook completed — far-future → signed_pdf_path set, HOA WAITING
 10. docusign webhook completed — in-window → immediate HOA send, HOA COMPLETE
 11. docusign webhook declined  — DOCUSIGN_SEND → FAILED
 12. dashboard dispatch         — _dispatch_pending_tasks runs PENDING handlers only

The app's ``AsyncSessionLocal`` binds to ``settings.database_url``, which
``tests/conftest.py`` points at the same local Postgres test DB the ``db_session``
fixture creates the schema in — so a trigger opening its own session sees the
booking the test seeded and committed.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.db.models import (
    Booking,
    BookingStatus,
    BookingTask,
    Platform,
    ProcessedMessage,
    TaskState,
    TaskType,
)
from app.db.session import AsyncSessionLocal
from app.db.session import engine as app_engine
from app.ingestion.classifier import EmailType

FIXTURES = Path(__file__).parent.parent / "fixtures"
from tests.conftest import recorded_email_bytes

TODAY = date.today()

# Reused module-level references to load_config so every trigger's config lookup
# resolves to the committed test fixture (property_id == "test_property").
_CONFIG_TARGETS = [
    "app.config.load_config",
    "app.tasks.scheduled.load_config",
    "app.tasks.handlers.hoa.load_config",
    "app.tasks.handlers.docusign.load_config",
    "app.tasks.handlers.cleaner_sheet.load_config",
    "app.ingestion.cancellation.load_config",
]


@pytest_asyncio.fixture(autouse=True)
async def _dispose_app_engine():
    """Drop the app engine's pooled connections at teardown.

    The app's global ``AsyncSessionLocal`` engine is created once at import, but
    pytest-asyncio (auto mode) runs each test on a fresh event loop. asyncpg
    connections are bound to the loop that created them, so a connection left in
    the pool by one test errors ("another operation is in progress") when the
    next test's loop tries to reuse it. Disposing in teardown — on the same loop
    that created the connections — keeps each test's pool loop-local.
    """
    yield
    await app_engine.dispose()


@pytest.fixture(autouse=True)
def patch_config():
    """Point every trigger's load_config() at the committed test fixture config."""
    from app.config import load_config as real_load_config

    cfg = real_load_config(FIXTURES / "config.test.yaml")
    with contextlib.ExitStack() as stack:
        for target in _CONFIG_TARGETS:
            stack.enter_context(patch(target, return_value=cfg))
        yield cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _booking(**overrides) -> Booking:
    """Build a Booking with sensible defaults for a test_property reservation."""
    defaults = dict(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id="HMTEST0001",
        property_id="test_property",
        guest_first_name="Trig",
        guest_last_name="Test",
        guest_phone="+15551234567",
        guest_email="trig@example.com",
        check_in_date=TODAY + timedelta(days=30),
        check_out_date=TODAY + timedelta(days=34),
        status=BookingStatus.ACTIVE,
        source_email_message_id=f"src-{uuid.uuid4()}",
    )
    defaults.update(overrides)
    return Booking(**defaults)


async def _reload(booking_id: uuid.UUID) -> tuple[Booking, dict[TaskType, BookingTask]]:
    """Re-read a booking + its tasks in a fresh session (sees committed writes)."""
    async with AsyncSessionLocal() as session:
        booking = (
            await session.execute(
                select(Booking)
                .where(Booking.id == booking_id)
                .options(selectinload(Booking.tasks))
            )
        ).scalar_one()
        return booking, {t.task_type: t for t in booking.tasks}


async def _booking_count() -> int:
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(func.count(Booking.id)))).scalar_one()


def _external_id_from_eml(eml_name: str, platform: Platform) -> str:
    """Read a cancellation's confirmation code out of a fixture email using the
    production parser, so no real booking code is hardcoded in this file."""
    from email import message_from_bytes, policy

    from app.ingestion.cancellation import parse_cancellation_external_id

    msg = message_from_bytes(recorded_email_bytes(eml_name), policy=policy.default)
    external_id = parse_cancellation_external_id(msg, platform)
    assert external_id, f"no external id parsed from {eml_name}"
    return external_id


def _gmail_service_for(msg_id: str, eml_name: str) -> MagicMock:
    """Fake Gmail service returning one message whose raw body is the fixture eml."""
    raw = base64.urlsafe_b64encode(recorded_email_bytes(eml_name)).decode()
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {"messages": [{"id": msg_id}]}
    messages.get.return_value.execute.return_value = {"raw": raw}
    return service


def _hmac_sig(body: bytes, secret: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("utf-8")


WEBHOOK_SECRET = "step14-webhook-secret"


# ===========================================================================
# 1–3. poll_booking_feed
# ===========================================================================


async def test_poller_new_booking_persists_booking_and_tasks(db_session):
    from app.ingestion.poller import poll_booking_feed

    service = _gmail_service_for("m-airbnb-1", "airbnbbooking-1.eml")

    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            return_value=service,
        ),
        patch("app.integrations.gmail.oauth.get_alerts_service", return_value=MagicMock()),
    ):
        await poll_booking_feed()

    async with AsyncSessionLocal() as session:
        booking = (
            await session.execute(
                select(Booking)
                .where(Booking.source_email_message_id == "m-airbnb-1")
                .options(selectinload(Booking.tasks))
            )
        ).scalar_one()

    assert booking.platform == Platform.AIRBNB
    assert booking.status == BookingStatus.ACTIVE
    assert booking.external_id  # a confirmation code was parsed
    # _initial_tasks creates the full 11-row task set for a new booking.
    assert len(booking.tasks) == 11
    tasks = {t.task_type: t for t in booking.tasks}
    # Airbnb emails never carry phone/email → those automations start WAITING.
    assert tasks[TaskType.DOCUSIGN_SEND].state == TaskState.WAITING
    assert tasks[TaskType.ACCESS_CODE_CREATE].state == TaskState.WAITING
    assert tasks[TaskType.HOA_EMAIL].state == TaskState.WAITING
    # The new-booking alert email was sent (alerts service mocked) → the task
    # records COMPLETE (R7 fix; it used to stay PENDING forever).
    assert tasks[TaskType.OWNER_ALERT_NEW_BOOKING].state == TaskState.COMPLETE
    assert tasks[TaskType.OWNER_ALERT_NEW_BOOKING].completed_at is not None


async def test_poller_dedup_skips_already_seen_message(db_session):
    from app.ingestion.poller import poll_booking_feed

    seeded = _booking(source_email_message_id="m-dup")
    db_session.add(seeded)
    await db_session.commit()

    service = _gmail_service_for("m-dup", "airbnbbooking-1.eml")

    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            return_value=service,
        ),
        patch("app.integrations.gmail.oauth.get_alerts_service", return_value=MagicMock()),
    ):
        await poll_booking_feed()

    # Dedup happens before fetch: the message body is never even retrieved.
    service.users.return_value.messages.return_value.get.assert_not_called()
    assert await _booking_count() == 1


async def test_poller_cancellation_marks_booking_cancelled(db_session):
    from app.ingestion.poller import poll_booking_feed

    seeded = _booking(
        platform=Platform.AIRBNB,
        # Taken from the fixture email itself, so the seeded booking is the one
        # the cancellation refers to without hardcoding a real code here.
        external_id=_external_id_from_eml("airbnb-cancellation-1.eml", Platform.AIRBNB),
        source_email_message_id="src-cancel-target",
    )
    db_session.add(seeded)
    await db_session.commit()

    alerts = MagicMock()
    service = _gmail_service_for("m-cancel-1", "airbnb-cancellation-1.eml")

    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            return_value=service,
        ),
        # cancellation.py binds get_alerts_service at module import → patch there.
        patch("app.ingestion.cancellation.get_alerts_service", return_value=alerts),
    ):
        await poll_booking_feed()

    booking, tasks = await _reload(seeded.id)
    assert booking.status == BookingStatus.CANCELLED
    assert booking.cancellation_email_message_id == "m-cancel-1"
    # Owner alert actually dispatched via the alerts Gmail account.
    alerts.users.return_value.messages.return_value.send.assert_called_once()
    # Cancellation-alert task rows created + completed.
    assert tasks[TaskType.OWNER_ALERT_CANCELLATION_HOA].state == TaskState.COMPLETE
    assert tasks[TaskType.OWNER_ALERT_CANCELLATION_CLEANER].state == TaskState.COMPLETE


# ===========================================================================
# 4. check_daily_reminders
# ===========================================================================


async def test_daily_reminders_fires_missing_phone_7d(db_session):
    from app.tasks import scheduled

    booking = _booking(
        guest_phone=None,
        guest_email="has@example.com",
        signed_pdf_path="/tmp/signed.pdf",  # suppress docusign-unsigned reminder
        check_in_date=TODAY + timedelta(days=6),  # within 7d, outside 4d
    )
    booking.tasks.extend(
        BookingTask(task_type=tt, state=TaskState.PENDING)
        for tt in (
            TaskType.OWNER_ALERT_MISSING_PHONE_7D,
            TaskType.OWNER_ALERT_MISSING_PHONE_4D,
            TaskType.OWNER_ALERT_MISSING_EMAIL_7D,
            TaskType.OWNER_ALERT_MISSING_EMAIL_4D,
            TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D,
            TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D,
        )
    )
    db_session.add(booking)
    await db_session.commit()

    send = MagicMock()
    with (
        patch.object(scheduled, "send_reminder_alert", send),
        patch.object(scheduled, "get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.check_daily_reminders()

    _, tasks = await _reload(booking.id)
    assert tasks[TaskType.OWNER_ALERT_MISSING_PHONE_7D].state == TaskState.COMPLETE
    assert tasks[TaskType.OWNER_ALERT_MISSING_PHONE_7D].completed_at is not None
    assert tasks[TaskType.OWNER_ALERT_MISSING_PHONE_4D].state == TaskState.PENDING
    # email present + pdf signed → those reminders are moot and flip to SKIPPED
    # (they used to sit PENDING forever)
    assert tasks[TaskType.OWNER_ALERT_MISSING_EMAIL_7D].state == TaskState.SKIPPED
    assert tasks[TaskType.OWNER_ALERT_MISSING_EMAIL_4D].state == TaskState.SKIPPED
    assert tasks[TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_7D].state == TaskState.SKIPPED
    assert tasks[TaskType.OWNER_ALERT_DOCUSIGN_UNSIGNED_4D].state == TaskState.SKIPPED
    send.assert_called_once()
    assert send.call_args.args[0] == TaskType.OWNER_ALERT_MISSING_PHONE_7D


async def test_daily_reminders_sweeps_moot_reminders(db_session):
    """PENDING reminders on past-check-in or cancelled bookings can never fire —
    the daily job's backstop sweep flips them to SKIPPED (real bulk UPDATE)."""
    from app.tasks import scheduled

    past = _booking(
        guest_phone=None,
        external_id="HMPAST0001",
        check_in_date=TODAY - timedelta(days=3),
        check_out_date=TODAY - timedelta(days=1),
    )
    cancelled = _booking(
        guest_phone=None,
        external_id="HMCANC0001",
        status=BookingStatus.CANCELLED,
        check_in_date=TODAY + timedelta(days=10),
    )
    future = _booking(
        guest_phone=None,
        external_id="HMFUTR0001",
        check_in_date=TODAY + timedelta(days=10),
    )
    for b in (past, cancelled, future):
        b.tasks.append(
            BookingTask(
                task_type=TaskType.OWNER_ALERT_MISSING_PHONE_7D,
                state=TaskState.PENDING,
            )
        )
        db_session.add(b)
    await db_session.commit()

    with (
        patch.object(scheduled, "send_reminder_alert", MagicMock()),
        patch.object(scheduled, "get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.check_daily_reminders()

    _, past_tasks = await _reload(past.id)
    _, cancelled_tasks = await _reload(cancelled.id)
    _, future_tasks = await _reload(future.id)
    assert past_tasks[TaskType.OWNER_ALERT_MISSING_PHONE_7D].state == TaskState.SKIPPED
    assert cancelled_tasks[TaskType.OWNER_ALERT_MISSING_PHONE_7D].state == TaskState.SKIPPED
    # A live future booking with the condition still present is untouched.
    assert future_tasks[TaskType.OWNER_ALERT_MISSING_PHONE_7D].state == TaskState.PENDING


# ===========================================================================
# 5–8. check_hoa_window
# ===========================================================================


def _hoa_booking(**overrides) -> Booking:
    kwargs = dict(
        signed_pdf_path="/tmp/signed.pdf",
        check_in_date=TODAY + timedelta(days=4),  # in HOA window today
    )
    kwargs.update(overrides)
    b = _booking(**kwargs)
    b.tasks.append(BookingTask(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING))
    return b


async def test_hoa_window_in_window_sends_and_completes(db_session):
    from app.tasks import scheduled

    booking = _hoa_booking()
    db_session.add(booking)
    await db_session.commit()

    send = MagicMock()
    with (
        patch("app.tasks.handlers.hoa.send_hoa_email", send),
        patch("app.tasks.handlers.hoa.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.check_hoa_window()

    _, tasks = await _reload(booking.id)
    assert tasks[TaskType.HOA_EMAIL].state == TaskState.COMPLETE
    send.assert_called_once()


async def test_hoa_window_early_leaves_waiting(db_session):
    from app.tasks import scheduled

    booking = _hoa_booking(check_in_date=TODAY + timedelta(days=60))  # far future → early
    db_session.add(booking)
    await db_session.commit()

    send = MagicMock()
    with (
        patch("app.tasks.handlers.hoa.send_hoa_email", send),
        patch("app.tasks.handlers.hoa.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.check_hoa_window()

    _, tasks = await _reload(booking.id)
    assert tasks[TaskType.HOA_EMAIL].state == TaskState.WAITING
    send.assert_not_called()


async def test_hoa_window_no_signed_pdf_not_selected(db_session):
    from app.tasks import scheduled

    booking = _hoa_booking(signed_pdf_path=None)
    db_session.add(booking)
    await db_session.commit()

    send = MagicMock()
    with (
        patch("app.tasks.handlers.hoa.send_hoa_email", send),
        patch("app.tasks.handlers.hoa.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.check_hoa_window()

    _, tasks = await _reload(booking.id)
    assert tasks[TaskType.HOA_EMAIL].state == TaskState.WAITING
    send.assert_not_called()


async def test_hoa_window_isolates_per_booking_failure(db_session):
    """One booking's HOA send failure must not block the others in the same run.

    Substantiates the failure-isolation ordering bug: the original loop shared a
    single session and rolled it back on failure, which expired every remaining
    booking so the *next* booking's ``.tasks`` access raised mid-loop.
    """
    from app.tasks import scheduled

    b1 = _hoa_booking(external_id="HMISOL0001", source_email_message_id="iso-1")
    b2 = _hoa_booking(external_id="HMISOL0002", source_email_message_id="iso-2")
    db_session.add_all([b1, b2])
    await db_session.commit()

    calls = {"n": 0}

    def _send(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom on first booking")

    with (
        patch("app.tasks.handlers.hoa.send_hoa_email", side_effect=_send),
        patch("app.tasks.handlers.hoa.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.check_hoa_window()

    _, t1 = await _reload(b1.id)
    _, t2 = await _reload(b2.id)
    states = sorted(
        [t1[TaskType.HOA_EMAIL].state, t2[TaskType.HOA_EMAIL].state],
        key=lambda s: s.value,
    )
    # Exactly one booking sent (COMPLETE); the failed one stays WAITING for retry.
    assert states == sorted(
        [TaskState.COMPLETE, TaskState.WAITING], key=lambda s: s.value
    )
    assert calls["n"] == 2  # both bookings were attempted


# ===========================================================================
# 9–11. DocuSign webhook (event trigger)
# ===========================================================================


async def _post_webhook(status: str, envelope_id: str):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    body = json.dumps({"status": status, "envelopeId": envelope_id}).encode("utf-8")
    sig = _hmac_sig(body, WEBHOOK_SECRET)
    mock_settings = MagicMock()
    mock_settings.docusign_hmac_key = WEBHOOK_SECRET
    with patch("app.routers.webhooks.settings", mock_settings):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/webhooks/docusign",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-DocuSign-Signature-1": sig,
                },
            )


def _mock_envelope_api() -> tuple:
    api = MagicMock()
    api.get_document.return_value = b"%PDF-1.4 test signed document"
    return api, "test-account"


async def test_webhook_completed_far_future_stores_pdf_leaves_hoa_waiting(
    db_session, tmp_path
):
    envelope_id = str(uuid.uuid4())
    booking = _booking(check_in_date=TODAY + timedelta(days=60))
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.DOCUSIGN_SEND,
            state=TaskState.COMPLETE,
            external_ref=envelope_id,
        )
    )
    booking.tasks.append(
        BookingTask(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING)
    )
    db_session.add(booking)
    await db_session.commit()

    with (
        patch(
            "app.tasks.handlers.docusign.get_envelope_api",
            return_value=_mock_envelope_api(),
        ),
        patch(
            "app.tasks.handlers.docusign._pdf_path_for_booking",
            side_effect=lambda b: tmp_path / f"{b.id}.pdf",
        ),
    ):
        resp = await _post_webhook("completed", envelope_id)

    assert resp.status_code == 200
    booking_after, tasks = await _reload(booking.id)
    assert booking_after.signed_pdf_path == str(tmp_path / f"{booking.id}.pdf")
    assert (tmp_path / f"{booking.id}.pdf").exists()
    # Far-future check-in → HOA window not open yet → scheduler will handle it.
    assert tasks[TaskType.HOA_EMAIL].state == TaskState.WAITING


async def test_webhook_completed_in_window_sends_hoa_immediately(db_session, tmp_path):
    envelope_id = str(uuid.uuid4())
    booking = _booking(check_in_date=TODAY + timedelta(days=4))  # in HOA window
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.DOCUSIGN_SEND,
            state=TaskState.COMPLETE,
            external_ref=envelope_id,
        )
    )
    booking.tasks.append(
        BookingTask(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING)
    )
    db_session.add(booking)
    await db_session.commit()

    send = MagicMock()
    with (
        patch(
            "app.tasks.handlers.docusign.get_envelope_api",
            return_value=_mock_envelope_api(),
        ),
        patch(
            "app.tasks.handlers.docusign._pdf_path_for_booking",
            side_effect=lambda b: tmp_path / f"{b.id}.pdf",
        ),
        patch("app.tasks.handlers.docusign.send_hoa_email", send),
        patch(
            "app.tasks.handlers.docusign.get_alerts_service", return_value=MagicMock()
        ),
    ):
        resp = await _post_webhook("completed", envelope_id)

    assert resp.status_code == 200
    booking_after, tasks = await _reload(booking.id)
    assert booking_after.signed_pdf_path is not None
    # Step-2 fix path: immediate HOA send fired and the task is COMPLETE.
    send.assert_called_once()
    assert tasks[TaskType.HOA_EMAIL].state == TaskState.COMPLETE


async def test_webhook_declined_marks_docusign_send_failed(db_session):
    envelope_id = str(uuid.uuid4())
    booking = _booking()
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.DOCUSIGN_SEND,
            state=TaskState.COMPLETE,
            external_ref=envelope_id,
        )
    )
    db_session.add(booking)
    await db_session.commit()

    resp = await _post_webhook("declined", envelope_id)

    assert resp.status_code == 200
    _, tasks = await _reload(booking.id)
    assert tasks[TaskType.DOCUSIGN_SEND].state == TaskState.FAILED
    assert tasks[TaskType.DOCUSIGN_SEND].last_error == "Recipient declined to sign"


# ===========================================================================
# 12. Dashboard-initiated background dispatch
# ===========================================================================


async def test_dispatch_runs_pending_handlers_and_skips_waiting(db_session):
    from app.tasks import dispatch

    booking = _booking()
    booking.tasks.extend(
        [
            BookingTask(task_type=TaskType.CLEANER_SHEET_ADD, state=TaskState.PENDING),
            BookingTask(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.PENDING),
            BookingTask(task_type=TaskType.ACCESS_CODE_CREATE, state=TaskState.PENDING),
            BookingTask(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING),
        ]
    )
    db_session.add(booking)
    await db_session.commit()

    async def _complete(bk, task, session):
        task.state = TaskState.COMPLETE

    cleaner = AsyncMock(side_effect=_complete)
    docusign = AsyncMock(side_effect=_complete)
    access = AsyncMock(side_effect=_complete)
    hoa = AsyncMock(side_effect=_complete)

    with (
        patch.object(dispatch, "handle_cleaner_sheet", cleaner),
        patch.object(dispatch, "handle_docusign_send", docusign),
        patch.object(dispatch, "handle_access_code_create", access),
        patch.object(dispatch, "handle_hoa_email", hoa),
    ):
        await dispatch._dispatch_pending_tasks(booking.id)

    cleaner.assert_called_once()
    docusign.assert_called_once()
    access.assert_called_once()
    hoa.assert_not_called()  # WAITING tasks are never dispatched

    _, tasks = await _reload(booking.id)
    assert tasks[TaskType.CLEANER_SHEET_ADD].state == TaskState.COMPLETE
    assert tasks[TaskType.DOCUSIGN_SEND].state == TaskState.COMPLETE
    assert tasks[TaskType.ACCESS_CODE_CREATE].state == TaskState.COMPLETE
    assert tasks[TaskType.HOA_EMAIL].state == TaskState.WAITING


# ===========================================================================
# Step 19 · R1 — HOA double-send: webhook-immediate vs hourly, no atomic claim
# ===========================================================================


async def test_hoa_no_double_send_webhook_racing_hourly(db_session, tmp_path):
    """R1: the webhook-immediate HOA send and the hourly ``check_hoa_window``
    must not BOTH send the HOA email for the same booking.

    Deterministic interleave (no sleeps): the webhook handler runs but its
    session has NOT yet committed the HOA task's terminal state when the hourly
    scan reads the DB. Without an atomic claim, both paths observe the HOA task
    as WAITING and both call ``send_hoa_email`` — the HOA gets two emails. With
    the shared atomic claim (WAITING -> IN_PROGRESS, committed before the send),
    the webhook publishes the claim before sending, so the hourly scan sees a
    non-WAITING task and skips.
    """
    envelope_id = str(uuid.uuid4())
    booking = _booking(check_in_date=TODAY + timedelta(days=4))  # in HOA window
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.DOCUSIGN_SEND,
            state=TaskState.COMPLETE,
            external_ref=envelope_id,
        )
    )
    booking.tasks.append(
        BookingTask(task_type=TaskType.HOA_EMAIL, state=TaskState.WAITING)
    )
    db_session.add(booking)
    await db_session.commit()

    from app.tasks.handlers.docusign import handle_envelope_completed
    from app.tasks.scheduled import check_hoa_window

    # One shared mock so we count TOTAL HOA sends across both paths.
    send = MagicMock()

    with (
        patch(
            "app.tasks.handlers.docusign.get_envelope_api",
            return_value=_mock_envelope_api(),
        ),
        patch(
            "app.tasks.handlers.docusign._pdf_path_for_booking",
            side_effect=lambda b: tmp_path / f"{b.id}.pdf",
        ),
        patch("app.tasks.handlers.docusign.send_hoa_email", send),
        patch("app.tasks.handlers.docusign.get_alerts_service", return_value=MagicMock()),
        patch("app.tasks.handlers.hoa.send_hoa_email", send),
        patch("app.tasks.handlers.hoa.get_alerts_service", return_value=MagicMock()),
    ):
        # Webhook path runs in its own session; we deliberately do NOT commit
        # its terminal HOA state until AFTER the hourly job has scanned.
        async with AsyncSessionLocal() as wh_session:
            bk = (
                await wh_session.execute(
                    select(Booking)
                    .where(Booking.id == booking.id)
                    .options(selectinload(Booking.tasks))
                )
            ).scalar_one()
            ds_task = next(
                t for t in bk.tasks if t.task_type == TaskType.DOCUSIGN_SEND
            )
            await handle_envelope_completed(bk, ds_task, envelope_id, wh_session)
            # Hourly job fires before the webhook session commits its final state.
            await check_hoa_window()
            await wh_session.commit()

    # Exactly one HOA email was sent despite the two concurrent paths.
    assert send.call_count == 1
    _, tasks = await _reload(booking.id)
    assert tasks[TaskType.HOA_EMAIL].state == TaskState.COMPLETE


# ===========================================================================
# Step 19 · Risk 4 — DocuSign webhook: authentic but unparseable payload
# ===========================================================================


async def test_webhook_authentic_but_unparseable_alerts_owner():
    """Risk 4: an HMAC-authentic Connect payload whose envelope id / status can't
    be extracted must NOT be silently dropped. The endpoint still returns 200 (so
    Connect does not retry-storm) but fires an owner alert so an unexpected
    payload shape is visible instead of a signed form vanishing.
    """
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    # Valid JSON + valid HMAC, but no envelopeId/status on any known key path.
    body = json.dumps({"unexpected": {"envelopeId": "x"}, "weird": True}).encode("utf-8")
    sig = _hmac_sig(body, WEBHOOK_SECRET)

    mock_settings = MagicMock()
    mock_settings.docusign_hmac_key = WEBHOOK_SECRET
    mock_settings.domain = "example.test"

    alert = MagicMock()
    with (
        patch("app.routers.webhooks.settings", mock_settings),
        patch("app.routers.webhooks.send_docusign_webhook_parse_failure_alert", alert),
        patch("app.routers.webhooks.get_alerts_service", return_value=MagicMock()),
        patch("app.routers.webhooks.load_config", return_value=MagicMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhooks/docusign",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-DocuSign-Signature-1": sig,
                },
            )

    assert resp.status_code == 200
    alert.assert_called_once()
    # The alert names the payload's top-level keys so drift is diagnosable.
    assert set(alert.call_args.kwargs["payload_keys"]) == {"unexpected", "weird"}


# ===========================================================================
# Step 19 · R3b — poller dead-letter for unprocessable mail
# ===========================================================================


async def _processed_rows() -> list[ProcessedMessage]:
    async with AsyncSessionLocal() as session:
        return list((await session.execute(select(ProcessedMessage))).scalars().all())


async def test_poller_other_email_is_dead_lettered_not_refetched(db_session):
    """R3b: a message classified as OTHER is recorded so it is not re-fetched on
    every poll. No owner alert — OTHER is expected noise, not a dropped booking.
    """
    from app.ingestion import poller

    service = _gmail_service_for("m-other", "airbnbbooking-1.eml")

    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            return_value=service,
        ),
        patch("app.ingestion.poller.classify", return_value=EmailType.OTHER),
    ):
        await poller.poll_booking_feed()
        await poller.poll_booking_feed()  # second poll must skip it

    rows = await _processed_rows()
    assert len(rows) == 1
    assert rows[0].message_id == "m-other"
    assert rows[0].disposition == "other"
    # Body fetched only on the first poll; dedup skips it thereafter.
    service.users.return_value.messages.return_value.get.assert_called_once()
    assert await _booking_count() == 0


async def test_poller_unparseable_booking_records_and_alerts(db_session):
    """R3b: a message classified as a booking that fails to parse is dead-lettered
    (so it is not re-fetched forever) AND raises an owner alert (a booking that
    won't parse is a silently dropped booking — likely platform format drift).
    """
    from app.ingestion import poller

    service = _gmail_service_for("m-badbooking", "airbnbbooking-1.eml")
    alerts = MagicMock()

    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            return_value=service,
        ),
        patch("app.ingestion.poller.classify", return_value=EmailType.AIRBNB_BOOKING),
        patch(
            "app.ingestion.poller.parse_airbnb_booking",
            side_effect=ValueError("no confirmation code found"),
        ),
        patch("app.ingestion.poller.get_alerts_service", return_value=alerts),
    ):
        await poller.poll_booking_feed()
        await poller.poll_booking_feed()  # second poll must skip it

    rows = await _processed_rows()
    assert len(rows) == 1
    assert rows[0].disposition == "parse_error"
    assert rows[0].classified_as == "airbnb_booking"
    assert "no confirmation code" in rows[0].error
    # Owner was alerted via the real sender → alerts Gmail service send() called.
    alerts.users.return_value.messages.return_value.send.assert_called_once()
    # Fetched only once despite two polls.
    service.users.return_value.messages.return_value.get.assert_called_once()
    assert await _booking_count() == 0


async def test_poller_cancellation_before_booking_is_not_dead_lettered(db_session):
    """R3a: a cancellation whose booking does not exist yet must NOT be recorded —
    it self-heals once the booking arrives, so it must keep being retried.
    """
    from app.ingestion import poller

    # airbnb-cancellation-1.eml, but no matching booking seeded.
    service = _gmail_service_for("m-orphan-cancel", "airbnb-cancellation-1.eml")
    alerts = MagicMock()

    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            return_value=service,
        ),
        patch("app.ingestion.cancellation.get_alerts_service", return_value=alerts),
    ):
        await poller.poll_booking_feed()

    # Nothing dead-lettered: the message is still eligible for reprocessing.
    assert await _processed_rows() == []


# ===========================================================================
# F1 (bug hunt 2026-07-22): poller infinite-replay fixes — real DB
# ===========================================================================


def _gmail_service_for_raw(msg_id: str, raw_email: str) -> MagicMock:
    """Fake Gmail service returning one message built from an inline raw email."""
    raw = base64.urlsafe_b64encode(raw_email.encode()).decode()
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {"messages": [{"id": msg_id}]}
    messages.get.return_value.execute.return_value = {"raw": raw}
    return service


async def test_poller_duplicate_confirmation_is_dead_lettered_not_replayed(db_session):
    """F1c: the same confirmation delivered under a second Gmail message id must
    not raise IntegrityError every poll — it is dead-lettered as 'duplicate'."""
    import email as email_lib
    from email import policy as email_policy

    from app.db.models import ProcessedMessage
    from app.ingestion import poller
    from app.ingestion.parsers.airbnb import parse_airbnb_booking

    fixture = recorded_email_bytes("airbnbbooking-1.eml")
    parsed = parse_airbnb_booking(
        email_lib.message_from_bytes(fixture, policy=email_policy.default)
    )
    seeded = _booking(
        external_id=parsed.confirmation_code,
        source_email_message_id="src-original-conf",
    )
    db_session.add(seeded)
    await db_session.commit()

    service = _gmail_service_for("m-dup-conf", "airbnbbooking-1.eml")
    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            return_value=service,
        ),
        patch("app.integrations.gmail.oauth.get_alerts_service", return_value=MagicMock()),
    ):
        await poller.poll_booking_feed()
        await poller.poll_booking_feed()  # second poll must skip it

    assert await _booking_count() == 1  # no second booking row
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ProcessedMessage).where(
                    ProcessedMessage.message_id == "m-dup-conf"
                )
            )
        ).scalar_one()
    assert row.disposition == "duplicate"
    # Dead-lettered → the second poll never re-fetched the message body.
    assert service.users.return_value.messages.return_value.get.call_count == 1


async def test_poller_unprocessable_cancellation_dead_letters_and_alerts(db_session):
    """F1a: a cancellation whose subject carries no reservation code can never
    self-heal — dead-letter it and alert the owner instead of replaying forever."""
    from app.db.models import ProcessedMessage
    from app.ingestion import poller

    raw_email = (
        "From: Airbnb <automated@airbnb.com>\r\n"
        "Subject: Your reservation is canceled\r\n"
        "Content-Type: text/plain\r\n\r\nJane canceled the trip.\r\n"
    )
    alerts = MagicMock()
    service = _gmail_service_for_raw("m-cancel-noid", raw_email)
    with (
        patch(
            "app.integrations.gmail.oauth.get_booking_feed_service",
            return_value=service,
        ),
        patch("app.ingestion.poller.get_alerts_service", return_value=alerts),
    ):
        await poller.poll_booking_feed()
        await poller.poll_booking_feed()  # second poll must skip it

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ProcessedMessage).where(
                    ProcessedMessage.message_id == "m-cancel-noid"
                )
            )
        ).scalar_one()
    assert row.disposition == "cancellation_parse_error"
    assert row.classified_as == "airbnb_cancellation"
    # Owner alert dispatched (booking stays ACTIVE — all cleanups manual now).
    alerts.users.return_value.messages.return_value.send.assert_called_once()
    assert service.users.return_value.messages.return_value.get.call_count == 1


async def test_poller_gmail_fetch_does_not_block_the_event_loop(db_session):
    """F11: the poller's Gmail list/fetch calls are sync network I/O; on the
    loop they froze webhooks/dashboard for the duration of every poll. The
    canary ticks every 10ms for ~0.8s; at least one tick must land INSIDE the
    slow fetch's [start, end] window — impossible if the fetch blocks the loop."""
    import time

    from app.ingestion import poller

    window = {"start": None, "end": None}

    def slow_list_execute():
        window["start"] = time.monotonic()
        time.sleep(0.4)
        window["end"] = time.monotonic()
        return {"messages": []}

    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.side_effect = slow_list_execute

    ticks: list[float] = []

    async def canary_coro():
        for _ in range(80):
            await asyncio.sleep(0.01)
            ticks.append(time.monotonic())

    with patch(
        "app.integrations.gmail.oauth.get_booking_feed_service",
        return_value=service,
    ):
        await asyncio.gather(canary_coro(), poller.poll_booking_feed())

    assert window["start"] is not None and window["end"] is not None
    during = [t for t in ticks if window["start"] < t < window["end"]]
    assert during, (
        "no canary tick landed during the slow Gmail fetch — the fetch is "
        "blocking the event loop"
    )


# ===========================================================================
# F17 (bug hunt 2026-07-22): daily requeue/redispatch of stalled automations
# ===========================================================================


async def _seed(booking: Booking) -> uuid.UUID:
    """Persist a booking (+tasks) in its own session; return its id."""
    async with AsyncSessionLocal() as session:
        session.add(booking)
        await session.commit()
        return booking.id


async def test_requeue_flips_failed_automation_and_redispatches(db_session):
    """A FAILED automation under the attempt cap on an ACTIVE future booking
    is flipped back to PENDING and re-dispatched the next morning."""
    from app.tasks import scheduled

    booking = _booking()
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.DOCUSIGN_SEND,
            state=TaskState.FAILED,
            last_error="transient 500",
            attempt_count=1,
        )
    )
    booking_id = await _seed(booking)

    async def ok(bk, task, session):
        task.state = TaskState.COMPLETE

    with (
        patch("app.tasks.dispatch.handle_docusign_send", ok),
        patch("app.tasks.dispatch.handle_cleaner_sheet", ok),
        patch("app.tasks.dispatch.handle_access_code_create", ok),
        patch("app.tasks.dispatch.handle_hoa_email", ok),
        patch("app.tasks.scheduled.send_stalled_automations_alert") as mock_alert,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.requeue_stalled_automations()

    _, tasks = await _reload(booking_id)
    assert tasks[TaskType.DOCUSIGN_SEND].state == TaskState.COMPLETE
    mock_alert.assert_not_called()  # nothing terminal to report


async def test_requeue_redispatches_orphaned_pending(db_session):
    """A PENDING automation whose original dispatch was lost (restart between
    the contact-save commit and create_task) is picked up by the daily job."""
    from app.tasks import scheduled

    booking = _booking()
    booking.tasks.append(
        BookingTask(task_type=TaskType.CLEANER_SHEET_ADD, state=TaskState.PENDING)
    )
    booking_id = await _seed(booking)

    async def ok(bk, task, session):
        task.state = TaskState.COMPLETE

    with (
        patch("app.tasks.dispatch.handle_cleaner_sheet", ok),
        patch("app.tasks.dispatch.handle_docusign_send", ok),
        patch("app.tasks.dispatch.handle_access_code_create", ok),
        patch("app.tasks.dispatch.handle_hoa_email", ok),
        patch("app.tasks.scheduled.send_stalled_automations_alert"),
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.requeue_stalled_automations()

    _, tasks = await _reload(booking_id)
    assert tasks[TaskType.CLEANER_SHEET_ADD].state == TaskState.COMPLETE


async def test_requeue_respects_attempt_cap_and_alerts_terminal_failures(db_session):
    """At the attempt cap the task is NOT requeued (no more silent retries) and
    the owner is alerted instead."""
    from app.tasks import scheduled

    booking = _booking()
    booking.tasks.append(
        BookingTask(
            task_type=TaskType.DOCUSIGN_SEND,
            state=TaskState.FAILED,
            last_error="permanent config error",
            attempt_count=scheduled.MAX_AUTOMATION_ATTEMPTS,
        )
    )
    booking_id = await _seed(booking)

    dispatched = []

    async def spy(bk, task, session):
        dispatched.append(task.task_type)

    with (
        patch("app.tasks.dispatch.handle_docusign_send", spy),
        patch("app.tasks.dispatch.handle_cleaner_sheet", spy),
        patch("app.tasks.dispatch.handle_access_code_create", spy),
        patch("app.tasks.dispatch.handle_hoa_email", spy),
        patch("app.tasks.scheduled.send_stalled_automations_alert") as mock_alert,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.requeue_stalled_automations()

    _, tasks = await _reload(booking_id)
    assert tasks[TaskType.DOCUSIGN_SEND].state == TaskState.FAILED  # not resurrected
    assert dispatched == []
    mock_alert.assert_called_once()
    items = mock_alert.call_args.args[0]
    assert any(i["task"] == "docusign_send" for i in items)


async def test_requeue_ignores_cancelled_and_past_bookings(db_session):
    from app.tasks import scheduled

    cancelled = _booking(status=BookingStatus.CANCELLED, external_id="HMRQCAN1")
    cancelled.tasks.append(
        BookingTask(
            task_type=TaskType.DOCUSIGN_SEND, state=TaskState.FAILED, attempt_count=1
        )
    )
    past = _booking(
        external_id="HMRQPAST1",
        check_in_date=TODAY - timedelta(days=10),
        check_out_date=TODAY - timedelta(days=7),
        source_email_message_id=f"src-{uuid.uuid4()}",
    )
    past.tasks.append(
        BookingTask(
            task_type=TaskType.CLEANER_SHEET_ADD,
            state=TaskState.FAILED,
            attempt_count=1,
        )
    )
    cancelled_id = await _seed(cancelled)
    past_id = await _seed(past)

    dispatched = []

    async def spy(bk, task, session):
        dispatched.append(task.task_type)

    with (
        patch("app.tasks.dispatch.handle_docusign_send", spy),
        patch("app.tasks.dispatch.handle_cleaner_sheet", spy),
        patch("app.tasks.dispatch.handle_access_code_create", spy),
        patch("app.tasks.dispatch.handle_hoa_email", spy),
        patch("app.tasks.scheduled.send_stalled_automations_alert") as mock_alert,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.requeue_stalled_automations()

    _, cancelled_tasks = await _reload(cancelled_id)
    _, past_tasks = await _reload(past_id)
    assert cancelled_tasks[TaskType.DOCUSIGN_SEND].state == TaskState.FAILED
    assert past_tasks[TaskType.CLEANER_SHEET_ADD].state == TaskState.FAILED
    assert dispatched == []
    mock_alert.assert_not_called()  # neither booking is actionable


async def test_requeue_alerts_on_stuck_in_progress(db_session):
    """A task IN_PROGRESS for >24h is the risk register's crash-window edge —
    surface it to the owner (never auto-reset: the external side effect may
    have happened)."""
    from sqlalchemy import update as sa_update

    from app.tasks import scheduled

    booking = _booking()
    booking.tasks.append(
        BookingTask(task_type=TaskType.ACCESS_CODE_CREATE, state=TaskState.IN_PROGRESS)
    )
    booking_id = await _seed(booking)

    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_update(BookingTask)
            .where(
                BookingTask.booking_id == booking_id,
                BookingTask.task_type == TaskType.ACCESS_CODE_CREATE,
            )
            .values(updated_at=datetime.now(UTC) - timedelta(hours=25))
        )
        await session.commit()

    with (
        patch("app.tasks.scheduled.send_stalled_automations_alert") as mock_alert,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.requeue_stalled_automations()

    _, tasks = await _reload(booking_id)
    assert tasks[TaskType.ACCESS_CODE_CREATE].state == TaskState.IN_PROGRESS  # untouched
    mock_alert.assert_called_once()
    items = mock_alert.call_args.args[0]
    assert any(i["task"] == "access_code_create" and i["state"] == "in_progress" for i in items)


# ===========================================================================
# F10 (bug hunt 2026-07-22): daily on-device access-code verification
# ===========================================================================


def _code_booking(days_to_checkin: int, *, ref: str = "ac-verify-1") -> Booking:
    b = _booking(
        external_id=f"HMVER{days_to_checkin}",
        check_in_date=TODAY + timedelta(days=days_to_checkin),
        check_out_date=TODAY + timedelta(days=days_to_checkin + 3),
        source_email_message_id=f"src-{uuid.uuid4()}",
    )
    b.tasks.append(
        BookingTask(
            task_type=TaskType.ACCESS_CODE_CREATE,
            state=TaskState.COMPLETE,
            external_ref=ref,
        )
    )
    return b


def _seam_code(status: str = "set", errors=None):
    code = MagicMock()
    code.status = status
    code.errors = errors or []
    return code


async def test_verify_access_codes_healthy_code_no_alert(db_session):
    from app.tasks import scheduled

    await _seed(_code_booking(2))
    client = MagicMock()
    client.access_codes.get.return_value = _seam_code("set")

    with (
        patch("app.tasks.scheduled.get_seam_client", return_value=client),
        patch("app.tasks.scheduled.send_access_code_problem_alert") as mock_alert,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.verify_access_codes()

    client.access_codes.get.assert_called_once_with(access_code_id="ac-verify-1")
    mock_alert.assert_not_called()


async def test_verify_access_codes_alerts_on_seam_errors(db_session):
    from app.tasks import scheduled

    await _seed(_code_booking(2))
    client = MagicMock()
    client.access_codes.get.return_value = _seam_code(
        "unset", errors=[{"error_code": "failed_to_set_on_device"}]
    )

    with (
        patch("app.tasks.scheduled.get_seam_client", return_value=client),
        patch("app.tasks.scheduled.send_access_code_problem_alert") as mock_alert,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.verify_access_codes()

    mock_alert.assert_called_once()
    items = mock_alert.call_args.args[0]
    assert any("failed_to_set_on_device" in i["problem"] for i in items)


async def test_verify_access_codes_alerts_when_not_set_on_final_day(db_session):
    """Within 1 day of check-in, anything other than status='set' is a problem
    — the guest arrives at 4 PM."""
    from app.tasks import scheduled

    await _seed(_code_booking(1))
    client = MagicMock()
    client.access_codes.get.return_value = _seam_code("setting")

    with (
        patch("app.tasks.scheduled.get_seam_client", return_value=client),
        patch("app.tasks.scheduled.send_access_code_problem_alert") as mock_alert,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.verify_access_codes()

    mock_alert.assert_called_once()


async def test_verify_access_codes_skips_far_future_bookings(db_session):
    from app.tasks import scheduled

    await _seed(_code_booking(10))
    client = MagicMock()

    with (
        patch("app.tasks.scheduled.get_seam_client", return_value=client),
        patch("app.tasks.scheduled.send_access_code_problem_alert") as mock_alert,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.verify_access_codes()

    client.access_codes.get.assert_not_called()
    mock_alert.assert_not_called()


async def test_verify_access_codes_alerts_when_fetch_fails(db_session):
    """A code Seam can't return (deleted / 404 / API error) is a problem —
    treat the fetch failure itself as reportable, not a silent skip."""
    from app.tasks import scheduled

    await _seed(_code_booking(2))
    client = MagicMock()
    client.access_codes.get.side_effect = RuntimeError("404 access_code not found")

    with (
        patch("app.tasks.scheduled.get_seam_client", return_value=client),
        patch("app.tasks.scheduled.send_access_code_problem_alert") as mock_alert,
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await scheduled.verify_access_codes()

    mock_alert.assert_called_once()
    items = mock_alert.call_args.args[0]
    assert any("404" in i["problem"] for i in items)


async def test_daily_jobs_log_heartbeats_on_idle_runs(db_session, caplog):
    """Ops observability: APScheduler silently discards missed runs, so the
    health check treats 'no log line in 24h' as 'job never ran'. That is only
    sound if every daily job logs a heartbeat even when it has nothing to do
    (previously all three were silent on idle runs)."""
    import logging

    from app.tasks import scheduled

    caplog.set_level(logging.INFO, logger="app.tasks.scheduled")
    with (
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
        patch("app.tasks.scheduled.get_seam_client", return_value=MagicMock()),
    ):
        await scheduled.check_daily_reminders()
        await scheduled.requeue_stalled_automations()
        await scheduled.verify_access_codes()

    assert "check_daily_reminders: run started" in caplog.text
    assert "requeue_stalled_automations: run started" in caplog.text
    assert "verify_access_codes: run started" in caplog.text


# ===========================================================================
# F15 (bug hunt 2026-07-22): terminal booking state
# ===========================================================================


async def test_complete_past_bookings_flips_active_to_completed_after_checkout(
    db_session,
):
    """Without a terminal state the dashboard's active list and every daily
    scan accumulate every booking forever. ACTIVE bookings whose checkout has
    passed flip to COMPLETED; a booking still checking out today, and a
    CANCELLED booking (already terminal), are left alone."""
    from app.tasks import scheduled

    past = _booking(
        external_id="HMPAST0002",
        source_email_message_id=f"src-{uuid.uuid4()}",
        check_in_date=TODAY - timedelta(days=10),
        check_out_date=TODAY - timedelta(days=5),
    )
    checking_out_today = _booking(
        external_id="HMTODAY0001",
        source_email_message_id=f"src-{uuid.uuid4()}",
        check_in_date=TODAY - timedelta(days=1),
        check_out_date=TODAY,
    )
    cancelled_past = _booking(
        external_id="HMCANCPAST1",
        source_email_message_id=f"src-{uuid.uuid4()}",
        check_in_date=TODAY - timedelta(days=10),
        check_out_date=TODAY - timedelta(days=5),
        status=BookingStatus.CANCELLED,
    )
    db_session.add_all([past, checking_out_today, cancelled_past])
    await db_session.commit()

    await scheduled.complete_past_bookings()

    reloaded_past, _ = await _reload(past.id)
    reloaded_today, _ = await _reload(checking_out_today.id)
    reloaded_cancelled, _ = await _reload(cancelled_past.id)

    assert reloaded_past.status == BookingStatus.COMPLETED
    assert reloaded_today.status == BookingStatus.ACTIVE
    assert reloaded_cancelled.status == BookingStatus.CANCELLED


async def test_complete_past_bookings_logs_heartbeat_on_idle_run(db_session, caplog):
    import logging

    from app.tasks import scheduled

    caplog.set_level(logging.INFO, logger="app.tasks.scheduled")
    await scheduled.complete_past_bookings()

    assert "complete_past_bookings: run started" in caplog.text


# ===========================================================================
# Sustainability audit 2026-07-23 — classifier-drift digest & monthly status
# ===========================================================================


async def _insert_processed(session, **overrides) -> None:
    defaults = dict(
        message_id=f"pm-{uuid.uuid4()}",
        disposition="other",
        sender="automated@airbnb.com",
        subject="Some subject",
    )
    defaults.update(overrides)
    session.add(ProcessedMessage(**defaults))
    await session.commit()


@pytest.mark.asyncio
async def test_classifier_drift_digest_sends_for_platform_other_rows(db_session):
    """Only OTHER rows from platform domains within the last 7 days make the
    digest; other senders, older rows, and non-other dispositions do not."""
    from app.tasks.scheduled import check_classifier_drift

    await _insert_processed(
        db_session, sender="automated@airbnb.com", subject="Reservation update?"
    )
    await _insert_processed(db_session, sender="noreply@example.com")
    await _insert_processed(
        db_session,
        sender="no-reply@vrbo.com",
        created_at=datetime.now(UTC) - timedelta(days=10),
    )
    await _insert_processed(
        db_session, sender="automated@airbnb.com", disposition="parse_error"
    )

    send = MagicMock()
    with (
        patch("app.tasks.scheduled.send_classifier_drift_digest", send),
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await check_classifier_drift()

    send.assert_called_once()
    items = send.call_args.kwargs["items"]
    assert len(items) == 1
    assert items[0]["sender"] == "automated@airbnb.com"
    assert items[0]["subject"] == "Reservation update?"


@pytest.mark.asyncio
async def test_classifier_drift_digest_silent_when_no_suspects(db_session):
    from app.tasks.scheduled import check_classifier_drift

    await _insert_processed(db_session, sender="newsletter@example.com")

    send = MagicMock()
    with (
        patch("app.tasks.scheduled.send_classifier_drift_digest", send),
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await check_classifier_drift()

    send.assert_not_called()


@pytest.mark.asyncio
async def test_monthly_status_email_sends_report_with_stats(db_session):
    from app.tasks.scheduled import send_monthly_status_email

    booking = _booking()
    db_session.add(booking)
    await db_session.commit()

    send = MagicMock()
    with (
        patch("app.tasks.scheduled.send_monthly_status_report", send),
        patch("app.tasks.scheduled.get_alerts_service", return_value=MagicMock()),
    ):
        await send_monthly_status_email()

    send.assert_called_once()
    stats = send.call_args.kwargs["stats"]
    assert stats["active_bookings"] == 1
    for key in (
        "completed_last_30d",
        "failed_tasks",
        "stuck_in_progress",
        "dead_letters_other_30d",
        "dead_letters_error_30d",
        "docusign_token_store_age_days",
    ):
        assert key in stats
