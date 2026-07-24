"""Step 17 — systematic failure-mode tests across all integration points.

Each test drives one failure class against the real local Postgres test DB with
external HTTP boundaries mocked, and asserts the system's behavior (graceful
failure, task FAILED + last_error, idempotency, or consistent state) rather than
a silent failure / inconsistency.

Failure classes (per the Step 17 runbook prompt):
  1. Network failure / timeout
  2. Invalid/expired credentials
  3. Unexpected data
  4. Duplicate events (idempotency)
  5. Partial failure mid-dispatch

The app's ``AsyncSessionLocal`` binds to the same local test DB the ``db_session``
fixture builds the schema in (see tests/conftest.py), so a trigger/dispatcher
opening its own session sees what the test committed.
"""
from __future__ import annotations

import asyncio
import base64
import uuid
from datetime import date, timedelta
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
    TaskState,
    TaskType,
)
from app.db.session import AsyncSessionLocal, engine as app_engine

TODAY = date.today()
from tests.conftest import recorded_email_bytes


@pytest_asyncio.fixture(autouse=True)
async def _dispose_app_engine():
    """Keep the app engine's asyncpg pool loop-local (see test_triggers_isolation)."""
    yield
    await app_engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _booking(**overrides) -> Booking:
    defaults = dict(
        id=uuid.uuid4(),
        platform=Platform.AIRBNB,
        external_id=f"HM{uuid.uuid4().hex[:8].upper()}",
        property_id="test_property",
        guest_first_name="Fail",
        guest_last_name="Mode",
        guest_phone="+15551234567",
        guest_email="fail@example.com",
        check_in_date=TODAY + timedelta(days=30),
        check_out_date=TODAY + timedelta(days=34),
        status=BookingStatus.ACTIVE,
        source_email_message_id=f"src-{uuid.uuid4()}",
    )
    defaults.update(overrides)
    return Booking(**defaults)


async def _seed(booking: Booking) -> uuid.UUID:
    async with AsyncSessionLocal() as s:
        s.add(booking)
        await s.commit()
        return booking.id


async def _reload_tasks(booking_id: uuid.UUID) -> dict[TaskType, BookingTask]:
    async with AsyncSessionLocal() as s:
        booking = (
            await s.execute(
                select(Booking).where(Booking.id == booking_id).options(selectinload(Booking.tasks))
            )
        ).scalar_one()
        return {t.task_type: t for t in booking.tasks}


async def _booking_count() -> int:
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(func.count(Booking.id)))).scalar_one()


def _gmail_service(msg_id: str, raw_bytes: bytes) -> MagicMock:
    """Fake booking-feed Gmail service returning one message with the given raw bytes."""
    service = MagicMock()
    m = service.users.return_value.messages.return_value
    m.list.return_value.execute.return_value = {"messages": [{"id": msg_id}]}
    m.get.return_value.execute.return_value = {
        "raw": base64.urlsafe_b64encode(raw_bytes).decode()
    }
    return service


# ===========================================================================
# Class 5 — Partial failure / duplicate dispatch (R2): concurrent dispatch must
# not run a task's handler (and its external side effect) more than once.
# ===========================================================================


