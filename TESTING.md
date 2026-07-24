# Testing

This project's test suite is **offline by default**: the everyday `make test`
needs **no external credentials** and never touches a real API. Live sandbox
verification is a deliberate opt-in (`--run-live` / `RUN_LIVE=1`).

> TL;DR
> - `make test-unit` — pure logic, no DB, no creds.
> - `make test` — offline suite (unit + integration). No creds. Needs local Postgres.
> - `make test-integration` — everything **live** against the sandbox. Needs `.env` creds.

---

## Suite layout

| Path | What it is | Needs Postgres? | Needs sandbox creds? | Default run |
|---|---|---|---|---|
| `tests/unit/` | Pure logic, no I/O. External clients fully mocked. | No | No | **Runs** |
| `tests/integration/` | DB-backed behavior against the local Postgres **test** DB; external HTTP boundaries mocked. Includes the per-integration isolation tests (`test_live_*.py`), which run **offline against recorded responses** by default. | Yes | No | **Runs** (offline) |
| `tests/e2e/` | The full booking workflow against **real sandbox** APIs — the go-live gate. | Yes | Yes (all 11) | **Skipped** unless live |

Offline, the whole suite is credential-free. The `@pytest.mark.live` tests
(the four `test_live_*.py::*_live_roundtrip` cases and everything under
`tests/e2e/`) are **skipped** unless you opt into live mode.

---

## Offline vs live — how it's selected

A test is **live** if it is marked `@pytest.mark.live` or lives under
`tests/e2e/` (auto-tagged). Live tests are skipped unless the run opts in via
**either**:

