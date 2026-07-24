#!/bin/sh
set -e
alembic upgrade head
# --proxy-headers: Caddy terminates TLS and proxies over plain HTTP, so without
# this the app believes it is serving http:// and builds wrong absolute URLs.
# --forwarded-allow-ips=*: the app is only reachable via Caddy (compose binds
# the host port to 127.0.0.1 and Caddy reaches it on the internal network), so
# the forwarded headers can be trusted.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips="*"
