"""Go-live item 7 orchestration. Runs INSIDE the app container (live prod DB,
prod creds, real config.yaml with the HOA email TEMPORARILY overridden to an
inbox we own). Modes:

  send      persist a fake in-window booking (poller path), enter contact info,
            dispatch -> REAL DocuSign envelope to the guest inbox, REAL
            future-dated Seam code on the real lock, REAL cleaner-sheet row.
            Prints the envelope id, then STOPS so the human can sign.
  verify    print booking + task state (run after signing + the real webhook).
  teardown  void envelope (no-op if already completed), delete Seam code,
            delete sheet row, delete PDF, delete DB booking (+cascade).

Guest signer = an inbox we own that is NOT the DocuSign account owner address
(item 7 verifies prod does not self-suppress). HOA recipient must NOT be the
real HOA — send() aborts unless config points at E2E_HOA_RECIPIENT.

Required environment (no defaults — this script sends real email):
  E2E_GUEST_EMAIL     inbox that receives the real DocuSign signing request
  E2E_HOA_RECIPIENT   the test inbox config.yaml's HOA email must be set to
"""
from __future__ import annotations

import asyncio
import email as email_lib
import os
import sys
import uuid
from datetime import date
from email import policy
from pathlib import Path

from sqlalchemy import delete, select

from app.config import load_config
from app.db.models import Booking, Platform, TaskState, TaskType
from app.db.session import AsyncSessionLocal
from app.ingestion.cancellation import delete_seam_access_code
from app.ingestion.parsers.airbnb import AirbnbBookingData
from app.ingestion.poller import persist_booking
from app.tasks.dispatch import _dispatch_pending_tasks
from app.tasks.handlers.docusign import void_envelope_idempotent

EXTID = "ZZGOLIVE01"
FIRST = "ZZGolive"
LAST = "Testguest"                    # unique teardown marker in the sheet
GUEST_PHONE = "5550000007"
CHECK_IN = date(2026, 7, 25)          # in HOA window for 2026-07-21; code activates 4pm 07-25
CHECK_OUT = date(2026, 7, 28)


def _require_env(name: str, purpose: str) -> str:
    """Read a required address from the environment. No default: this script
    sends real email, so an unset variable must stop the run, not silently
    fall back to somebody's personal inbox."""
    value = (os.environ.get(name) or "").strip()
    if not value:
        sys.exit(
            f"*** {name} is not set. ***\n"
            f"    {purpose}\n"
            f"    Set it in .env (see .env.template) or export it before running:\n"
            f"        {name}=someone@example.com .venv/bin/python {sys.argv[0]} ...\n"
        )
    return value


async def _get():
    async with AsyncSessionLocal() as s:
        b = (await s.execute(select(Booking).where(Booking.external_id == EXTID))).scalar_one_or_none()
        if not b:
            return None
        await s.refresh(b, attribute_names=["tasks"])
        return b


async def _state(label):
    b = await _get()
    if not b:
        print(f"[{label}] booking {EXTID} NOT FOUND")
        return None
    print(f"[{label}] {b.external_id} status={b.status.value} "
          f"email={b.guest_email} phone={b.guest_phone} "
          f"signed_pdf={'YES' if b.signed_pdf_path else 'no'}")
    for t in sorted(b.tasks, key=lambda t: t.task_type.value):
        ref = f" ref={t.external_ref}" if t.external_ref else ""
        err = f" err={t.last_error!r}" if t.last_error else ""
        print(f"    {t.task_type.value:34s} {t.state.value:12s}{ref}{err}")
    return b


