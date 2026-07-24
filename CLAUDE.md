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

### Keep the docs honest
If implementation reveals something that contradicts `CONTEXT.md` or an ADR, update the doc before moving on. The docs are the source of truth for domain decisions; the code is the source of truth for implementation.

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
3. `pg_restore` on new host
4. `docker compose up`

Migration scripts live in `scripts/`. Keep them up to date.
