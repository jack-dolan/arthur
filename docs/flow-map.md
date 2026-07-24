# Happy-Path Flow Map (Step 15)

> Precise end-to-end map of the booking → check-in workflow, written to support the
> Step 16 full E2E run. Covers every trigger, external API call, DB state change,
> and side effect. The **two human-in-the-loop points are marked 🧑**.
>
> Verified against source on 2026-07-02 — a snapshot, not a living spec. One rule has
> changed since: the HOA window's `latest` is now a **scheduling deadline, not a
> send-blocker** (owner adjudication 2026-07-22), and it leaves 2 full HOA-open days
> *strictly between* send day and check-in, so the worked dates in "HOA date selection"
> below no longer match `hoa_window()` exactly. `CONTEXT.md` is authoritative for the rule.
>
> Key files: `app/ingestion/poller.py`,
> `app/tasks/dispatch.py`, `app/routers/dashboard.py`, `app/routers/webhooks.py`,
> `app/tasks/handlers/*.py`, `app/tasks/scheduled.py`, `app/integrations/hoa/window.py`.

---

## Task rows created at booking time (`_initial_tasks`, poller.py:99)

Every new booking gets **11** task rows. Initial state depends on what the email
carried (Airbnb email has neither phone nor email; VRBO email has phone, no email):

| Task type | Group | Initial state (Airbnb) | Initial state (VRBO) |
|---|---|---|---|
| `OWNER_ALERT_NEW_BOOKING` | Alerts | PENDING | PENDING |
| `CLEANER_SHEET_ADD` | Automations | PENDING | PENDING |
| `DOCUSIGN_SEND` | Automations | WAITING (no email) | WAITING (no email) |
| `HOA_EMAIL` | Automations | WAITING | WAITING |
| `ACCESS_CODE_CREATE` | Automations | WAITING (no phone) | **PENDING** (phone present) |
| `OWNER_ALERT_MISSING_PHONE_7D` | Reminders | PENDING | SKIPPED |
| `OWNER_ALERT_MISSING_PHONE_4D` | Reminders | PENDING | SKIPPED |
| `OWNER_ALERT_MISSING_EMAIL_7D` | Reminders | PENDING | PENDING |
| `OWNER_ALERT_MISSING_EMAIL_4D` | Reminders | PENDING | PENDING |
| `OWNER_ALERT_DOCUSIGN_UNSIGNED_7D` | Reminders | PENDING | PENDING |
| `OWNER_ALERT_DOCUSIGN_UNSIGNED_4D` | Reminders | PENDING | PENDING |

Note: neither platform's *email* is ever in the confirmation, so `DOCUSIGN_SEND`
always starts WAITING. Guest **email** is always a manual entry; guest **phone** is
manual for Airbnb only.

**Display-quirk fix (2026-07-22):** `OWNER_ALERT_NEW_BOOKING` used to stay PENDING
forever. `persist_booking` now flips it COMPLETE after a successful alert send
(PENDING + `last_error` on failure), so PENDING genuinely means "alert not sent."
Similarly, reminder rows now flip PENDING→SKIPPED as soon as their condition
resolves (contact entered via dashboard, form signed via webhook) or can never
fire (check-in passed, booking cancelled — swept daily).

---

## STEP 1 — Booking email arrives & is ingested

- **Trigger:** APScheduler `poll_booking_feed` — `interval, minutes=5` (first run ~5 min
  after boot), `max_instances=1`. (`app/main.py:55`)
- **External calls (Gmail API, booking-feed account, read-only):**
  - `users().messages().list(labelIds=["INBOX"])`
  - `users().messages().get(id, format="raw")` per new id
- **Dedup:** loads `bookings.source_email_message_id` + `bookings.cancellation_email_message_id`
  into an `already_seen` set; only unseen ids are processed. (poller.py:257)
- **Classify + parse:** `classify(msg)` → `parse_airbnb_booking` / `parse_vrbo_booking`.
- **DB change (`persist_booking`, poller.py:158):** one `Booking` row inserted
  (`status=ACTIVE`, `source_email_message_id=msg_id`, guest name + dates always;
  guest_phone for VRBO), the **11 task rows** above, and `DataPoint` provenance rows
  (source `EMAIL_PARSE`). Single commit.