async def send():
    guest_email = _require_env(
        "E2E_GUEST_EMAIL",
        "Inbox that receives the REAL DocuSign signing request. Must be an inbox "
        "you own, and must NOT be the DocuSign account owner address (demo "
        "DocuSign suppresses the signing email when signer == account owner).",
    )
    hoa_recipient = _require_env(
        "E2E_HOA_RECIPIENT",
        "Test inbox that config.yaml's HOA email must be overridden to before "
        "this run, so the live HOA send cannot reach the real HOA.",
    )
    hoa = (load_config().properties[0].hoa.email or "")
    print(f"HOA recipient in config: {hoa}")
    # Allowlist, not denylist: proceed only if config points at the declared test
    # inbox. Anything else — including the real HOA, or an unset value — aborts.
    if hoa.strip().lower() != hoa_recipient.strip().lower():
        print(f"*** ABORT: config HOA email is {hoa!r}, but E2E_HOA_RECIPIENT is "
              f"{hoa_recipient!r}. Override config.yaml first — this run sends a "
              f"real HOA email and must never reach the real HOA. ***")
        return
    if await _get():
        print(f"*** booking {EXTID} already exists — run teardown first. ***")
        return

    parsed = AirbnbBookingData(
        confirmation_code=EXTID, guest_first_name=FIRST, guest_last_name=LAST,
        check_in_date=CHECK_IN, check_out_date=CHECK_OUT,
    )
    msg_id = f"golive-e2e-{EXTID}-{uuid.uuid4()}"
    raw = ("From: Airbnb <automated@airbnb.com>\r\n"
           f"Subject: Reservation confirmed - {FIRST} {LAST}\r\n"
           "Content-Type: text/plain\r\n\r\nsynthetic go-live test\r\n")
    msg = email_lib.message_from_string(raw, policy=policy.default)

    print("\n[1] persist_booking (poller path; sends a new-booking alert)")
    await persist_booking(msg_id, msg, parsed, Platform.AIRBNB)
    b = await _state("after persist")

    print("\n[2] enter contact info -> flip WAITING->PENDING")
    async with AsyncSessionLocal() as s:
        bb = (await s.execute(select(Booking).where(Booking.id == b.id))).scalar_one()
        bb.guest_email = guest_email
        bb.guest_phone = GUEST_PHONE
        await s.refresh(bb, attribute_names=["tasks"])
        for t in bb.tasks:
            if t.task_type in (TaskType.DOCUSIGN_SEND, TaskType.ACCESS_CODE_CREATE) \
                    and t.state == TaskState.WAITING:
                t.state = TaskState.PENDING
        await s.commit()
    await _state("after contact")

    print("\n[3] dispatch -> REAL envelope, REAL Seam code, REAL sheet row")
    await _dispatch_pending_tasks(b.id)
    b = await _state("after dispatch")
    by = {t.task_type: t for t in b.tasks}
    env = by[TaskType.DOCUSIGN_SEND].external_ref
    print(f"\n>>> Envelope sent: {env}")
    print(f">>> Sign the DocuSign email at {guest_email}, then we run: verify")


async def verify():
    await _state("VERIFY (after signing + webhook)")


async def teardown():
    b = await _get()
    if not b:
        print("no booking to tear down")
        return
    by = {t.task_type: t for t in b.tasks}
    env = by.get(TaskType.DOCUSIGN_SEND).external_ref if TaskType.DOCUSIGN_SEND in by else None
    seam = by.get(TaskType.ACCESS_CODE_CREATE).external_ref if TaskType.ACCESS_CODE_CREATE in by else None
    bid, signed = b.id, b.signed_pdf_path

    if env:
        try:
            void_envelope_idempotent(env)
            print(f"voided envelope {env}")
        except Exception as e:
            print(f"void skipped/failed (expected if completed): {e}")
    if seam:
        try:
            delete_seam_access_code(seam)
            print(f"deleted Seam code {seam}")
        except Exception as e:
            print(f"seam delete failed: {e}")
    try:
        from app.integrations.sheets.client import _find_sheet_id, get_sheets_service
        prop = load_config().properties[0]
        sid, name = prop.cleaner_schedule.spreadsheet_id, prop.cleaner_schedule.sheet_name
        svc = get_sheets_service()
        rows = svc.spreadsheets().values().get(spreadsheetId=sid, range=name).execute().get("values", [])
        hits = [i for i, r in enumerate(rows) if any(LAST in str(c) for c in r)]
        if hits:
            sheet_id = _find_sheet_id(svc.spreadsheets().get(spreadsheetId=sid).execute(), name)
            for i in sorted(hits, reverse=True):
                svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [
                    {"deleteDimension": {"range": {"sheetId": sheet_id, "dimension": "ROWS",
                                                   "startIndex": i, "endIndex": i + 1}}}]}).execute()
            print(f"deleted {len(hits)} sheet row(s) matching {LAST!r}")
        else:
            print("no sheet rows matched")
    except Exception as e:
        print(f"sheet cleanup failed: {e}")
    if signed:
        try:
            p = Path(signed)
            if p.exists():
                p.unlink()
                print(f"removed PDF {p}")
        except Exception as e:
            print(f"pdf cleanup failed: {e}")
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Booking).where(Booking.id == bid))
        await s.commit()
    print(f"deleted booking {bid} (+cascade)")


MODES = {"send": send, "verify": verify, "teardown": teardown}

if __name__ == "__main__":
    m = sys.argv[1] if len(sys.argv) > 1 else ""
    if m not in MODES:
        print("usage: golive_e2e.py {send|verify|teardown}")
        sys.exit(1)
    asyncio.run(MODES[m]())
