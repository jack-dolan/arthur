# Implementation Plan

## Testing Philosophy

**TDD is the default discipline for this project, not an option.** Every piece of logic is written test-first using red-green-refactor. No production code is written before a failing test exists for it. Use `/octo:tdd` when the logic is complex or the edge cases are subtle — but even simple functions get tests before implementation.

Test structure follows pytest conventions:
- `tests/unit/` — pure logic, no I/O (parsers, date calculators, classifiers, formatters)
- `tests/integration/` — real external calls behind feature flags or recorded fixtures (Gmail, DocuSign, Seam, Sheets, scraper)
- `tests/e2e/` — full booking workflow from injected email to completed task checklist

Coverage target: **90%+ on unit tests**. Integration and e2e tests cover the happy path and the most dangerous edge cases (Sunday check-ins, unsigned DocuSign near deadline, cancellation mid-workflow).

## Critical Path

Everything depends on Phase 2 (booking ingestion). Without a booking in the database, no downstream task can run. Phases 3–5 are largely independent of each other once Phase 2 is done, which makes them good candidates for parallel implementation via `/octo:parallel`.

---

## Phase 1 — Foundation
*Goal: runnable skeleton that compiles, migrates, and serves a health check.*

| Step | Work |
|---|---|
| 1.1 | Docker Compose: `app` (FastAPI), `db` (PostgreSQL), `caddy` or `nginx` for TLS |
| 1.2 | FastAPI app skeleton: `main.py`, router structure, `/health` endpoint |
| 1.3 | Alembic setup: initial migration with empty schema |
| 1.4 | `.env` template: all secrets listed with placeholder values; `.gitignore` verified |
| 1.5 | Gmail OAuth setup: service account or OAuth2 credentials for both inboxes |
| 1.6 | Property config system: Python dataclass/Pydantic model per property, loaded from config file |

**Done when:** `docker compose up` starts cleanly, `/health` returns 200, `alembic upgrade head` runs without error, `pytest` runs (even with 0 tests yet) without configuration errors.

---

## Phase 2 — Booking Ingestion (Critical Path)
*Goal: new booking emails are parsed into a database row with all available fields populated.*

| Step | Work |
|---|---|
| 2.1 | Database schema: `bookings`, `booking_tasks`, `data_points` (provenance tracking) |
| 2.2 | Gmail poller: APScheduler job, checks booking feed inbox every 5 minutes via Gmail API |
| 2.3 | Email classifier: determines email type (Airbnb new booking / VRBO new booking / cancellation / other) |
| 2.4 | Airbnb confirmation parser: extracts name, dates, confirmation code from email body |
| 2.5 | VRBO confirmation parser: extracts name, dates, phone, reservation ID from email body |
| 2.6 | New booking alert: on booking detection, send alert email to owners with deep link to booking detail page on the dashboard (link is functional once Phase 3 is complete) |
| 2.7 | Cancellation handler: voids DocuSign envelope (if exists) and deletes Seam access code (if exists); fires alert email for HOA and cleaner sheet |

**Done when:** a real booking email forwarded to the booking feed inbox produces a `bookings` row with all email-available fields populated and correct provenance, the new-booking alert email is sent, and the full test suite passes.

**TDD focus areas in Phase 2:**
- Email classifier: test every known email shape (Airbnb booking, VRBO booking, cancellation, noise) before writing the classifier
- Airbnb/VRBO parsers: test against real anonymized email fixtures from `example-emails/` (kept out of git; use copies with PII stripped for the test fixtures)
- Cancellation handler: test each mid-workflow state (before DocuSign sent, after sent but unsigned, after signed, after HOA email sent)

---

## Phase 3 — Dashboard (Full, Including Data Entry)
*Goal: password-protected web UI showing all active bookings and their task states, with write capability for missing fields.*

| Step | Work |
|---|---|
| 3.1 | FastAPI route: `GET /` → rendered Jinja2 template — booking list with guest name, platform, check-in/checkout, days until check-in |
| 3.2 | Booking detail page: task state checklist (pending / complete / failed / waiting) and data provenance per field (email parse / manual entry / webhook) |
| 3.3 | Data entry form on booking detail page: input fields for missing phone number and/or email address; validates format before saving; records provenance as "manual entry" |
| 3.4 | On successful field save: trigger downstream tasks that were blocked on that field (DocuSign send if email just entered; access code creation if phone just entered) |
| 3.5 | HTTP basic auth: password from `.env` |

**Done when:** new booking alert email deep link opens the booking detail page; owner can enter phone/email; saving unblocks downstream tasks; task states update correctly in the UI.

---

## Phase 4 — Task Integrations
*These four integrations are independent of each other. Use `/octo:parallel` to build them simultaneously across Claude instances.*