- **Side effect — new-booking alert email:** after commit, `send_new_booking_alert`
  sends via **alerts** Gmail account (`get_alerts_service()`), containing the guest
  name, stay dates, and a dashboard deep link `https://{DOMAIN}/bookings/{id}`.
  Wrapped in try/except — a send failure is logged, non-fatal, booking still persists.
  (poller.py:208)

**State after Step 1:** booking ACTIVE. `CLEANER_SHEET_ADD` PENDING but *not yet run*
(the poller does **not** dispatch automation tasks — dispatch is only triggered by the
dashboard contact-save, Step 3). Automations sit at their initial states until then.

> ⚠️ Consequence worth knowing for Step 16: **nothing dispatches the PENDING
> `CLEANER_SHEET_ADD` until the owner saves contact info.** In a pure "inject email"
> run, the cleaner row is not written until Step 3 fires `_dispatch_pending_tasks`.
> The E2E/orchestration script must therefore perform the manual contact-save step to
> exercise the automations.

---

## STEP 2 🧑 — Owner enters missing contact field (HUMAN-IN-THE-LOOP #1)

- **Trigger:** owner opens the dashboard deep link, submits the contact form →
  `POST /bookings/{id}/contact` (`save_contact`, dashboard.py:222). Behind the dashboard's
  Google login + email allowlist (`require_user`; this doc predates that change — it read
  "HTTP Basic auth" until Basic was replaced by OIDC on 2026-07-05).
- **Input:** guest **email** (always needed), and guest **phone** for Airbnb bookings.
- **Validation:** `_validate_phone` (≥10 digits) / `_validate_email`. Invalid → form
  re-render with errors, no writes.
- **DB change (single transaction, dashboard.py:274):**
  - Sets `booking.guest_email` / `booking.guest_phone` (only if currently None).
  - Adds `DataPoint` rows with source `MANUAL_ENTRY`.
  - `_flip_waiting_tasks`: `DOCUSIGN_SEND` WAITING→PENDING when email saved;
    `ACCESS_CODE_CREATE` WAITING→PENDING when phone saved. (dashboard.py:105)
  - `await db.commit()` **before** the redirect.
- **Side effect:** `asyncio.create_task(_dispatch_pending_tasks(booking_id))` — fire-and-forget
  background dispatch (passes UUID only). Returns `303` redirect to the detail page.

**State after Step 2:** email-dependent + phone-dependent automations flipped to PENDING;
background dispatch enqueued.

---

## STEP 3 — Background dispatch runs the automations

- **Trigger:** the `_dispatch_pending_tasks` task from Step 2 (`app/tasks/dispatch.py:49`).
- **Behavior:** opens its own session, loads the booking + tasks, and iterates the fixed
  order **`CLEANER_SHEET_ADD → DOCUSIGN_SEND → ACCESS_CODE_CREATE → HOA_EMAIL`**, running
  only rows currently **PENDING** (WAITING/COMPLETE/FAILED/SKIPPED skipped). **Commit after
  each task**; on handler exception → task FAILED + `last_error` + `attempt_count++`, then
  continue (failure isolation, dispatch.py:106).

### 3a. `CLEANER_SHEET_ADD` (handler `handle_cleaner_sheet`)
- IN_PROGRESS → **Google Sheets API**: `spreadsheets().get` (sheetId),
  `values().get(A:F)`, find sentinel (`prop.cleaner_schedule.sentinel_pattern`,
  scans all cells), compute chronological insertion index, `batchUpdate` insertDimension,
  `values().update(A#:F#)`.
- **Side effect:** one new row above the sentinel — `[False, "First Last", M/D/YYYY in,
  M/D/YYYY out, "4:00:00 PM", "11:00:00 AM"]`. Task → COMPLETE.

