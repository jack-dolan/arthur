---
name: testing-safely
description: Safety rules and mock-boundary conventions for writing and running tests in this repo, whose test environment loads REAL production credentials. Use when writing new tests, doing TDD RED runs, or testing any path that sends email or touches DocuSign, Seam, or Google Sheets.
---

# Testing safely

**The test environment loads the real production `.env`.** Any unmocked path
to an external client sends real email, creates real door codes, real
envelopes, real sheet rows. This has happened **twice** (an integration test
in Step 14; a unit-test RED run on 2026-07-22). Both leaks came from tests
whose author believed the dangerous path was covered.

## Non-negotiables

1. **Mock at the module that CALLS the boundary**, not at its source.
   Modules that do `from app.integrations.gmail.oauth import
   get_alerts_service` at import time hold their own binding — patching the
   source module silently does nothing.
   - Right: `patch("app.ingestion.cancellation.get_alerts_service", ...)`
   - Wrong: `patch("app.integrations.gmail.oauth.get_alerts_service", ...)`
     when the caller imported the name at module level.
2. **A test that will be run RED must dead-end its side-effect paths
   explicitly.** RED runs execute paths you haven't finished mocking — that
   is exactly how the second leak happened. Patch the alert/send seams even
   when you believe the code "can't reach them yet".
3. **Never rely on the conftest guard alone.** It is a safety net, not a
   mock strategy.

## Test data: no real people, ever

Test files are published, so no real name, email, phone, address, confirmation
code, GUID, or message id belongs in `tests/` or `scripts/manual/`. Conventions:

- Emails → `*@example.com`. The platform senders (`automated@airbnb.com`,
  `no-reply@vrbo.com`, `sender@messages.homeaway.com`,
  `vrbo@partners.expediagroup.com`, `dsdevcenter@docusign.com`) are
  load-bearing for the classifier and stay as-is.
- Phones → `555…`; guest names → `<First> Example`; codes → obviously-fake
  bodies in the real shape (`HMFAKE0001`, `HA-XXXXXX`); GUIDs/message ids →
  zeroed, shape preserved.
- **A value that comes from config must be derived from config, not
  hardcoded.** `tests/fixtures/config.test.yaml` is the source; load it and
  read the field (see `test_auth.py`'s allowlist, `test_hoa_email.py`'s HOA
  address / alerts sender / `signature_name`, `test_dashboard_routes.py`'s
  stub user). A test asserting a literal the operator can change in
  `config.yaml` is testing the wrong thing.
- Values that must come from a real inbox (the manual `scripts/manual/`
  drivers, which send live email) read a **required** env var —
  `E2E_GUEST_EMAIL`, `E2E_HOA_RECIPIENT` — and abort when unset. Never add a
  personal-address default.

Verify before committing: re-read the **staged** diff for real names, emails,
phone numbers, addresses, confirmation codes, GUIDs and message ids. The
gitignored `.eml` fixtures under `tests/fixtures/emails/` are the usual source
of a leak — anything copied out of one must be rewritten before it lands in a
tracked file.

## The guard (know it, keep it healthy)

`tests/conftest.py::_block_live_external_apis` is autouse for **every** test
scope and blocks all four integrations at construction choke points: Gmail
`oauth.build`, Sheets `client.build`, the `Seam` constructor, and the
DocuSign module's `httpx` binding (the token exchange gates every DocuSign
call). A nested `patch(...)` inside a test overrides the guard bindings, so
normal mocking is unaffected.

- `tests/unit/test_conftest_guards.py` pins all four guards tripping. If you
  refactor any guarded binding (move an import, rename a constructor path),
  that test failing is the guard telling you to re-point it — never delete
  or skip it.
- Escape hatch: `@pytest.mark.live` under `--run-live`/`RUN_LIVE=1` opts a
  test into the real sandbox on purpose. Everything under `tests/e2e/` is
  auto-tagged live.
- DocuSign httpx mocks must target the module binding
  (`app.integrations.docusign.client.httpx.post`) — a global
  `patch("httpx.post")` no longer reaches the code under test.

## Suite layout and commands

- `make test` — offline default (unit + integration, recorded fakes, no
  creds needed). This is the green gate for every change.
- `make test-live` / `make test-e2e` / `make test-integration` — live
  against the sandbox/real APIs. Only run when the user asks; never as a
  routine check.
- Integration tests use the local Postgres test DB; unit tests must not
  need the DB.

## Mock gotchas that have burned real sessions here

- `AsyncMock` chains: `session.execute.return_value` is itself an
  AsyncMock, so `.scalar_one_or_none()` returns a **truthy coroutine**, not
  your configured value. Build the result explicitly:
  `session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))`.
- `patch.object(settings, "field", ...)` restores the real value at context
  exit — assert **inside** the `with` block.
- After a real `session.rollback()` the whole identity map is expired;
  reading any attribute on an AsyncSession-bound instance then raises
  `MissingGreenlet`. Capture needed values before the risky call.
- Event-loop canary tests: schedule the canary coroutine **before** the
  coroutine under test in `asyncio.gather`, or the test may pass vacuously.

## TDD flow (per CLAUDE.md, with the safety addition)

1. Write the failing test — with side-effect seams explicitly patched.
2. Run it RED; **read the failure**: it must fail on the assertion you
   intended, not on setup (a wrong-reason RED proves nothing).
3. Implement; run GREEN; then `make test` for the full offline suite.
