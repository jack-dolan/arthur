# Log-signal catalog

## Contents
- Healthy noise (do not report as problems)
- Actionable signals (report, with severity)
- Expected cadence (absence is a finding)
- Alert-email cross-checks

## Healthy noise (do not report as problems)

| Signal | Why it's normal |
|---|---|
| `other` rows in processed_messages | Most booking-feed inbox traffic is platform noise (ratings, receipts, guest messages). In one real session 17 of 18 messages were `other`. Volume varies with inbox chatter. |
| Bot probes in caddy logs (`/wp-json/`, `/xmlrpc.php`, random 404s) | Internet background radiation against any public host. Not an attack on this app. |
| `http` → 308 redirect on the public domain | Caddy's HTTP→HTTPS redirect. `curl` without `-L` sees 308 by design. |
| `HOA window not yet open ... leaving for Phase 5 scheduler` | The hourly scan re-checking a signed-but-early booking. Expected until the window opens. |
| `already claimed by another path; skipping` | The R1 atomic claim doing its job (webhook vs hourly race). One send happened; the loser logs this. |
| `Booking ... already cancelled; skipping` | Idempotent replay of a cancellation email. |
| A single 502 right after a deploy | The ~2s gap between container start and uvicorn listening; self-heals. |

## Actionable signals (report, with severity)

| Signal | Severity | Meaning / next step |
|---|---|---|
| `Gmail auth failed` repeating | HIGH | The poller cannot read the booking feed — bookings are NOT being ingested. See `recovering-credentials`. |
| `keep-alive failed` | HIGH | DocuSign refresh-token exchange failing. If it persists, every DocuSign action dies within 30 days. An owner alert email should also exist. See `recovering-credentials`. |
| `dispatch: task ... failed` | MED–HIGH | An automation failed; the daily requeue retries up to the attempt cap. Repeats of the same task → read its `last_error` in the DB and hand off to `triaging-stuck-tasks`. |
| `could not record FAILED` | HIGH | The dispatcher couldn't even write the failure — DB trouble; the task may be stranded IN_PROGRESS. Triage skill, immediately. |
| `MissingGreenlet` | HIGH | An async/session bug regression. Capture the traceback; this class of bug has recurred. |
| `sending anyway` (HOA late send) | MED | A guest signed after the last acceptable day; the form went out late by design. The owner may want to call the HOA to expedite the packet. |
| `could not be extracted` (webhook) | MED | DocuSign Connect payload shape drifted; a parse-failure owner alert should exist. Capture the logged body and update `_extract_envelope_event`. |
| `cancellation_parse_error` / `classify_error` / `parse_error` / `duplicate` dead-letters | MED | Platform email format drift or resends. Each should have a matching owner-alert send line; the underlying email needs manual handling. |
| `alteration` dead-letter (F9) | MED | A booking's dates/details may have changed on the platform; the classifier heuristic is deliberately loose (no real sample email yet) so this fires on any subject containing updated/changed/modified/altered. Check the owner alert, verify against the platform, update the booking by hand if needed. |
| Stuck IN_PROGRESS >24h (DB section) | HIGH | The risk register's crash-window edge. **Never reset it from here** — the external side effect may exist. Use `triaging-stuck-tasks`. |
| FAILED at attempt cap (DB section) | MED | Automatic retries exhausted; needs a human fix. `triaging-stuck-tasks`. |
| `Cannot persist rotated DocuSign refresh token` to a path other than `.env` | MED | The durable token store on `/app/data` should be writable; if the store write fails, restarts lose rotations again. |

## Expected cadence (absence is a finding)

APScheduler **silently discards** a job run whose fire moment is missed
(default misfire grace ~1s), so "no log line" means "job never ran". The
three daily jobs log an INFO **"run started" heartbeat even on idle runs**
precisely so this check is sound. In any window ≥24h expect:

- `complete_past_bookings: run started` — daily (02:00 ET).
- `verify_credentials: run started` — daily (07:00 ET); a healthy run also
  logs `all 5 credential checks passed`. Any `check FAILED` line is HIGH
  severity (a live credential is dead).
- `check_daily_reminders: run started` — daily (08:00 ET).
- `requeue_stalled_automations: run started` — daily (08:30 ET).
- `verify_access_codes: run started` — daily (09:00 ET).
- `refresh_docusign_token` — weekly (cron, Mon 03:30 ET); in a 7-day window
  expect exactly one `keep-alive succeeded`.
- `check_classifier_drift: run started` — weekly (Sun 09:00 ET); idle runs
  log `no platform-domain OTHER dead-letters`.
- `send_monthly_status_email: run started` — monthly (1st, 08:00 ET).
- `Heartbeat ping failed` (WARNING) — the external healthchecks.io monitor
  was unreachable; occasional one-offs are noise, repeated failures mean the
  dead-man's-switch layer is blind (MEDIUM).

No heartbeat exists for the high-frequency jobs (they'd flood the logs):
`poll_booking_feed` (5 min) and `check_hoa_window` (hourly) log at DEBUG on
quiet cycles, so their absence is normal — but poller auth failures, or days
of total silence with zero webhook/HOA/booking activity, deserve a scheduler
health check.

## Alert-email cross-checks

Every dead-letter of a serious disposition and every terminal task state has
a paired owner-alert send:

- `parse_error`/`classify_error` → `Sent unparseable-email alert`
- `cancellation_parse_error` → `Sent unprocessable-cancellation alert`
- `alteration` dead-letter (F9) → `Sent booking-alteration alert`
- keep-alive failure → `Sent DocuSign keep-alive failure alert`
- capped FAILED / stuck IN_PROGRESS → `Sent stalled-automations alert`
- unhealthy door code near check-in → `Sent access-code problem alert`

A dead-letter or terminal state **without** its paired send line means the
owner was never told — that gap is itself HIGH severity.