### 3b. `DOCUSIGN_SEND` (handler `handle_docusign_send`)
- If `guest_email` is None → back to WAITING (no send). Otherwise IN_PROGRESS →
  **DocuSign API**: refresh-token→access-token exchange, `create_envelope` from
  `docusign_template_id` with a `TemplateRole(email, name, role_name=docusign_signer_role)`
  and built-in reminder (7-day). 
- **Side effect:** envelope created (status `sent`) → **DocuSign emails the guest**.
  `task.external_ref = envelope_id`; task → COMPLETE.

### 3c. `ACCESS_CODE_CREATE` (handler `handle_access_code_create`)
- If `guest_phone` is None → WAITING (returns cleanly; dispatcher does not mark FAILED).
  Otherwise IN_PROGRESS → **Seam API** `access_codes.create` on `seam_device_id`,
  `code = last 4 digits of phone`, window `starts_at = 4:00 PM ET check-in day`,
  `ends_at = 11:00 AM ET checkout day` (stored/sent as UTC `...Z`).
- **Side effect:** time-bound code on the (sandbox) lock. `task.external_ref =
  access_code_id`; task → COMPLETE.

### 3d. `HOA_EMAIL` (handler `handle_hoa_email`) — dispatch-time attempt
- Guard: needs `signed_pdf_path`. **At this point in the happy path the guest has not
  signed yet**, so `signed_pdf_path` is None → handler logs + returns, **task stays
  WAITING**. (The real HOA send happens later, Step 5, off the webhook — not here.)

**State after Step 3 (typical):** `CLEANER_SHEET_ADD` COMPLETE, `DOCUSIGN_SEND` COMPLETE,
`ACCESS_CODE_CREATE` COMPLETE, `HOA_EMAIL` still WAITING (no PDF yet).

---

## STEP 4 🧑 — Guest signs the DocuSign envelope (HUMAN-IN-THE-LOOP #2)

- Out-of-band: the guest opens the DocuSign email and signs. No app action; DocuSign
  fires a Connect webhook when the envelope reaches `completed`.

---

## STEP 5 — DocuSign webhook: PDF stored + HOA send (immediate branch)

- **Trigger:** `POST /webhooks/docusign` (`app/routers/webhooks.py:117`). Fires out-of-band
  whenever the guest signs.
- **Security:** reads raw body first; validates `X-DocuSign-Signature-1` HMAC-SHA256
  (constant-time) against `DOCUSIGN_HMAC_KEY`; bad/missing → 400 before any DB write.
- **Routing:** `_extract_envelope_event` → `(envelope_id, status)`. On `completed`:
  loads the `DOCUSIGN_SEND` task by `external_ref == envelope_id`, then the `Booking`
  **with `selectinload(Booking.tasks)`** (eager-load required — the handler walks
  `booking.tasks`), then `handle_envelope_completed`. On `declined` → `DOCUSIGN_SEND`
  → FAILED. Other statuses → 200 no-op. Always returns 200 on authentic events.
- **`handle_envelope_completed` (docusign.py:117):**
  1. **DocuSign API** `get_document(account_id, "combined", envelope_id)` — note the
     arg order (Step 11 fix); downloads combined PDF.
  2. Writes `/app/data/pdfs/{booking.id}.pdf` (UUID-only path).
  3. Sets `booking.signed_pdf_path` and **`session.commit()` immediately** — the durable
     signed-PDF record is persisted *before* any HOA send is attempted (Step 2
     commit-ordering decision).
  4. Computes `hoa_window(check_in, open_days, days_min=2, days_max=7)`:
     - **If `earliest ≤ today ≤ latest` (in window):** sends HOA email **now** via
       `send_hoa_email(...)` from **alerts** Gmail with the signed PDF attached to
       `hoa.email`; sets `HOA_EMAIL` → COMPLETE. Webhook's outer commit (webhooks.py:203)
       persists that state.
     - **If `today < earliest` (too early):** leaves `HOA_EMAIL` **WAITING** for the
       hourly scheduler.

**Side effect:** signed PDF on disk + (in-window only) the HOA notification email to the
HOA address with the PDF attached.

