---
name: triaging-stuck-tasks
description: Diagnoses and safely resolves stuck, FAILED, or stranded booking tasks and dead-lettered emails in the rental-automation production DB — including when a task state may be reset and how to inspect DocuSign, Seam, and Sheets first. Use when a task is stuck or FAILED, a stalled-automations digest or dead-letter alert email arrived, or the user asks why an automation didn't run.
---

# Triaging stuck tasks

This is a **low-freedom procedure**. The stakes: a wrong reset can create a
**duplicate DocuSign envelope** (consumes the finite annual send quota) or a
**duplicate/live door code on a real lock**.

## The one rule that must never be broken

**Never reset an IN_PROGRESS task to PENDING without inspecting the external
system first.** IN_PROGRESS means a dispatcher claimed the task and may have
died between the external create succeeding and the `external_ref` commit —
the side effect may exist with no record. A blind reset is the only path to a
silent duplicate. (Risk register, "Residual partial-failure edge".)

## Step 1 — Establish the facts (read-only)

On the VPS repo root (`set -a; . ./.env; set +a` first), or via
`docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"`:

```sql
SELECT bt.task_type, bt.state, bt.attempt_count, bt.external_ref,
       bt.last_error, bt.updated_at, b.status, b.check_in_date
FROM booking_tasks bt JOIN bookings b ON b.id = bt.booking_id
WHERE b.id = '<booking-uuid>';
```

`last_error` usually names the real cause. Read it before anything else.

## Step 2 — Inspect the external system for the task type

Run these **read-only** checks inside the app container (real API GETs, no
writes). Get the `external_ref` from Step 1.

**DOCUSIGN_SEND** — does the envelope exist, and in what state?

```bash
docker compose exec app python -c "
from app.integrations.docusign.client import get_envelope_api
api, acct = get_envelope_api()
e = api.get_envelope(acct, '<external_ref>')
print(e.status, e.sent_date_time)"
```

**ACCESS_CODE_CREATE** — does the code exist on Seam, and is it on the lock?

```bash
docker compose exec app python -c "
from app.integrations.seam.client import get_seam_client
c = get_seam_client().access_codes.get(access_code_id='<external_ref>')
print(c.status, getattr(c, 'errors', None))"
```

**CLEANER_SHEET_ADD** — no external_ref is stored; check the sheet itself
for a row with the guest's name (ask the user to eyeball it, or read the
sheet via the app's Sheets client). Duplicate rows must be removed by hand.

**HOA_EMAIL** — check the alerts Gmail account's Sent folder for the
registration email (the user can do this fastest).

## Step 3 — Resolve by state

**IN_PROGRESS (stuck >24h):**
- External side effect **exists** → the crash hit after the create. Record
  reality: set the task COMPLETE with the external ref
  (`UPDATE booking_tasks SET state='complete', external_ref='<ref>',
  completed_at=now() WHERE id='<task-uuid>';`). Get the ref from the external
  system (Seam list / DocuSign sent items filtered by the guest).
- Side effect **does not exist** → safe to reset to PENDING; the daily
  requeue job (08:30 ET) or a re-submit of the booking's contact form will
  re-dispatch it.

**FAILED:**
- `attempt_count < 5`: the daily requeue retries it automatically —
  usually do nothing unless check-in is imminent. To retry NOW, fix the
  underlying cause (see `last_error`; credential problems →
  `recovering-credentials`), then either re-submit the booking's contact
  form on the dashboard (re-dispatches all PENDING) or flip
  FAILED→PENDING manually. Manual flips are safe **only** for
  CLEANER_SHEET_ADD / DOCUSIGN_SEND / ACCESS_CODE_CREATE — their handlers
  are idempotent (external_ref guards; sheet insert rolls back on partial
  failure).
- `attempt_count >= 5`: retries are exhausted by design; the stalled digest
  emails daily. Fix the cause or do the step by hand, then flip to PENDING
  (one more attempt) or mark SKIPPED with a note if handled manually.
- **HOA_EMAIL is different**: its retry loop is the hourly scan, not the
  requeue job. FAILED here is unusual — a transient send failure releases
  the claim back to WAITING automatically. Investigate before touching.

**PENDING that never ran:** the dispatch was lost (e.g. restart between the
contact-save commit and the background task). The daily requeue re-dispatches
these; to force it now, re-submit the booking's contact form (even with no
new values) — that unconditionally re-dispatches PENDING automations.

**WAITING:** not stuck — it's waiting on a precondition (guest email/phone
missing, or the HOA window/signed form). Check the booking's fields before
assuming a fault.

## Dead-lettered emails (processed_messages)

```sql
SELECT message_id, disposition, classified_as, error, created_at
FROM processed_messages ORDER BY created_at DESC LIMIT 20;
```

| Disposition | Meaning | Action |
|---|---|---|
| `other` | Inbox noise | None. Never reprocess. |
| `parse_error` | A booking email that wouldn't parse | Owner was alerted; the booking is entered manually. If the parser has since been FIXED, reprocess (below). |
| `cancellation_parse_error` | A cancellation with no readable ID | Booking is still ACTIVE in the system — confirm every cleanup happened manually (code deleted, envelope voided, HOA/cleaner told). |
| `classify_error` | Message couldn't even be classified | As parse_error. |
| `duplicate` | Re-sent confirmation for an existing booking | Usually nothing. **If the resend was a date-change notice, the stored dates are stale** — verify against the platform. |

**Reprocess a dead-lettered message** (only after fixing the cause): delete
its row — the poller re-fetches anything not in `processed_messages` on the
next 5-minute cycle:

```sql
DELETE FROM processed_messages WHERE message_id = '<gmail-id>';
```

Never do this for `duplicate` (it will just dead-letter again) or for a
message whose manual handling is already complete (it would double-apply).

## Closeout

Note what was found and changed in the Session Log (no guest PII), and if
the triage exposed a new failure mode, fold it into this skill so the next
session starts from it.
