#!/bin/sh
set -e

# Migrations are an explicit deployment step, never an application-boot side
# effect. `docker compose run --rm --no-deps app migrate` is idempotent and may
# be run before every deploy.
if [ "${1:-}" = "migrate" ]; then
  shift
  exec alembic upgrade head "$@"
fi

# Preserve normal container command semantics for smoke tests and operator
# commands without running either the app or its schema guard.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# Read-only startup guard:
# - exact head: start
# - revision unknown to this build (DB ahead after rollback): warn and start
# - known older revision: refuse, because the explicit migration step was skipped
python -m app.schema_guard

# --proxy-headers: Caddy terminates TLS and proxies over plain HTTP, so without
# this the app believes it is serving http:// and builds wrong absolute URLs.
# --forwarded-allow-ips=*: the app is only reachable via Caddy (compose binds
# the host port to 127.0.0.1 and Caddy reaches it on the internal network), so
# the forwarded headers can be trusted.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips="*"