### 4A — Google Sheets (Cleaner Schedule)
- Google Sheets API OAuth setup (service account preferred)
- Find the target sheet by name, read all rows to determine insertion point
- Insert new row above the sentinel row in correct chronological position
- Write 5 fields: Guest, Check In, Check Out, Time In (default 4:00:00 PM), Time Out (default 11:00:00 AM)

**TDD focus:** Row insertion logic — test chronological ordering, insertion before sentinel, insertion when new booking is earlier than all existing rows, insertion when sheet is empty except for the sentinel.

### 4B — DocuSign
- DocuSign eSign API OAuth setup
- Send envelope from existing template: pass guest name + email, set 7-day built-in reminder on send
- Webhook endpoint (`POST /webhooks/docusign`) to receive envelope status events
- On `envelope-completed`: retrieve signed PDF via API, store it, trigger HOA email window check
- On cancellation: void envelope via API

**TDD focus:** Webhook handler state transitions — test each envelope status event against every possible booking state; test that void is idempotent (called twice doesn't error).

### 4C — Seam / Schlage
- Seam API setup: connect Schlage Encode (BE489WB2) to Seam workspace
- Create time-bound access code: last 4 digits of guest phone, active 4:00 PM check-in day → 11:00 AM checkout day (all US/Eastern, confirm Seam's time zone handling at implementation time)
- Delete access code on cancellation
- Retry logic: if phone number isn't scraped yet when this task runs, re-queue

**TDD focus:** Access code derivation (last 4 digits, edge cases like phone numbers with extensions or formatting), time window calculation across DST boundaries.

### 4D — HOA Email
- HOA window calculator: given a check-in date, compute the earliest and latest valid send dates accounting for HOA closed Sundays
- Gmail send via API: templated subject/body (Eastern time-of-day greeting), signed PDF attached, from public-facing automation address to the HOA email in `config.yaml`
- Trigger: fires when DocuSign webhook marks envelope complete AND current date is within the HOA window (if too early, scheduler checks daily; if already in window, sends immediately)

**TDD focus (use `/octo:tdd`):** HOA window calculator is the most edge-case-dense logic in the system. Test matrix: check-in on each day of the week × form signed early / on time / late / in window / past deadline. Sunday check-in (HOA closed) is the canonical hard case. Also test time-of-day greeting (morning / afternoon / evening) at Eastern time boundaries.

---

## Phase 5 — Scheduling & Alerts
*Goal: time-based checks run on schedule and fire actionable alert emails.*

| Step | Work |
|---|---|
| 5.1 | APScheduler jobs: daily check for all active bookings against reminder thresholds |
| 5.2 | Alert email builder: templates for each alert type (missing phone/email at 7d before check-in, missing phone/email at 4d before check-in, DocuSign unsigned at 7d, DocuSign unsigned at 4d) |
| 5.3 | Platform-routing instructions embedded in alerts: "paste this into [Airbnb/VRBO] in the conversation with [Guest Name]" |
| 5.4 | Cancellation alert emails: HOA already notified / cleaner sheet row to remove |

**TDD focus:** Reminder threshold logic — test each alert condition against bookings at various days-until-check-in values, including edge cases at exactly 7 and 4 days. Test that alerts are not re-fired if already sent (idempotency). Test that no alerts fire for completed or cancelled bookings. Test that missing-data reminders fire for both phone and email independently (one field entered but not the other).

---

## Phase 6 — Hardening & Go-Live
*Run before putting any real booking through the system.*

| Step | Work |
|---|---|
| 6.1 | `/octo:security` audit: credentials in `.env`, scraping session handling, webhook endpoint auth, Gmail token storage |
| 6.2 | `/octo:review` across Phase 2–4 code: Codex + Copilot review |
| 6.3 | Migration scripts: `scripts/backup.sh` (pg_dump) and `scripts/restore.sh` with documented VPS migration procedure |
| 6.4 | UptimeRobot setup: external ping to `/health` every 5 minutes |
| 6.5 | End-to-end dry run: manually inject a test booking, verify all tasks complete, verify alert emails arrive |

---

## Octo Usage Guide (for this project)

| When | Command | Why |
|---|---|---|
| Every feature, always | `/octo:tdd` | Default discipline — red-green-refactor throughout |
| Building Phase 4 integrations | `/octo:parallel` | 4A–4D are independent; build them simultaneously |
| HOA window date math | `/octo:tdd` | Highest edge-case density in the system |
| After each phase | `/octo:review` | Codex + Copilot catch what Claude misses |
| Before go-live | `/octo:security` | Credential handling, webhook auth |

---

## Post-V1 Roadmap

- Full admin UI: trigger/override individual tasks manually
- Data provenance drill-down: click any field to see how it was obtained
- Multi-property support: add property 2 via config, new cleaner adapter
- Hostex integration: replace Airbnb/VRBO scraping if maintenance burden grows
- DocuSign send-quota monitoring: alert before hitting annual limit