async def test_concurrent_dispatch_runs_each_handler_exactly_once(db_session):
    """Two _dispatch_pending_tasks racing on the same booking (double contact-info
    POST / refresh) must each task's handler run EXACTLY once — otherwise a
    duplicate DocuSign envelope and Seam code are created (R2). The atomic
    PENDING->IN_PROGRESS claim guarantees single execution across dispatches."""
    from app.tasks.dispatch import _dispatch_pending_tasks

    booking = _booking()
    booking.tasks.append(BookingTask(task_type=TaskType.CLEANER_SHEET_ADD, state=TaskState.PENDING))
    booking.tasks.append(BookingTask(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.PENDING))
    booking.tasks.append(BookingTask(task_type=TaskType.ACCESS_CODE_CREATE, state=TaskState.PENDING))
    booking_id = await _seed(booking)

    calls = {"cleaner": 0, "docusign": 0, "seam": 0}

    async def h_cleaner(bk, task, session):
        calls["cleaner"] += 1
        await asyncio.sleep(0.25)  # widen the race window
        task.state = TaskState.COMPLETE

    async def h_docusign(bk, task, session):
        calls["docusign"] += 1
        await asyncio.sleep(0.25)
        task.external_ref = f"env-{calls['docusign']}"
        task.state = TaskState.COMPLETE

    async def h_seam(bk, task, session):
        calls["seam"] += 1
        await asyncio.sleep(0.25)
        task.external_ref = f"code-{calls['seam']}"
        task.state = TaskState.COMPLETE

    async def h_hoa(bk, task, session):
        task.state = TaskState.COMPLETE

    with (
        patch("app.tasks.dispatch.handle_cleaner_sheet", h_cleaner),
        patch("app.tasks.dispatch.handle_docusign_send", h_docusign),
        patch("app.tasks.dispatch.handle_access_code_create", h_seam),
        patch("app.tasks.dispatch.handle_hoa_email", h_hoa),
    ):
        await asyncio.gather(
            _dispatch_pending_tasks(booking_id),
            _dispatch_pending_tasks(booking_id),
        )

    assert calls["cleaner"] == 1, f"CLEANER_SHEET_ADD ran {calls['cleaner']}x (want 1)"
    assert calls["docusign"] == 1, f"DOCUSIGN_SEND ran {calls['docusign']}x (want 1) — duplicate envelope!"
    assert calls["seam"] == 1, f"ACCESS_CODE_CREATE ran {calls['seam']}x (want 1) — duplicate code!"

    tasks = await _reload_tasks(booking_id)
    assert tasks[TaskType.DOCUSIGN_SEND].state == TaskState.COMPLETE
    assert tasks[TaskType.ACCESS_CODE_CREATE].state == TaskState.COMPLETE
    assert tasks[TaskType.CLEANER_SHEET_ADD].state == TaskState.COMPLETE


# ===========================================================================
# Class 4 — Duplicate events: the same Gmail message id polled twice creates
# exactly one booking (dedup via bookings.source_email_message_id + the UNIQUE
# constraint as a backstop).
# ===========================================================================


async def test_poller_duplicate_message_id_creates_one_booking(db_session):
    from app.ingestion.poller import poll_booking_feed

    raw = recorded_email_bytes("airbnbbooking-1.eml")
    service = _gmail_service("dup-msg-1", raw)

    with patch("app.integrations.gmail.oauth.get_booking_feed_service", return_value=service):
        await poll_booking_feed()   # first: creates the booking
        await poll_booking_feed()   # second: same id is already-seen → skipped

    assert await _booking_count() == 1


# ===========================================================================
# Class 1/2 — Gmail READ unreachable / auth failure: poll aborts gracefully,
# no crash, no partial writes.
# ===========================================================================


async def test_poller_gmail_auth_failure_is_graceful(db_session):
    from app.ingestion.poller import poll_booking_feed

    with patch(
        "app.integrations.gmail.oauth.get_booking_feed_service",
        side_effect=RuntimeError("gmail unreachable / invalid_grant"),
    ):
        await poll_booking_feed()   # must NOT raise

    assert await _booking_count() == 0


# ===========================================================================
# Class 3 — Unexpected data: a booking email that classifies as Airbnb but is
# missing required fields raises a parser error that the poll loop isolates
# (logged, no booking, no crash). NOTE: the message is NOT recorded, so it is
# re-polled on the next run (documented replay limitation — see Session Log).
# ===========================================================================


async def test_poller_unparseable_booking_email_is_isolated(db_session):
    from app.ingestion.poller import poll_booking_feed

    # Valid Airbnb sender+subject (classifies as AIRBNB_BOOKING) but no
    # CONFIRMATION CODE section → parse_airbnb_booking raises AirbnbParseError.
    raw = (
        b"From: Airbnb <automated@airbnb.com>\r\n"
        b"Subject: Reservation confirmed - Jane Smith arrives Jul 15\r\n"
        b"Date: Thu, 28 May 2026 12:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"This email is missing the confirmation code and dates.\r\n"
    )
    service = _gmail_service("bad-msg-1", raw)

    with patch("app.integrations.gmail.oauth.get_booking_feed_service", return_value=service):
        await poll_booking_feed()   # must NOT raise despite the parse error

    assert await _booking_count() == 0