---

## STEP 6 — Hourly HOA scheduler (the WAITING → sent recovery path)

- **Trigger:** APScheduler `check_hoa_window` — `interval, hours=1` (`app/main.py:65`).
- **Query:** ACTIVE bookings with `signed_pdf_path IS NOT NULL` and an `HOA_EMAIL`
  task in WAITING. Two-phase: scan ids in a short session, then process each booking in
  its **own** session (Step 14 failure-isolation fix). Re-checks `hoa_task.state ==
  WAITING` under the fresh read before sending (shrinks the double-send window vs the
  webhook-immediate path — see Risk R1, Step 14).
- **Per booking:** `handle_hoa_email` re-evaluates the window:
  - `early` → return, leave WAITING (tries again next hour).
  - `in_window` → **Gmail send** HOA email w/ PDF; `HOA_EMAIL` → COMPLETE + `completed_at`.
  - `past` → return, leave unchanged (does **not** FAIL; logged).

**This is the branch a far-future test booking eventually rides:** WAITING until the
calendar reaches `earliest`, then the next hourly run sends it.

---

## STEP 7 — Daily reminder scheduler (parallel, only if fields still missing / unsigned)

- **Trigger:** APScheduler `check_daily_reminders` — `cron hour=8 US/Eastern`,
  `misfire_grace_time=None`, `coalesce=True`.
- **Query:** ACTIVE bookings with `check_in_date > today_et`.
- **Logic (`_pending_reminders`):** for each of the 6 reminder task types, fire if the
  row is still PENDING, `days_until_checkin ≤ threshold` (7 or 4), **and** the underlying
  field is *still* missing/unsigned at runtime (re-checked live). Sends via **alerts**
  Gmail; on success flips that reminder task → COMPLETE.
- In a clean happy path (email+phone entered promptly, guest signs), these conditions are
  false, so no reminders fire and the reminder rows are simply never triggered
  (still PENDING) or were SKIPPED at creation.

---

## Terminal happy-path state

| Task | Final state | Evidence |
|---|---|---|
| `OWNER_ALERT_NEW_BOOKING` | COMPLETE (since 2026-07-22 fix) | alert email sent in Step 1 |
| `CLEANER_SHEET_ADD` | COMPLETE | row above sentinel |
| `DOCUSIGN_SEND` | COMPLETE | `external_ref` = envelope_id |
| `ACCESS_CODE_CREATE` | COMPLETE | `external_ref` = access_code_id; code on lock |
| `HOA_EMAIL` | COMPLETE **or** WAITING | COMPLETE if check-in in window; WAITING if far-future (correct) |
| 6 reminder tasks | PENDING/SKIPPED/COMPLETE | only COMPLETE if a reminder actually fired |
| Booking | ACTIVE, `signed_pdf_path` set | PDF on disk |

**Two HOA outcomes are both "correct":**
- **In-window check-in** → HOA_EMAIL COMPLETE, email sent immediately off the webhook
  (this is the path Step 2 fixed; Step 16 must prove it fires **live**).
- **Far-future check-in** → HOA_EMAIL WAITING; no email yet; hourly scheduler will send
  it once the window opens.

---

## Cancellation branch (not the happy path, for reference)

Cancellation email → poller → `handle_cancellation` → `_apply_cancellation`: booking
→ CANCELLED; **automatic**: DocuSign void (idempotent) + Seam code delete; **alert-only**:
one owner email summarizing the HOA + cleaner-sheet manual cleanups, and the two
`OWNER_ALERT_CANCELLATION_{HOA,CLEANER}` task rows created→COMPLETE. (Step 3 of runbook.)

---

# Preconditions & manual setup for the Step 16 E2E run

### What to inject / how to drive it
The E2E must include **the human contact-save step**, because the poller alone never
dispatches automations (Step 1 ⚠️). Two viable drivers:

1. **`persist_booking` directly (recommended for the orchestration script).** Lets you
   set an **arbitrary check-in date** so you can deterministically hit either HOA branch
   without editing `.eml` fixtures. Build an `AirbnbBookingData` (no phone/email → forces
   both manual-entry fields) or `VrboBookingData` (phone present) and call
   `persist_booking(msg_id, msg, parsed, platform)`.
