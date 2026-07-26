# Rental Automation — Claude Instructions

## What this project is

A Python service that automates the pre-arrival workflow for short-term rental properties listed on Airbnb and VRBO. The automation scope is **booking confirmation → guest check-in**. After check-in, all guest interaction is manual.

Read `CONTEXT.md` for the full domain glossary and task graph. Read `docs/implementation-plan.md` for the phased build plan. Read `config.example.yaml` to understand the shape of per-property configuration.

## How to work in this project

### TDD is non-negotiable
Write a failing test before writing any production code. No exceptions. Use red-green-refactor throughout. Run `pytest` after every meaningful change and do not move on while tests are red.

Test layout:
- `tests/unit/` — pure logic, no I/O
- `tests/integration/` — real external calls, use recorded fixtures or feature flags
- `tests/e2e/` — full booking workflow from injected email to completed task checklist

Coverage target: 90%+ on unit tests.

### Check the plan before starting
Always read `docs/implementation-plan.md` before beginning a phase. Each phase has a "Done when" criterion — don't declare a phase complete until it's met.

### Config values come from config.yaml
Sensitive values (names, addresses, HOA email, sheet name, API keys) live in `config.yaml`, which is gitignored. `config.example.yaml` is the committed template. Never hardcode these values in source files.

### Commit clean
- `config.yaml` must never be committed
- `example-emails/` must never be committed
- `.claude/settings.local.json` must never be committed (machine-local); shared
  `.claude/` material — skills, commands, `settings.json` — IS committed
- All secrets go in `.env` (also gitignored)

### Privacy rules

Guest personal data — names, phone numbers, email addresses, booking
confirmation codes, street addresses — **never goes into a committed file**.
Git history is permanent: a value committed once is committed forever, and it
outlives whatever made it seem harmless at the time. The leak path that
matters here is not the code, it is prose — session notes and runbook entries
written while narrating real work.

- **Docs and Session Log entries** name a booking by its external-id *shape*
  (`HMXXXXXXXXX`, `HA-XXXXXX`) or just call it "the guest". Never by name.
- **Tests and fixtures** use placeholder values (`*@example.com`, `+1 (555)
  01x-xxxx`, `<First> Example` names) and derive config-owned values from
  `tests/fixtures/config.test.yaml` instead of hardcoding them. The
  `testing-safely` skill has the full conventions.
- **Real guest material stays gitignored** — `example-emails/`,
  `tests/fixtures/emails/`, `config.yaml`, `.env`, `.secrets/`. Never
  `git add --force` any of them.

A **pre-commit hook enforces this**, scanning only the lines a commit *adds*
(so material already in history does not re-flag, and deleting it is not a
violation). It blocks on: a literal from the local name denylist
(`.secrets/denylist-guest.txt` — gitignored, so a fresh clone will not have
it; the hook then prints a notice and stands down rather than blocking), a
real-looking phone number (555 placeholders exempt), or a staged path under
one of the private-by-design directories above. Install it once per clone
with `make setup` or `bash scripts/install_hooks.sh` — git hooks are not part
of a repository and do not survive a clone. A deliberate, reviewed exception
is `git commit --no-verify`.

### Keep the docs honest
If implementation reveals something that contradicts `CONTEXT.md` or an ADR, update the doc before moving on. The docs are the source of truth for domain decisions; the code is the source of truth for implementation.

### Database migrations

Every migration must be **N-1 compatible**: after the new schema is applied,
the immediately previous release's code must still read and write it safely.
Redeploying the previous image is the whole rollback; the database is never
restored as part of a code rollback because that would discard newer writes.
Applied migrations are immutable, and the rule binds to new migrations only.

Migrations never run at application boot. Boot runs a read-only schema guard:
an exact head starts, a revision unknown to the build warns and starts (the
database is ahead after rollback), and a known older revision refuses to start.
Deployments explicitly run the image's idempotent `migrate` command before
starting its app process.

Breaking changes use expand/contract across releases:

- Rename: add the new column, dual-write, backfill, switch reads, then drop the
  old column releases later.
- Drop: ship code that no longer references the object first; drop it in a
  later release.
