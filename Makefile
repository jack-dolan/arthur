# Rental Automation — test targets (see TESTING.md for the full guide)
#
# Offline is the default everywhere: `make test` needs NO external credentials
# and replays recorded responses for the integration round-trips. The live
# targets opt in with RUN_LIVE=1 + --run-live and require sandbox creds in .env.

PYTEST := .venv/bin/pytest

.PHONY: help test test-unit test-integration test-live test-e2e

help:
	@echo "Test targets:"
	@echo "  make test-unit         Pure unit tests only (no DB, no creds)."
	@echo "  make test              OFFLINE suite: unit + integration, recorded"
	@echo "                         responses, NO credentials. Needs local Postgres."
	@echo "  make test-integration  EVERYTHING LIVE: unit + integration + E2E against"
	@echo "                         the real sandbox. Requires .env sandbox creds."
	@echo "  make test-live         Live per-integration isolation tests only."
	@echo "  make test-e2e          The full live E2E go-live gate only."

# Pure logic, no I/O — the fastest signal.
test-unit:
	$(PYTEST) tests/unit -q

# Default developer loop: offline, credential-free, fast.
# Integration tests use the local Postgres test DB (docker compose service `db`),
# but hit NO external API — external calls replay recorded fixtures.
test:
	$(PYTEST) tests/unit tests/integration -q

# Full live verification: runs the live-marked isolation tests AND the E2E gate
# against the real sandbox. Fails loudly if any of the 11 sandbox creds are
# missing from .env (the E2E credential hard-fail).
test-integration:
	RUN_LIVE=1 $(PYTEST) tests/unit tests/integration tests/e2e --run-live

# Just the live per-integration isolation round-trips (Sheets/Seam/DocuSign/Gmail).
test-live:
	RUN_LIVE=1 $(PYTEST) tests/integration --run-live -m live

# Just the live end-to-end go-live gate.
test-e2e:
	RUN_LIVE=1 $(PYTEST) tests/e2e --run-live
