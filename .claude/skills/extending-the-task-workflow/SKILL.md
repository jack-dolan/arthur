---
name: extending-the-task-workflow
description: Conventions and invariants for adding or changing booking task types, task states, dashboard statuses, scheduled jobs, or date-window logic in the rental-automation workflow. Use when adding a new automation or alert task type, a new scheduler job, a new booking/task state, or any feature touching the task state machine.
---

# Extending the task workflow

The state machine's correctness rests on invariants that are easy to miss
when adding features — several month-3 bugs came from exactly that. Work
through the applicable checklist; tests enforce some items, this skill is
the reason for the rest.

## Adding a new task type

```
New-task-type checklist:
- [ ] Enum value in TaskType (app/db/models.py) — DB enum needs an Alembic
      migration (ALTER TYPE ... ADD VALUE runs in an autocommit block; see
      migration 0005 for the pattern)
- [ ] Created in _initial_tasks (app/ingestion/poller.py) with the right
      initial state, or created on demand at its trigger point
- [ ] TASK_LABELS entry (app/routers/dashboard.py) — a test enforces full
      coverage, the suite goes red without it
- [ ] TASK_GROUPS placement: "Automations" only if it is a real work item
      (it then counts in the dashboard x/N progress); alerts/reminders go in
      their groups so they don't inflate progress
- [ ] If dispatched: HANDLERS + _DISPATCH_ORDER (app/tasks/dispatch.py)
- [ ] If externally side-effecting: idempotency guard (see below) and, if
      idempotent, add to REQUEUEABLE_TASK_TYPES (app/tasks/scheduled.py) so
      the daily requeue can retry it
- [ ] Cancellation behavior: should _apply_cancellation SKIP it when
      unstarted? (Default yes for automations)
- [ ] New dashboard status value? .badge-<status> CSS rule (a test enforces)
```

## Invariants that must hold for every handler

- **Terminal-state discipline:** every handler exit path must leave the task
  in an explicit state. A claimed task whose handler returns without setting
  one strands IN_PROGRESS forever (nothing re-dispatches IN_PROGRESS).
- **`completed_at` accompanies COMPLETE.** Always. The dashboard shows it.
- **Idempotency before external side effects:** either an `external_ref`
  guard (skip the create if a ref exists — DocuSign/Seam pattern) or
  rollback-on-partial-failure (cleaner-sheet pattern). Without one of these
  the task must NOT be requeueable and R2-style races become duplicates.
- **Multi-trigger tasks need the atomic claim** (`app/tasks/claim.py`):
  any task reachable from more than one trigger (webhook + scheduled scan,
  like HOA_EMAIL) must claim `expect→IN_PROGRESS` before the side effect
  and release the claim on send failure so the other path can retry.
- **Record what the external API returned, not what was requested** — e.g.
  the door code shown on the dashboard is the Seam response value stored as
  a DataPoint, never re-derived from the phone in the UI.

## Date and time rules

- **All HOA/booking window comparisons use `today_et()`**
  (`app/integrations/hoa/window.py`) — never `date.today()`; the container
  runs UTC, which is already "tomorrow" from 8 PM ET.
- Store timestamps in UTC; convert to `America/New_York` at the edges
  (access-code windows, greetings, cron schedules).
- The HOA window's `latest` is a **scheduling deadline, not a send-blocker**
  (owner decision 2026-07-22): late-signed forms still send immediately.
  Don't reintroduce an upper cutoff.
- Parsed booking dates run through the poller's sanity guard (anchored to
  the email's own Date header). New parsers must feed it too.

## Adding a scheduled job

- Register in the lifespan (`app/main.py`) with an explicit `id`,
  `timezone="US/Eastern"` for crons, and `coalesce=True`; a lifespan test
  asserts registration — add one (pattern in
  `tests/unit/test_main_lifespan.py`).
- Job bodies: per-item try/except isolation, **one session per item** (a
  shared session's rollback expires every loaded instance →
  MissingGreenlet on the next iteration — this bug has happened twice).
- Any sync network call inside a job or handler goes through
  `asyncio.to_thread` — a blocked event loop makes APScheduler silently
  *discard* other jobs' runs.
- Log at INFO under the `app.` namespace; the health-report script greps
  these lines, so give new jobs a stable, distinctive log phrase and add it
  to `checking-production-health/reference/log-signals.md`.

## Alerts

New failure modes that a human must act on get an owner alert: a pure
`build_*` function plus a `send_*` wrapper in `app/ingestion/alerts.py`
(builder unit-tested on content), sent via `asyncio.to_thread`, failures
logged non-fatally. Silent failure states are this project's defining bug
class — when in doubt, alert.

## Docs and closeout

TDD per CLAUDE.md (failing test first — read the `testing-safely` skill
before writing tests). If the change contradicts CONTEXT.md or an ADR,
update the doc in the same change. Session Log entry on completion.