# ===========================================================================
# Class 5 — Partial failure mid-dispatch: one handler raising must NOT abort the
# others; the failing task goes FAILED with last_error + attempt_count, and the
# rest still COMPLETE (dispatcher failure isolation, persisted to the real DB).
# ===========================================================================


async def test_dispatch_isolates_one_failing_task(db_session):
    from app.tasks.dispatch import _dispatch_pending_tasks

    booking = _booking()
    booking.tasks.append(BookingTask(task_type=TaskType.CLEANER_SHEET_ADD, state=TaskState.PENDING))
    booking.tasks.append(BookingTask(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.PENDING))
    booking.tasks.append(BookingTask(task_type=TaskType.ACCESS_CODE_CREATE, state=TaskState.PENDING))
    booking_id = await _seed(booking)

    async def ok(bk, task, session):
        task.state = TaskState.COMPLETE

    async def boom(bk, task, session):
        raise RuntimeError("docusign 500 / network timeout")

    with (
        patch("app.tasks.dispatch.handle_cleaner_sheet", ok),
        patch("app.tasks.dispatch.handle_docusign_send", boom),
        patch("app.tasks.dispatch.handle_access_code_create", ok),
        patch("app.tasks.dispatch.handle_hoa_email", ok),
    ):
        await _dispatch_pending_tasks(booking_id)

    tasks = await _reload_tasks(booking_id)
    assert tasks[TaskType.CLEANER_SHEET_ADD].state == TaskState.COMPLETE
    assert tasks[TaskType.ACCESS_CODE_CREATE].state == TaskState.COMPLETE
    ds = tasks[TaskType.DOCUSIGN_SEND]
    assert ds.state == TaskState.FAILED
    assert ds.last_error and "network timeout" in ds.last_error
    assert ds.attempt_count == 1


async def test_dispatch_survives_handler_that_aborts_the_transaction(db_session):
    """F12 (bug hunt 2026-07-22): a handler failing with a DB error leaves the
    session's transaction ABORTED. The old except block committed without a
    rollback first -> PendingRollbackError escaped the dispatcher: the failure
    was never recorded (task stranded IN_PROGRESS — claimed, never re-
    dispatched) and every later task in the dispatch order was skipped."""
    from sqlalchemy import text

    from app.tasks.dispatch import _dispatch_pending_tasks

    booking = _booking()
    booking.tasks.append(BookingTask(task_type=TaskType.CLEANER_SHEET_ADD, state=TaskState.PENDING))
    booking.tasks.append(BookingTask(task_type=TaskType.DOCUSIGN_SEND, state=TaskState.PENDING))
    booking.tasks.append(BookingTask(task_type=TaskType.ACCESS_CODE_CREATE, state=TaskState.PENDING))
    booking_id = await _seed(booking)

    async def ok(bk, task, session):
        task.state = TaskState.COMPLETE

    async def aborts_transaction(bk, task, session):
        # A failed statement poisons the transaction exactly like a mid-flush
        # IntegrityError or a dropped connection would.
        await session.execute(text("SELECT * FROM table_that_does_not_exist"))

    with (
        patch("app.tasks.dispatch.handle_cleaner_sheet", ok),
        patch("app.tasks.dispatch.handle_docusign_send", aborts_transaction),
        patch("app.tasks.dispatch.handle_access_code_create", ok),
        patch("app.tasks.dispatch.handle_hoa_email", ok),
    ):
        await _dispatch_pending_tasks(booking_id)  # must not raise

    tasks = await _reload_tasks(booking_id)
    # The failure is durably recorded — NOT stranded IN_PROGRESS.
    ds = tasks[TaskType.DOCUSIGN_SEND]
    assert ds.state == TaskState.FAILED
    assert ds.last_error is not None
    assert ds.attempt_count == 1
    # Later tasks in the dispatch order still ran.
    assert tasks[TaskType.ACCESS_CODE_CREATE].state == TaskState.COMPLETE
