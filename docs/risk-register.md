# Pre-Go-Live Risk Register (Step 19)

Authoritative record of every documented risk carried into go-live, its final
disposition, and — for anything accepted or deferred — *why it is safe to go live
without it* and *what would trigger revisiting it*. Built during runbook Step 19
(2026-07-05). See the Session Log in `GETTING-TO-PRODUCTION.md` for the commit
hashes.

## Fixed (this step)

| # | Risk | Fix | Reproducing test |
|---|---|---|---|
| **R1** | HOA double-send: webhook-immediate vs hourly `check_hoa_window`, no atomic claim on the HOA task | Shared atomic claim `app/tasks/claim.py::claim_task` (`UPDATE ... WHERE state=WAITING RETURNING` + commit) used by BOTH `handle_envelope_completed` and `handle_hoa_email` before sending; a send failure releases the claim (WAITING) so the hourly job can retry | `tests/integration/test_triggers_isolation.py::test_hoa_no_double_send_webhook_racing_hourly` (deterministic interleave; RED = 2 sends, GREEN = 1) |
| **Risk 4** | DocuSign Connect payload shape unvalidated → an authentic payload we can't parse is silently dropped (200, no action) | Harden-to-loud: on an HMAC-authentic payload with no extractable envelopeId/status, log ERROR with the top-level keys AND send an owner alert (`send_docusign_webhook_parse_failure_alert`); still return 200 to avoid Connect retry-storms | `test_webhook_authentic_but_unparseable_alerts_owner` + `tests/unit/test_alerts.py` builder tests |
| **R3b** | Unparseable / `EmailType.OTHER` mail re-polled every 5 min forever; a real booking that fails to parse is silently dropped with no alert | New `processed_messages` dead-letter table (model + Alembic `0004`); poller unions it into `already_seen`; `OTHER` recorded silently, hard parse failures recorded **and** raise an owner alert (`send_unparseable_email_alert`) | `test_poller_other_email_is_dead_lettered_not_refetched`, `test_poller_unparseable_booking_records_and_alerts`, `test_poller_cancellation_before_booking_is_not_dead_lettered` |
| **R5** | DocuSign refresh token expires in 30 days; the rotated token returned on each exchange was discarded, so the clock never reset — a quiet month with no bookings kills it | `_refresh_access_token` persists the rotated `refresh_token` back to `.env` + in-memory settings; a **weekly** APScheduler keep-alive (`refresh_docusign_token`) does a token-only OAuth exchange (**no envelope, no cost**) so the token never expires idle | `tests/unit/test_docusign_token_rotation.py` (6 tests) |

## Accepted / Deferred (deliberate, recorded)

### R2 — concurrent dashboard dispatch (duplicate DocuSign envelope + Seam code) — **CLOSED, verified**
Step 17 added the atomic `UPDATE booking_tasks SET state=in_progress WHERE
id=? AND state=pending RETURNING` claim in `app/tasks/dispatch.py` plus
`external_ref` idempotency guards in the DocuSign/Seam handlers. Re-verified in
Step 19 against the current code: a concurrent double contact-info POST results
in each handler running at most once. No action needed.
**Revisit if:** the dispatcher's claim/commit ordering is ever refactored, or a
new externally-side-effecting task type is added without an `external_ref` guard.

### Residual partial-failure edge (crash between external create and `external_ref` commit) — **ACCEPT WITH DOC**
The atomic claim commits `IN_PROGRESS` *before* the external create. If the
process dies after the create returns but before the `external_ref` commit, the
task is left `IN_PROGRESS` with the external side effect done but its ref not
recorded. This does **not** silently duplicate: the dispatcher only ever claims
`PENDING` rows, so a re-dispatch skips the stuck `IN_PROGRESS` task rather than
re-creating. The outcome is a *visible* stuck task, not a duplicate envelope/code.
- **Why safe for go-live:** requires a crash inside a sub-second window (between
  an HTTP response and a local DB commit); the failure is visible (a task stuck
  `IN_PROGRESS` with no `external_ref`), not a silent double-charge/double-unlock.
- **Operational rule (do not break this):** never blindly reset an
  `IN_PROGRESS` task back to `PENDING` to "retry" — inspect the external system
  first (was the envelope/code actually created?). A blind reset is the only way
  this edge becomes a real duplicate.
- **Revisit if:** we start seeing stuck `IN_PROGRESS` tasks in practice, or add
  automated retry of `IN_PROGRESS`/`FAILED` tasks. The clean fix is a
  pre-registered idempotency key (DocuSign supports an `X-DocuSign-Idempotency-Key`
  header; Seam would need its own scheme).

### R3a — cancellation-before-booking replay — **ACCEPT (retry is correct)**
A cancellation whose booking does not exist yet is re-polled every 5 minutes
until the booking arrives, then self-heals. This is *correct* behaviour, not a
bug: the message must keep being retried. It is deliberately **excluded** from
the R3b dead-letter (`process_message` records only `OTHER` and parse failures,
never the unknown-booking cancellation path).
- **Why safe for go-live:** self-healing; the only cost is log noise for the
  (rare, short-lived) window where a cancellation is processed before its booking.
- **Revisit if:** out-of-order batches become common enough that the log noise is
  a nuisance — then bound the retry (e.g. give up / alert after N attempts).

### R6 — HOA email lands in spam (first contact from a new Gmail sender) — **ACCEPT WITH DOC**
Gmail-API send already has aligned SPF/DKIM/DMARC (Google-signed). For a single,
known HOA recipient the reliable fix is a one-time allow-list (add sender to
contacts / mark not-spam). Documented in `CONTEXT.md`.
- **Revisit if:** the HOA recipient set grows or reports non-delivery after the
  allow-list.

### R7 — `OWNER_ALERT_NEW_BOOKING` stays `PENDING` forever — **FIXED (2026-07-22)**
Originally accepted as cosmetic: the alert *was* sent in `persist_booking`, but
the task row never flipped. Fixed post-go-live with the dashboard truthfulness
work: `persist_booking` now records COMPLETE (+`completed_at`) after a successful
send, and leaves PENDING with `last_error` on failure — so the row's state is
meaningful. Stale-reminder rows (condition resolved, check-in passed, or booking
cancelled) are likewise flipped to SKIPPED at their resolution points plus a
daily sweep. `scripts/manual/backfill_task_states.py` reconciled pre-fix rows.

## Live re-verification carried to Step 20/21

- **R1** touches the live HOA send path — Step 21's fresh in-window happy path
  must still show exactly one HOA email fired.
- **Risk 4**: Step 21 exercises the **real** inbound Connect payload for the
  first time (fresh signed envelope on the production tier). Confirm the
  `completed` event parses (envelopeId/status extracted → HOA sends). If the
  parse-failure **owner alert** fires instead, the real Connect shape differs
  from the three key-paths in `_extract_envelope_event` — capture it and update
  the parser.
- **R5**: confirm the weekly keep-alive actually persists a rotated token live
  (a real refresh writes a new `DOCUSIGN_REFRESH_TOKEN` to `.env`).
