# Home Rental Automation

A self-hosted Python service that runs the entire pre-arrival workflow for a
short-term rental property listed on Airbnb and VRBO.

A booking confirmation email arrives. The service parses it, creates the
booking, and opens a checklist of tasks against it — send the guest a DocuSign
registration form, program a time-bound door code on the smart lock, add a row
to the cleaners' schedule, email the signed form to the HOA inside a legally
awkward date window, and chase the owner for the two fields the listing
platforms refuse to put in a confirmation email. Then it works that checklist,
on its own, until the guest can walk in the door.

**It has been running unattended in production since 2026-07-21**, against real
bookings on a live listing. One VPS, one Docker Compose stack, no human in the
loop between "booking confirmed" and "guest checks in".

---

## Contents

- [What it actually does](#what-it-actually-does)
- [Architecture](#architecture)
- [The dashboard](#the-dashboard)
- [Domain rules that make this harder than it looks](#domain-rules-that-make-this-harder-than-it-looks)
- [Tech stack](#tech-stack)
- [Testing](#testing)
- [Deployment](#deployment)
- [How it was built](#how-it-was-built)
- [Quick start](#quick-start)
- [Repository map](#repository-map)

---

## What it actually does

The automation scope is **booking confirmation → guest check-in**. Everything
after check-in stays manual, on purpose — that is where a human is actually
useful.

| # | Task | Trigger | Integration |
|---|---|---|---|
| 1 | Detect and parse the booking | confirmation email lands in the feed inbox | Gmail API |
| 2 | Alert the owner, with a deep link to the fields only they can supply | immediately | Gmail API |
| 3 | Add a row to the cleaners' schedule, in chronological position | immediately | Google Sheets API |
| 4 | Send the guest the HOA registration form | as soon as a guest email exists | DocuSign eSign API |
| 5 | Program a door code, valid 4:00 PM check-in day → 11:00 AM checkout day | as soon as a guest phone exists | Seam API → Schlage lock |
| 6 | Email the signed form to the HOA | inside the HOA's send window | Gmail API |
| 7 | Chase unfilled fields and unsigned forms at 7 and 4 days out | scheduled | Gmail API |
| 8 | On cancellation: void the envelope, delete the door code, alert for the rest | cancellation email | DocuSign + Seam |

Each of those is a row in a `booking_tasks` table with its own state machine
(`pending → waiting → in_progress → complete / failed / skipped`), so the system
always knows what it has done, what it is waiting on, and what a human needs to
look at.

## Architecture

One FastAPI process holds the web app, the webhook receiver, and an APScheduler
instance. Postgres holds every booking, task and data point. Nothing else runs.

```mermaid
flowchart TD
    AB["Airbnb"] --> INBOX["Booking-feed Gmail inbox<br/>auto-forwarded confirmations"]
    VR["VRBO"] --> INBOX

    INBOX --> POLL["poll_booking_feed<br/>APScheduler, every 5 min"]
    POLL --> CLS{"classifier"}

    CLS -->|booking confirmation| PARSE["Airbnb / VRBO parsers"]
    CLS -->|cancellation| CANCEL["cancellation handler"]
    CLS -->|unrecognised| DEAD["dead-letter row<br/>+ weekly drift digest"]

    PARSE --> DB[("Postgres<br/>bookings · tasks · data-point provenance")]
    CANCEL --> DB

    DB --> DISPATCH["task dispatcher<br/>claim, then run one handler"]

    DISPATCH --> T1["send guest form"] --> DOCUSIGN(["DocuSign eSign API"])
    DISPATCH --> T2["create door code"] --> SEAM(["Seam API → Schlage lock"])
    DISPATCH --> T3["add cleaner row"] --> SHEETS(["Google Sheets API"])
    DISPATCH --> T4["email signed form to HOA"] --> GMAIL(["Gmail send API"])
    DISPATCH --> T5["owner alerts and reminders"] --> GMAIL

    DOCUSIGN -.->|Connect webhook, HMAC-verified| HOOK["POST /webhooks/docusign"]
    HOOK --> PDF["store the signed PDF"] --> DB

    DB --> DASH["dashboard<br/>FastAPI + Jinja2, Google OIDC + allowlist"]
    DASH -->|owner enters phone / email| DB

    SCHED["scheduled jobs<br/>HOA window hourly · reminders 08:00 ET<br/>requeue stalled 08:30 · verify door codes 09:00<br/>credential sentinel · token keep-alive · drift digest"] --> DB
```

Three things in that picture are worth calling out:

- **The classifier fails loudly, not silently.** An email that looks like a
  booking but will not parse becomes a dead-letter row *and* an owner alert. An
  email that does not look like a booking is dead-lettered quietly — but if it
  came from a platform domain it shows up in a weekly review digest, because
  "the platform changed its email format" is the failure mode that would
  otherwise take months to notice.
- **Tasks are claimed before they run.** A task moves to `in_progress` under a
  row lock, so a dashboard action and a scheduler tick cannot double-send an
  envelope or double-write a spreadsheet row.
- **Every field carries its provenance.** Guest name, phone, email and dates
  each record where they came from — email parse, manual entry, DocuSign
  webhook, Seam. When the system's picture of a booking is wrong, the dashboard
  says who told it that.

## The dashboard

Read-mostly, behind per-user Google sign-in with an email allowlist that is
re-checked on every request. The one write path is the contact form, because
that is the one thing the platforms will not give the system.

![Booking list](docs/images/dashboard-list.png)

Status is derived, not stored — it answers "who is the ball with": `action
needed` means the owner, `waiting on guest` means the signature, `scheduled`
means a date window, `failed` means look now.

![Booking detail with its task checklist](docs/images/booking-detail.png)

*(All data shown is fabricated for the screenshots — placeholder names, `555`
phone numbers and `example.com` addresses.)*

## Domain rules that make this harder than it looks

Most of the difficulty in this project was not the integrations. It was that
the real world has rules like these, and getting one of them subtly wrong means
a guest arrives at a locked door.

**The HOA send window.** The signed form may be emailed no earlier than 7 days
before check-in, and no later than the last day that still leaves two full
*HOA-open* days strictly between the send day and check-in. The HOA is closed
Sundays. The send day itself never counts as lead time and may itself be a
closed day. A Sunday check-in therefore has a Thursday deadline. Every
comparison uses the US/Eastern calendar date, never the server clock — the
production container runs UTC, which is already tomorrow from 8 PM ET.

![HOA registration panel](docs/images/hoa-panel.png)

And late signatures still send: the deadline is the last *acceptable* day for
the scheduled send, not a send-blocker. Late is better than never.

**Door codes are time-bound.** Active from 4:00 PM on check-in day to 11:00 AM
on checkout day, US/Eastern. Stored in UTC, converted at the Seam boundary. A
daily job re-reads the lock to confirm the code is actually on the device,
because Seam programs hardware asynchronously and "the API accepted it" is not
the same as "the guest can get in".

**The cleaners' spreadsheet has an opinion.** Rows are chronological by
check-in date, new rows are inserted at the correct position rather than
appended, and there is a sentinel row that must stay at the bottom. It is
someone else's spreadsheet; the system writes four columns and touches nothing
else.

**Some fields simply are not in the email.** Airbnb withholds the guest's phone
number and email address; VRBO withholds the email. There is no supported API
for a co-host to fetch them. Scraping was tried and
[deliberately abandoned](docs/adr/0004-manual-data-entry-replaces-scraping.md) —
so the design makes the gap explicit: downstream tasks sit in `waiting`, the
owner gets a deep link, and reminders escalate at 7 and 4 days out.

**Cancellation is half automatic on purpose.** Voiding the DocuSign envelope
and deleting the door code are safe to automate. Un-emailing an HOA and
removing a row from someone else's spreadsheet are not, so those become alerts
with instructions instead.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Web | FastAPI + Jinja2 templates, hand-written CSS, no build step |
| Database | PostgreSQL 17 via SQLAlchemy 2 async |
| Migrations | Alembic, applied on container start |
| Scheduler | APScheduler, in-process |
| Auth | Google OIDC (Authlib) + signed session cookie + config allowlist |
| Tests | pytest + pytest-asyncio |
| Runtime | Docker Compose; Caddy for TLS |
| Integrations | Gmail API, DocuSign eSign + Connect, Seam, Google Sheets |

No frontend framework, no message broker, no Redis, no Celery. One property,
one owner, a handful of bookings a month — the scheduler and Postgres are
enough, and every component removed is a component that cannot wake anyone at
3 a.m.

## Testing

**TDD is a hard rule in this repo, not an aspiration.** Every production change
starts with a failing test. The test suite is more than twice the size of the
application code, and it runs in CI on every push.

```bash
make test          # offline: unit + integration, no credentials, ~15s
make test-live     # the same integration scenarios against real sandbox APIs
```

Three properties the suite is built around, each of which was learned the hard
way and is documented in `TESTING.md`:

- **The offline suite needs no credentials and no network.** External responses
  are replayed from recorded fixtures, so a stranger can clone this repo and
  get a green run. CI proves that claim on every push.
- **Test data contains no real people.** Placeholder names, `555` phone
  numbers, `example.com` addresses, and expectations derived from
  `tests/fixtures/config.test.yaml` rather than hardcoded — a test asserting
  the operator's real email address is a test coupled to facts it has no
  business knowing.
- **Live tests are opt-in and guarded.** The test environment can load real
  credentials, so any path that sends email or touches DocuSign, Seam or
  Sheets is mocked at the calling module by default. The conventions are
  written down in `.claude/skills/testing-safely/`.

Fixtures for the email parsers came from real messages, and that mattered: the
VRBO parser passed against synthetic fixtures for weeks while being unable to
parse a single real VRBO booking, because real emails label fields with a
trailing colon and the fixtures did not. That bug was caught by a live alert,
not by a test.

## Deployment

One `docker compose up` on any Linux host with Docker:

- `app` — FastAPI + APScheduler, published on loopback only
- `db` — PostgreSQL 17 on a named volume
- `caddy` — TLS termination and reverse proxy

The entrypoint runs `alembic upgrade head` before uvicorn, so a fresh database
migrates itself and a deploy carrying a new migration applies it. Startup then
runs a credential guard that refuses to boot on a missing credential, a
placeholder `SECRET_KEY`, or an unconfigured `DATABASE_URL` — naming the
offending field. A service that runs unattended should fail at boot, loudly,
rather than at 3 a.m., quietly.

**Portability is a requirement, not a nice-to-have.** Moving to a new host is
`pg_dump` → copy the repo, `.env`, `config.yaml` and the backup files →
`docker compose up` → restore. No vendor-specific steps, no managed services to
re-provision.

Because the app cannot report its own death — every alert it sends travels
through its own Gmail token — the outermost layer of monitoring is external:
uptime checks from outside the host, and dead-man's-switch heartbeats that page
when a job stops succeeding rather than when it fails. Inside the app, a daily
credential sentinel probes every integration read-only, a daily job retries
failed automations and digests the stuck ones, and a monthly status email
proves the send path still works — its *absence* is the alarm.

Full procedures — backup, restore, off-site copies, migration, monitoring,
credential rotation: [`docs/operations.md`](docs/operations.md).

## How it was built

[**`GETTING-TO-PRODUCTION.md`**](GETTING-TO-PRODUCTION.md) is the honest
version of this project.

It is the 22-step runbook that took the codebase from "the tests pass on my
laptop" to a service handling real money and real guests, with a Session Log
entry for every step: what broke, what the wrong assumption was, and what the
fix was. Among other things it records the day the dashboard's stylesheet was
discovered to have never once reached a browser (Caddy terminated TLS, uvicorn
ran without `--proxy-headers`, the app built an `http://` stylesheet URL inside
an `https://` page, and every browser silently blocked it as mixed content —
no test could see it, because `TestClient` never enforces a scheme); the
DocuSign anti-fraud filter that voided the go-live envelope because the
account's admin address was a `@gmail.com` one; and the 17-finding bug hunt
that followed go-live.

It is long, and it is the most useful thing in this repository.

Also worth a look:

- [`CONTEXT.md`](CONTEXT.md) — the domain glossary and task graph. Written
  before the code, and kept true to it.
- [`docs/adr/`](docs/adr/) — architecture decisions, including the one where
  web scraping was built, evaluated and thrown away.
- [`docs/risk-register.md`](docs/risk-register.md) — every known risk with its
  final disposition: fixed, accepted, or deferred with a revisit trigger.

## Quick start

```bash
cp .env.template .env                # fill in credentials
cp config.example.yaml config.yaml   # fill in property settings
make setup                           # install the git hooks
docker compose up
```

The dashboard is on `https://localhost` (Caddy serves an internal certificate
locally) or `http://localhost:8000` direct. `docs/credential-setup.md` walks
through obtaining each credential; there are more of them than you would like.

## Repository map

```
app/
  ingestion/      poller, classifier, Airbnb + VRBO parsers, cancellation
  tasks/          state machine, claim + dispatch, handlers, scheduled jobs
  integrations/   gmail, docusign, seam, sheets, hoa (window math + email)
  routers/        dashboard, auth, webhooks, health
  db/             SQLAlchemy models and session
  templates/      Jinja2 + one hand-written stylesheet
tests/            unit · integration · e2e
alembic/          migrations
docs/             ADRs, operations, credential setup, risk register, flow map
scripts/          backup, restore, off-site backup, manual drivers, git hooks
.claude/skills/   task-specific runbooks used while developing this repo
```

---

## Further reading

- [`CLAUDE.md`](CLAUDE.md) — development conventions, TDD rules, domain
  invariants that must not be broken in code
- [`CONTEXT.md`](CONTEXT.md) — domain glossary and task graph
- [`TESTING.md`](TESTING.md) — suite layout, offline vs live selection
- [`docs/operations.md`](docs/operations.md) — deploy, back up, migrate, monitor
- [`docs/implementation-plan.md`](docs/implementation-plan.md) — the phased
  build plan
- [`docs/flow-map.md`](docs/flow-map.md) — end-to-end trace of a booking
- [`GETTING-TO-PRODUCTION.md`](GETTING-TO-PRODUCTION.md) — the build-to-production
  runbook and its Session Log