2. **Inject a real `.eml`** via the poller path only if you also want to exercise
   classify/parse live — but fixture dates are fixed (see below), so you'd still edit the
   date to control the HOA branch. Prefer option 1.

Either way, after persistence: call `save_contact` (or POST the form) with the missing
fields → this fires `_dispatch_pending_tasks` (cleaner row, DocuSign send, access code).
Then simulate/deliver the DocuSign `completed` webhook to drive Step 5.

### Contact info to enter 🧑
- **Guest email:** a **test recipient you control** — this receives the real sandbox
  DocuSign envelope, and you sign it to trigger the completed webhook. The manual
  scripts read it from `E2E_GUEST_EMAIL` (see `.env.template`); they have no default
  and abort if it is unset. It must NOT be the DocuSign account owner address — demo
  DocuSign suppresses the signing email when signer == account owner.
- **Guest phone (Airbnb only):** a clearly-fake test number, e.g. `+1 (555) 010-1234`.
  Last-4 (`1234`) becomes the door code on the **sandbox** lock — harmless.

### Sandbox resources that WILL be touched
- **Google Sheets:** the real spreadsheet — use the **throwaway test tab** and clearly
  fake guest name (`ZZ TEST — delete me`). Row must be cleaned up.
- **DocuSign (sandbox, `DOCUSIGN_SANDBOX=true`):** a real demo envelope is sent to your
  test email; sign it to complete. Void + leave voided in teardown.
- **Seam (sandbox workspace / virtual Schlage device):** a real sandbox access code is
  created on `seam_device_id`. Delete in teardown.
- **Gmail (alerts account):** real emails send — the new-booking alert and (in-window
  run) the HOA email. Send those to **your own address**, not a real HOA: both manual
  scripts read the override from `E2E_HOA_RECIPIENT` and abort when it is unset, and
  `golive_e2e.py` additionally refuses to run unless `config.yaml`'s HOA email equals
  it. ⚠️ The
  integration guard (`tests/integration/conftest.py::_block_live_gmail`, Step 14) blocks
  live Gmail under `tests/integration/`; the E2E lives under `tests/e2e/` and is **not**
  blocked — it is intended to send live. Point every recipient at an address you own.

### HOA date selection (today = **2026-07-02**, a Thursday; `days_min=2`, `days_max=7`, open Mon–Sat)
- **Far-future → HOA_EMAIL WAITING:** pick check-in `2026-09-01`.
  `earliest = 2026-08-25`; today `2026-07-02 < earliest` → `early` → stays **WAITING**.
  (The Airbnb fixture's `2026-07-15` also yields WAITING today: `earliest = 2026-07-08`.)
- **In-window → immediate HOA send fires:** pick check-in **`2026-07-06`** (Monday).
  `earliest = 2026-06-29`; `latest =` count 2 open days back from Mon 7/6 = **`2026-07-04`**
  (7/5 Sat → 1, 7/4 Fri → 2). Window `[2026-06-29, 2026-07-04]` contains today `2026-07-02`
  → `handle_envelope_completed` sends the HOA email **immediately** on the completed
  webhook and sets HOA_EMAIL COMPLETE. **This is the path Step 2 fixed — prove it live.**
  - ⚠️ Do **not** reuse the VRBO-1 fixture (`2026-07-02` check-in): that is *today*, and
    `latest = 2026-06-30` → today is already **`past`** the window (no send). Use `2026-07-06`.
  - Access-code window for `2026-07-06`→checkout (e.g. `2026-07-09`):
    `starts_at = 2026-07-06T20:00:00Z` (4 PM EDT), `ends_at = 2026-07-09T15:00:00Z` (11 AM EDT).

### Teardown checklist (Step 16 must leave nothing behind)
Envelope voided · Seam access code deleted · Sheet test row removed · signed PDF file
removed · Booking + task/data_point rows deleted from the DB.
