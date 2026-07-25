"""Step 16 orchestration: drive the full happy path against real sandbox APIs,
printing intermediate DB state after each step, for BOTH HOA-window branches.

This complements tests/e2e/test_full_booking_workflow.py (the pass/fail go-live
gate) by making every intermediate state and external side effect visible, and
by exercising the in-window immediate HOA send — the path fixed in Step 2 in
handle_envelope_completed — which the gate test does not cover (it uses a
far-future check-in and never simulates the DocuSign completed webhook).

Modes (argv[1]):

  far_future           One-shot: check-in far out; HOA_EMAIL must stay WAITING;
                       no signing. Runs send + dispatch + assert + teardown.

  in_window_send       Persist the in-window booking, enter contact info, and
                       dispatch (cleaner sheet, DocuSign envelope, Seam code).
                       Then STOP — leaves everything live so the guest (you) can
                       sign the real DocuSign email whenever it arrives. Prints
                       the envelope id. NO teardown.

  in_window_complete   Run AFTER you've signed. Looks up the in-window booking,
                       checks the envelope reached 'completed', then fires
                       handle_envelope_completed (real signed-PDF download +
                       real in-window HOA email, recipient overridden to a test
                       inbox — never the real HOA), verifies HOA_EMAIL=COMPLETE,
                       and tears everything down. If not yet signed, it says so
                       and tears down NOTHING, so you can sign and re-run.

The two-stage in_window flow exists because the DocuSign DEMO tier batches/delays
the signing-request email by several minutes, so a fixed poll-then-teardown races
the email + human signing. In production these emails deliver in seconds.

Required environment (no defaults — the in_window modes send real email):
  E2E_GUEST_EMAIL     inbox that receives the real DocuSign signing request.
                      Must NOT be the DocuSign account owner address — demo
                      DocuSign suppresses the signing email when signer ==
                      account owner.
  E2E_HOA_RECIPIENT   test inbox the HOA send is redirected to, so the live HOA
                      email can never reach the real HOA.
Both are read from .env (see .env.template) or the process environment.

Usage (run on the host that holds .env):
    .venv/bin/python scripts/manual/e2e_full_flow.py far_future
    .venv/bin/python scripts/manual/e2e_full_flow.py in_window_send
    # ... sign the DocuSign email at $E2E_GUEST_EMAIL ...
    .venv/bin/python scripts/manual/e2e_full_flow.py in_window_complete
"""
from __future__ import annotations

# --- Environment MUST be set before importing app modules --------------------
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://rental_automation:devpassword@127.0.0.1:5432/rental_automation_test",
)

from dotenv import dotenv_values  # noqa: E402

for _k, _v in dotenv_values(".env").items():
    # Load real sandbox creds, but never let .env's DATABASE_URL (docker host)
    # override the local test DB set above.
    if _k != "DATABASE_URL" and _v is not None and _k not in os.environ:
        os.environ[_k] = _v

# --- Now safe to import app + third-party -----------------------------------
import asyncio  # noqa: E402
import email as email_lib  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import uuid  # noqa: E402
from datetime import date  # noqa: E402
from email import policy  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

from sqlalchemy import delete, select  # noqa: E402

from app.config import load_config as _real_load_config  # noqa: E402
from app.db.models import Booking, Platform, TaskState, TaskType  # noqa: E402
from app.db.session import AsyncSessionLocal, Base, engine  # noqa: E402
from app.ingestion.cancellation import delete_seam_access_code  # noqa: E402
from app.ingestion.parsers.airbnb import AirbnbBookingData  # noqa: E402
from app.ingestion.poller import persist_booking  # noqa: E402
from app.integrations.docusign.client import get_envelope_api  # noqa: E402
from app.tasks.dispatch import _dispatch_pending_tasks  # noqa: E402
from app.tasks.handlers.docusign import (  # noqa: E402
    handle_envelope_completed,
    void_envelope_idempotent,
)