- `NOT NULL`: add nullable, backfill, add a `CHECK ... NOT VALID`, then
  `VALIDATE CONSTRAINT` separately so validation does not hold the original
  long-lived lock. Only tighten the column after old code no longer writes
  nulls.
- Type change: add a shadow column and move writes, data and reads in stages.

PostgreSQL enum additions have a semantic compatibility trap: new code can
write a value the old Python enum does not understand. Every enum-backed model
must use tolerant deserialization, and every dashboard read path must show an
unknown label/badge instead of raising. Adding a task type still requires a
`TASK_LABELS` entry for the new release. After rollback, rows only the newer
code understands semantically render as `unknown` under the old code. That
visible loss of meaning is acceptable; a crash or unsafe write is not.
Rollback validity is therefore about semantics as well as schema shape.

CI renders each new revision with Alembic offline mode and runs pinned Squawk
defaults over the SQL. A revision that genuinely cannot render offline (for
example, a Python data backfill) must carry this exact one-line marker inside
the revision:

```python
# migrations-lint: offline-skip - <specific one-line justification>
```

The skip is printed visibly by CI. Never add the marker merely to silence a
Squawk finding; fix the DDL or use an explicit, reviewed Squawk suppression.

### Reporting finished work

A closeout report has two parts: what you did, and what is still open. Both
parts are for a reader who has no context on the session.

**Count the open items and say the number.** If there are five, write "five
open items" and number them one to five. Do not put four items under a heading
that says "two things". Do not introduce a new open item inside the
explanation of a different one — if it deserves a mention, it deserves its own
numbered entry.

**Write every open item in four parts:**

1. **What the problem is.** Plain language, no shorthand.
2. **Why it matters.** The concrete consequence, and when it would actually
   bite. If the answer is "it probably never will", say that.
3. **How it could be fixed.** Give the options you can see, with the cost or
   risk of each. One option is fine if there genuinely is only one.
4. **What you recommend.** Fix it now, fix it later, or leave it alone. If
   later, name the place you have written it down where it will be read again
   — a future step's prompt, an issue, a checklist item. A paragraph buried in
   a Progress Log does not count, because nobody re-reads those before working.

**Style.** Short sentences. Explain the thing, then stop. No jargon, no
clipped half-sentences, no closing flourish that restates what you just said.
Prefer a number over an adjective: "480 MB, down from 699 MB" beats
"significantly smaller".

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ |
| Web framework | FastAPI with Jinja2 templates |
| Database | PostgreSQL via SQLAlchemy async |
| Migrations | Alembic |
| Background scheduler | APScheduler |
| Tests | pytest + pytest-asyncio |
| Containerization | Docker Compose |

## Key external integrations

| Integration | Purpose | Credential location |
|---|---|---|
| Gmail API (OAuth2) | Poll booking feed inbox; send alert and HOA emails | `.env` |
| DocuSign eSign API | Send guest form envelope; receive signed webhook; retrieve PDF | `.env` |
| Seam API | Create/delete time-bound Schlage access codes | `.env` |
| Google Sheets API | Write rows to cleaner schedule sheet | `.env` |

## Domain rules that must not be broken in code

- **HOA email window**: no earlier than 7 days before check-in, no later than 2 HOA-open days before check-in. HOA is closed Sundays. The window calculation must account for this.
- **Access code timing**: active from 4:00 PM check-in day to 11:00 AM checkout day, US/Eastern. Store timestamps in UTC; convert for Seam.
- **Airbnb phone**: not included in Airbnb confirmation emails; entered manually by the owner via the dashboard.
- **VRBO email**: not included in VRBO confirmation emails; entered manually by the owner via the dashboard.
- **Cleaner schedule row order**: rows are chronological; new rows are inserted at the correct position, always above the sentinel row at the bottom.
- **Cancellation**: DocuSign void and Seam code deletion are automatic; HOA and cleaner sheet cleanup are alert-only (human action required).

## Portability

This service must be fully migratable to a new VPS. Migration procedure:
1. `pg_dump` on old host
2. Copy repo + `.env` + `config.yaml` to new host
3. Start PostgreSQL and `pg_restore` on new host
4. Run the image's explicit `migrate` command
5. Start the app and proxy

Migration scripts live in `scripts/`. Keep them up to date.