- `pytest --run-live`, or
- `RUN_LIVE=1` in the environment (what the Makefile's live targets export).

The per-integration isolation tests (`test_live_sheets.py`, `test_live_seam.py`,
`test_live_docusign.py`, `test_live_gmail.py`) each ship **two** tests that share
one round-trip body:

- `test_*_offline_roundtrip` — **always runs**, credential-free. It drives a
  **stateful fake** (`tests/integration/fakes.py`) seeded from a hand-written
  recording in `tests/fixtures/recorded/`. The fake keeps enough in-memory state
  that a full `read → create → verify → delete` round-trip behaves like the real
  service, so the offline path exercises the real orchestration/parsing code —
  only the transport is substituted. (This is the hand-written-fixture
  equivalent of a VCR cassette.)
- `test_*_live_roundtrip` — `@pytest.mark.live`; the **same** round-trip against
  the real sandbox. Skipped offline.

---

## Running the offline suite (no credentials)

Requires only a local Postgres with the **test** database. The repo's
`docker compose` `db` service (postgres:17 on `127.0.0.1:5432`) is the intended
host. One-time: create the test DB (separate from the app DB so tests can freely
create/drop the schema):

The suite's default connection is
`postgresql+asyncpg://rental_automation:devpassword@127.0.0.1:5432/rental_automation_test`,
so on a fresh clone set `POSTGRES_USER=rental_automation` and
`POSTGRES_PASSWORD=devpassword` in `.env` before starting the `db` service
(the compose service reads them), then create the test database:

```bash
docker compose up -d db
docker compose exec -T db createdb -U rental_automation rental_automation_test
```

Any other Postgres works too — point `TEST_DATABASE_URL` at it and everything
follows, because `tests/conftest.py` defaults the app's own `DATABASE_URL` from
it. (They must be the same database: the fixtures create the schema on one
engine while the code under test opens sessions on the other, so a mismatch
fails every DB-backed test with `relation "bookings" does not exist`.)

```bash
docker run -d --rm --name rental-test-db -p 55432:5432 \
  -e POSTGRES_USER=t -e POSTGRES_PASSWORD=t -e POSTGRES_DB=t postgres:17
export TEST_DATABASE_URL='postgresql+asyncpg://t:t@127.0.0.1:55432/t'
```

Each test creates the schema from the SQLAlchemy metadata and drops it after —
no migrations needed for tests.

Then:

```bash
make test-unit   # fastest signal: pure unit tests
make test        # offline suite: unit + integration (recorded responses)
```

`make test` passing with **no `.env` present** is the contract — the offline
suite must never depend on a credential.

### Recorded platform emails are gitignored (expect skips on a fresh clone)

`tests/fixtures/emails/*.eml` are genuine Airbnb/VRBO messages and carry real
guest names, phone numbers and confirmation codes, so they are **gitignored and
never committed**. The tests that replay one call
`tests/conftest.py::recorded_email_bytes`, which **skips** with a naming reason
when the file is absent instead of failing — so a clean checkout goes green with
a block of skips rather than red.

What those tests add is fidelity to *real* mail, and that has caught real bugs:
the VRBO parser once passed against synthetic fixtures while silently dropping
every real booking. Each such lesson is therefore also pinned by an inline
regression test built from a sanitized message (e.g.
`test_parse_vrbo_real_colon_label_format`), so the behaviour stays covered
without the recordings — only the provenance is lost. To restore full coverage,
drop real `.eml` exports into `tests/fixtures/emails/` using the filenames the
tests request.

---

## Live tests & the E2E gate

Live runs hit the **real sandbox** and require sandbox credentials in `.env`.
See `docs/credential-setup.md` and `GETTING-TO-PRODUCTION.md` (Steps 6–13) for
how to obtain each one.

### The 11 required sandbox credentials

```
GOOGLE_CLIENT_ID              GOOGLE_CLIENT_SECRET
GMAIL_BOOKING_FEED_REFRESH_TOKEN  GMAIL_ALERTS_REFRESH_TOKEN
GOOGLE_SHEETS_REFRESH_TOKEN
DOCUSIGN_ACCOUNT_ID  DOCUSIGN_CLIENT_ID  DOCUSIGN_CLIENT_SECRET
DOCUSIGN_REFRESH_TOKEN  DOCUSIGN_HMAC_KEY
SEAM_API_KEY
```

`config.yaml` must also point at test-safe resources: a throwaway Sheets tab, a
Seam **sandbox** virtual device, and the sandbox DocuSign template
(`DOCUSIGN_SANDBOX=true`).

### Commands

```bash
make test-live         # live per-integration isolation round-trips only
make test-e2e          # the full live E2E go-live gate only
make test-integration  # unit + integration + E2E, everything live
```

Each live isolation test uses clearly-fake, self-cleaning data:

- **Sheets** — inserts a far-future `ZZ TEST — delete me` row above the sentinel, verifies chronological placement, deletes it (search-by-marker teardown, safe on failure).
- **Seam** — creates a time-bound code on the sandbox device, verifies the ET→UTC window, deletes it (polls for async removal).
- **DocuSign** — sends an envelope from the configured template, confirms `sent`, then **voids** it.
- **Gmail** — confirms the alerts-account profile and sends one labelled test message **to that same account** (self-send — a live run never emails an external recipient).

> Live-Gmail note: the integration `_block_live_gmail` safety net normally makes
> any real Gmail call fail loudly. It steps aside **only** for a `live`-marked
> test running under `--run-live` — every other test, and every offline run,
> stays guarded.

### Interpreting the credential hard-fail

The E2E suite **hard-fails** (it does not skip) when run live with any of the 11
credentials missing:

```
AssertionError: E2E tests require sandbox credentials in .env. Missing: [...].
```

This is intentional (design D-01): a silently *skipped* go-live gate could be
mistaken for a *passing* one. If you see this, fill the named variables in
`.env` and re-run. Offline (`make test`), the E2E tests are simply skipped and
this assertion never fires.

---

## Manual / operational scripts (`scripts/manual/`)

The Phase 2 per-integration isolation scripts have been **folded into the pytest
suite** (the `test_live_*.py` files above) — run those instead. The scripts that
remain are operational, not test duplication:

| Script | Purpose |
|---|---|
| `get_gmail_refresh_tokens.py` | One-time: mint the two Gmail refresh tokens (`--account booking-feed\|alerts`). |
| `get_docusign_refresh_token.py` | One-time: mint the DocuSign refresh token (auth-code grant). DocuSign refresh tokens expire in 30 days. |
| `e2e_full_flow.py` | Interactive full-flow driver used by the go-live steps (`far_future`, `in_window_send`, `in_window_complete`, `in_window_hoa`); prints DB state after each step. See `docs/flow-map.md`. |
| `golive_e2e.py` | Go-live item 7 orchestration (`send`/`verify`/`teardown`), run inside the app container against the live prod DB. |
| `unsuspend_seam_sandbox.py` | Recovery: reactivate a Seam **sandbox** workspace that auto-suspended after idle days. |

> ⚠️ `e2e_full_flow.py` and `golive_e2e.py` send **real email**. They have no built-in
> recipients: the `in_window` modes require `E2E_GUEST_EMAIL` (DocuSign signer) and
> `E2E_HOA_RECIPIENT` (HOA override), and abort with a clear message when either is
> unset. `golive_e2e.py` additionally refuses to run unless `config.yaml`'s HOA email
> equals `E2E_HOA_RECIPIENT`, so a live run can never reach the real HOA. See
> `.env.template`.

---

## Recorded fixtures

Offline replay data lives in `tests/fixtures/recorded/`:

- `sheets_schedule.json` — a cleaner-schedule read (columns A–F + sentinel row).
- `external_responses.json` — Seam / DocuSign / Gmail response shapes.

These mirror the real sandbox payload **shapes** proven in the Phase 2 live tests; the
values themselves are synthetic placeholders, never real ids or addresses.
To refresh one, run the corresponding live test (`make test-live`) with logging,
observe the real response, and update the JSON to match. Keep them **secret-free**
— they are recordings of *shapes*, not captured credentials or tokens.