def _require_env(name: str, purpose: str) -> str:
    """Read a required address from the environment. No default: these modes
    send real email, so an unset variable must stop the run rather than silently
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


def _hoa_test_recipient() -> str:
    """HOA recipient override — the real config points at the real HOA. For the
    live in-window proof we redirect to a test inbox the operator owns. Resolved
    lazily so the far_future mode, which sends no HOA email, needs no env var."""
    return _require_env(
        "E2E_HOA_RECIPIENT",
        "Test inbox the live in-window HOA send is redirected to, so it can "
        "never reach the real HOA.",
    )

# Temp dir for the signed PDF (the handler hardcodes /app/data/pdfs, absent on host).
_PDF_DIR = Path(tempfile.mkdtemp(prefix="e2e_pdfs_"))


def _pdf_path_override(booking) -> Path:
    return _PDF_DIR / f"{booking.id}.pdf"


def _load_config_hoa_override():
    """Return the real config with the HOA recipient swapped to the test inbox."""
    cfg = _real_load_config()
    hoa = cfg.properties[0].hoa
    recipient = _hoa_test_recipient()
    try:
        hoa.email = recipient
    except Exception:
        object.__setattr__(hoa, "email", recipient)
    return cfg


SCENARIOS = {
    "far_future": dict(
        external_id="ZZFAR0001",
        first="ZZE2E",
        last="FarFuture",
        check_in=date(2026, 9, 1),
        check_out=date(2026, 9, 5),
        guest_email="e2e-farfuture@example.com",  # no signing needed
        expect_hoa=TaskState.WAITING,
    ),
    "in_window": dict(
        external_id="ZZWIN0001",
        first="ZZE2E",
        last="InWindow",
        check_in=date(2026, 7, 6),   # window [2026-06-29, 2026-07-04] contains today
        check_out=date(2026, 7, 9),
        # Signer must NOT be the DocuSign developer account's own email — demo
        # DocuSign suppresses the signing email when signer == account owner.
        # None => read E2E_GUEST_EMAIL at run time (see _resolved).
        guest_email=None,
        expect_hoa=TaskState.COMPLETE,
    ),
}

GUEST_PHONE = "5550000001"  # last-4 "0001": Seam sandbox isolation suffix


def _resolved(name: str) -> dict:
    """A scenario with its lazy fields filled in from the environment."""
    sc = dict(SCENARIOS[name])
    if sc["guest_email"] is None:
        sc["guest_email"] = _require_env(
            "E2E_GUEST_EMAIL",
            "Inbox that receives the REAL DocuSign signing request. Must be an "
            "inbox you own and must NOT be the DocuSign account owner address.",
        )
    return sc


async def print_state(label: str, booking_id) -> None:
    async with AsyncSessionLocal() as s:
        b = (await s.execute(select(Booking).where(Booking.id == booking_id))).scalar_one_or_none()
        if b is None:
            print(f"\n--- STATE @ {label}: booking {booking_id} NOT FOUND ---")
            return
        await s.refresh(b, attribute_names=["tasks"])
        print(f"\n--- STATE @ {label} ---")
        print(
            f"  booking {b.external_id}  status={b.status.value}  "
            f"signed_pdf={'YES' if b.signed_pdf_path else 'no'}  "
            f"email={b.guest_email}  phone={b.guest_phone}"
        )
        for t in sorted(b.tasks, key=lambda t: t.task_type.value):
            ref = f"  ref={t.external_ref}" if t.external_ref else ""
            err = f"  err={t.last_error!r}" if t.last_error else ""
            print(f"    {t.task_type.value:36s} {t.state.value:12s}{ref}{err}")


async def ensure_schema() -> None:
    import app.db.models  # noqa: F401 — register mappers
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _booking_refs(external_id: str):
    """Return (booking_id, envelope_id, seam_code_id) for a persisted booking."""
    async with AsyncSessionLocal() as s:
        b = (
            await s.execute(select(Booking).where(Booking.external_id == external_id))
        ).scalar_one_or_none()
        if b is None:
            return None
        await s.refresh(b, attribute_names=["tasks"])
        by = {t.task_type: t for t in b.tasks}
        return (
            b.id,
            by[TaskType.DOCUSIGN_SEND].external_ref,
            by[TaskType.ACCESS_CODE_CREATE].external_ref,
        )


async def _persist_contact_dispatch(sc) -> None:
    """Shared: STEP 1 persist, STEP 2 contact entry, STEP 3 dispatch (+state prints)."""
    parsed = AirbnbBookingData(
        confirmation_code=sc["external_id"],
        guest_first_name=sc["first"],
        guest_last_name=sc["last"],
        check_in_date=sc["check_in"],
        check_out_date=sc["check_out"],
    )
    msg_id = f"e2e-orch-{sc['external_id']}-{uuid.uuid4()}"
    raw = (
        "From: Airbnb <automated@airbnb.com>\r\n"
        f"Subject: Reservation confirmed - {sc['first']} {sc['last']}\r\n"
        "Content-Type: text/plain\r\n\r\nsynthetic\r\n"
    )
    msg = email_lib.message_from_string(raw, policy=policy.default)

    print("\n[STEP 1] persist_booking (poller path) — side effect: new-booking alert email")
    await persist_booking(msg_id, msg, parsed, Platform.AIRBNB)
    booking_id = (await _booking_refs(sc["external_id"]))[0]
    await print_state("after persist (booking + 11 tasks)", booking_id)

    print("\n[STEP 2] owner enters contact info (email+phone) -> flip WAITING->PENDING")
    async with AsyncSessionLocal() as s:
        b = (await s.execute(select(Booking).where(Booking.id == booking_id))).scalar_one()
        b.guest_email = sc["guest_email"]
        b.guest_phone = GUEST_PHONE
        await s.refresh(b, attribute_names=["tasks"])
        for t in b.tasks:
            if t.task_type in (TaskType.DOCUSIGN_SEND, TaskType.ACCESS_CODE_CREATE):
                if t.state == TaskState.WAITING:
                    t.state = TaskState.PENDING
        await s.commit()
    await print_state("after contact entry", booking_id)

    print("\n[STEP 3] dispatch PENDING tasks (cleaner sheet, DocuSign envelope, Seam code)")
    await _dispatch_pending_tasks(booking_id)
    await print_state("after dispatch", booking_id)
    _, env, seam = await _booking_refs(sc["external_id"])
    print(f"  external side effects: envelope_id={env}  seam_code_id={seam}")


async def _teardown(sc, booking_id, envelope_id, seam_code_id) -> None:
    print("\n[TEARDOWN] removing all artifacts")
    try:
        if envelope_id:
            void_envelope_idempotent(envelope_id)
            print(f"  voided envelope {envelope_id}")
    except Exception as exc:
        print(f"  envelope void failed: {exc}")
    try:
        if seam_code_id:
            delete_seam_access_code(seam_code_id)
            print(f"  deleted Seam code {seam_code_id}")
    except Exception as exc:
        print(f"  Seam delete failed: {exc}")
    try:
        from app.integrations.sheets.client import _find_sheet_id, get_sheets_service
        prop = _real_load_config().properties[0]
        sid = prop.cleaner_schedule.spreadsheet_id
        name = prop.cleaner_schedule.sheet_name
        svc = get_sheets_service()
        rows = (
            svc.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=name)
            .execute()
            .get("values", [])
        )
        marker = sc["last"]
        hits = [i for i, r in enumerate(rows) if any(marker in str(c) for c in r)]
        if hits:
            sheet_id = _find_sheet_id(svc.spreadsheets().get(spreadsheetId=sid).execute(), name)
            for i in sorted(hits, reverse=True):
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=sid,
                    body={"requests": [{"deleteDimension": {"range": {
                        "sheetId": sheet_id, "dimension": "ROWS",
                        "startIndex": i, "endIndex": i + 1,
                    }}}]},
                ).execute()
            print(f"  deleted {len(hits)} sheet row(s) matching {marker!r}")
    except Exception as exc:
        print(f"  sheet cleanup failed: {exc}")
    try:
        if booking_id:
            p = _pdf_path_override(type("B", (), {"id": booking_id})())
            if p.exists():
                p.unlink()
                print(f"  removed PDF {p}")
    except Exception as exc:
        print(f"  PDF cleanup failed: {exc}")
    try:
        if booking_id:
            async with AsyncSessionLocal() as s:
                await s.execute(delete(Booking).where(Booking.id == booking_id))
                await s.commit()
            print(f"  deleted booking {booking_id} from DB")
    except Exception as exc:
        print(f"  DB delete failed: {exc}")


async def run_far_future() -> None:
    sc = SCENARIOS["far_future"]
    print("\n================ SCENARIO: far_future (HOA must stay WAITING) ================")
    await ensure_schema()
    booking_id = envelope_id = seam_code_id = None
    try:
        await _persist_contact_dispatch(sc)
        booking_id, envelope_id, seam_code_id = await _booking_refs(sc["external_id"])
        print("\n[STEP 4] far-future: HOA_EMAIL should remain WAITING (window not open)")
        async with AsyncSessionLocal() as s:
            b = (await s.execute(select(Booking).where(Booking.id == booking_id))).scalar_one()
            await s.refresh(b, attribute_names=["tasks"])
            by = {t.task_type: t for t in b.tasks}
        ok = by[TaskType.HOA_EMAIL].state == sc["expect_hoa"]
        print(f"\n[RESULT] HOA_EMAIL={by[TaskType.HOA_EMAIL].state.value}  "
              f"expected={sc['expect_hoa'].value}  {'PASS' if ok else 'FAIL'}")
        print(f"[RESULT] CLEANER_SHEET_ADD={by[TaskType.CLEANER_SHEET_ADD].state.value}  "
              f"DOCUSIGN_SEND={by[TaskType.DOCUSIGN_SEND].state.value}  "
              f"ACCESS_CODE_CREATE={by[TaskType.ACCESS_CODE_CREATE].state.value}")
    finally:
        await _teardown(sc, booking_id, envelope_id, seam_code_id)
        await engine.dispose()


async def run_in_window_send() -> None:
    sc = _resolved("in_window")
    print("\n================ SCENARIO: in_window_send (leaves everything LIVE) ================")
    print(
        f"  check-in {sc['check_in']}  ->  "
        f"after signing, expect HOA_EMAIL = {sc['expect_hoa'].value}"
    )
    await ensure_schema()
    # If a stale in_window booking exists, refuse (avoid UNIQUE collision / confusion).
    if await _booking_refs(sc["external_id"]) is not None:
        print(
            f"\nA booking {sc['external_id']} already exists. "
            "Run in_window_complete (or clean up) first."
        )
        await engine.dispose()
        return
    await _persist_contact_dispatch(sc)
    _, env, _ = await _booking_refs(sc["external_id"])
    print("\n[STEP 4] SIGN NOW — no time pressure")
    print(f"    DocuSign envelope {env} was sent to {sc['guest_email']}.")
    print("    Demo email is delayed several minutes; open the 'Complete with "
          "DocuSign: <registration form>' message and sign it.")
    print("    Then run:  .venv/bin/python scripts/manual/e2e_full_flow.py in_window_complete")
    await engine.dispose()


async def run_in_window_complete() -> None:
    sc = _resolved("in_window")
    print("\n================ SCENARIO: in_window_complete ================")
    refs = await _booking_refs(sc["external_id"])
    if refs is None:
        print(f"No {sc['external_id']} booking found — run in_window_send first.")
        await engine.dispose()
        return
    booking_id, envelope_id, seam_code_id = refs

    # Check the envelope actually reached 'completed' (guest signed).
    api, acct = get_envelope_api()
    status = api.get_envelope(acct, envelope_id).status
    print(f"  envelope {envelope_id} status = {status!r}")
    if status != "completed":
        print("  NOT signed yet — sign the DocuSign email at "
              f"{sc['guest_email']}, then re-run in_window_complete.")
        print("  (Nothing torn down; the booking + envelope are left live.)")
        await engine.dispose()
        return

    try:
        print("\n[STEP 5] envelope COMPLETED — firing handle_envelope_completed "
              "(real signed-PDF download + in-window immediate HOA send)")
        async with AsyncSessionLocal() as s:
            b = (await s.execute(select(Booking).where(Booking.id == booking_id))).scalar_one()
            await s.refresh(b, attribute_names=["tasks"])
            hoa_task = next(t for t in b.tasks if t.task_type == TaskType.HOA_EMAIL)
            # Override PDF path (host has no /app) and HOA recipient (never the
            # real HOA). PDF download, window math, and the Gmail send are all LIVE.
            with patch("app.tasks.handlers.docusign._pdf_path_for_booking", _pdf_path_override), \
                 patch("app.tasks.handlers.docusign.load_config", _load_config_hoa_override):
                await handle_envelope_completed(b, hoa_task, envelope_id, s)
                await s.commit()
        print(f"  HOA email sent live to {_hoa_test_recipient()} with the signed PDF attached")
        await print_state("after HOA leg", booking_id)

        async with AsyncSessionLocal() as s:
            b = (await s.execute(select(Booking).where(Booking.id == booking_id))).scalar_one()
            await s.refresh(b, attribute_names=["tasks"])
            by = {t.task_type: t for t in b.tasks}
        ok = by[TaskType.HOA_EMAIL].state == sc["expect_hoa"] and b.signed_pdf_path
        print(f"\n[RESULT] HOA_EMAIL={by[TaskType.HOA_EMAIL].state.value}  "
              f"expected={sc['expect_hoa'].value}  "
              f"signed_pdf={'YES' if b.signed_pdf_path else 'no'}  "
              f"{'PASS' if ok else 'FAIL'}")
    finally:
        await _teardown(sc, booking_id, envelope_id, seam_code_id)
        await engine.dispose()


async def run_in_window_hoa() -> None:
    """Prove the in-window immediate HOA send (the Step-2 fix) LIVE.

    Runs the full flow (persist, contact, dispatch: real cleaner sheet + real
    DocuSign envelope + real Seam code), then drives handle_envelope_completed
    using a REFERENCE completed envelope id (REFERENCE_ENVELOPE_ID) — a real,
    previously-signed sandbox envelope — so the signed-PDF download is a genuine
    live DocuSign fetch of a real signature. Everything in the Step-2-fixed path
    runs live: the combined-PDF download, the HOA window evaluation, and the real
    HOA email send via Gmail (recipient overridden to a test inbox). The only
    substitution is *which* signature backs the PDF — used because the DocuSign
    DEMO tier throttled email delivery, blocking a fresh interactive signature.
    Guest-email delivery is separately proven (diagnostic sends reached a real
    external inbox earlier in Step 16).
    """
    sc = _resolved("in_window")
    ref_env = os.environ.get("REFERENCE_ENVELOPE_ID", "").strip()
    print(
        "\n============ SCENARIO: in_window_hoa "
        "(live HOA proof via reference signature) ============"
    )
    if not ref_env:
        print(
            "Set REFERENCE_ENVELOPE_ID to a COMPLETED sandbox envelope id "
            "(a real prior signature)."
        )
        await engine.dispose()
        return
    await ensure_schema()

    # Clean any previously-parked in_window booking first (idempotent).
    existing = await _booking_refs(sc["external_id"])
    if existing:
        print(f"Cleaning previously-parked {sc['external_id']} ...")
        await _teardown(sc, *existing)

    await _persist_contact_dispatch(sc)
    booking_id, envelope_id, seam_code_id = await _booking_refs(sc["external_id"])
    try:
        print(f"\n[STEP 4/5] DocuSign demo email is throttled, so using REFERENCE completed "
              f"envelope {ref_env}\n           for a REAL live signed-PDF download; window math + "
              f"HOA Gmail send all run LIVE.")
        async with AsyncSessionLocal() as s:
            b = (await s.execute(select(Booking).where(Booking.id == booking_id))).scalar_one()
            await s.refresh(b, attribute_names=["tasks"])
            hoa_task = next(t for t in b.tasks if t.task_type == TaskType.HOA_EMAIL)
            with patch("app.tasks.handlers.docusign._pdf_path_for_booking", _pdf_path_override), \
                 patch("app.tasks.handlers.docusign.load_config", _load_config_hoa_override):
                await handle_envelope_completed(b, hoa_task, ref_env, s)
                await s.commit()
        print(f"  HOA email sent live to {_hoa_test_recipient()} with the real signed PDF attached")
        await print_state("after HOA leg", booking_id)

        async with AsyncSessionLocal() as s:
            b = (await s.execute(select(Booking).where(Booking.id == booking_id))).scalar_one()
            await s.refresh(b, attribute_names=["tasks"])
            by = {t.task_type: t for t in b.tasks}
        ok = by[TaskType.HOA_EMAIL].state == sc["expect_hoa"] and b.signed_pdf_path
        print(f"\n[RESULT] HOA_EMAIL={by[TaskType.HOA_EMAIL].state.value}  "
              f"expected={sc['expect_hoa'].value}  "
              f"signed_pdf={'YES' if b.signed_pdf_path else 'no'}  "
              f"{'PASS' if ok else 'FAIL'}")
        print(f"[RESULT] CLEANER_SHEET_ADD={by[TaskType.CLEANER_SHEET_ADD].state.value}  "
              f"DOCUSIGN_SEND={by[TaskType.DOCUSIGN_SEND].state.value}  "
              f"ACCESS_CODE_CREATE={by[TaskType.ACCESS_CODE_CREATE].state.value}")
    finally:
        # Tears down the freshly-created envelope/seam/sheet/booking — NOT the
        # reference envelope (a persistent asset left completed for reuse).
        await _teardown(sc, booking_id, envelope_id, seam_code_id)
        await engine.dispose()


MODES = {
    "far_future": run_far_future,
    "in_window_send": run_in_window_send,
    "in_window_complete": run_in_window_complete,
    "in_window_hoa": run_in_window_hoa,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        print(f"usage: {sys.argv[0]} [{' | '.join(MODES)}]")
        return 2
    asyncio.run(MODES[sys.argv[1]]())
    return 0


if __name__ == "__main__":
    sys.exit(main())
