#!/usr/bin/env bash
set -uo pipefail

# Probe production's public health endpoint and say plainly what happened.
#
#   usage: bash scripts/prod_health.sh [hostname]
#
# WHY THIS EXISTS. The deploy skill used to build the URL from the LOCAL `.env`
# (`DOMAIN=$(grep '^DOMAIN=' .env | ...)`). On the dev box that value is
# `localhost`, because that file configures local development -- so the probe
# hit `https://localhost/health`, failed to connect, and printed a bare `000`.
# During a deploy that reads as "production is down" at exactly the moment you
# are least inclined to doubt the tooling. It cost time on 2026-07-30.
#
# Two rules follow, and both are covered by tests/unit/test_prod_health_probe.py:
#
#   1. The hostname is resolved from something that describes PRODUCTION -- the
#      running app container's own environment -- never from a local dev file.
#      A development value (`localhost`) is refused, not probed.
#   2. "Nothing answered" and "the app answered badly" get different words and
#      different exit codes. An unreachable host is not an outage verdict.
#
# Exit codes:  0 healthy | 1 reachable but not 200 | 2 unreachable or unresolved

HOST="${1:-${PROD_DOMAIN:-}}"

# --- resolve the hostname from production itself -----------------------------
# The app container holds the platform's own DOMAIN value, which IS the
# production hostname. Ask whichever docker is in reach: this script runs both
# on the dev box (docker is remote, over DEPLOY_VPS_SSH) and on the VPS itself
# (docker is local), and the weekly audit uses the second.
# One snippet, run either locally or over SSH. It must survive being re-parsed
# by a remote shell, so it is a single quoted string rather than an argv array
# (a `--format '{{.ID}} {{.Image}}'` argv splits on the space over ssh and the
# remote docker rejects it — found the first time this ran for real).
READ_DOMAIN='cid=$(docker ps --format "{{.ID}} {{.Image}}" | grep -m1 home-rental-automation | cut -d" " -f1); [ -n "$cid" ] && docker exec "$cid" printenv DOMAIN'

if [ -z "$HOST" ] && [ -z "${PROD_HEALTH_NO_RESOLVE:-}" ]; then
  if command -v docker >/dev/null 2>&1; then
    HOST="$(bash -c "$READ_DOMAIN" 2>/dev/null | tr -d '\r\n' || true)"
  fi
  if [ -z "$HOST" ]; then
    VPS="${DEPLOY_VPS_SSH:-}"
    if [ -z "$VPS" ] && [ -r .env ]; then
      VPS="$(grep '^DEPLOY_VPS_SSH=' .env | cut -d= -f2-)"
    fi
    if [ -n "$VPS" ]; then
      HOST="$(ssh -o BatchMode=yes "$VPS" "$READ_DOMAIN" 2>/dev/null | tr -d '\r\n' || true)"
    fi
  fi
fi

if [ -z "$HOST" ]; then
  cat >&2 <<'EOF'
UNRESOLVED: could not work out production's hostname.

Tried, in order: the argument, $PROD_DOMAIN, the local docker daemon, and the
host named by DEPLOY_VPS_SSH. None of them produced a running app container to
read DOMAIN from.

Do NOT fall back to the local .env — its DOMAIN describes this machine, not
production. Pass the hostname explicitly instead:

    bash scripts/prod_health.sh <production-hostname>
EOF
  exit 2
fi

if [ "$HOST" = "localhost" ] || [ "$HOST" = "127.0.0.1" ]; then
  echo "REFUSED: resolved the hostname '$HOST', which is a development value." >&2
  echo "That is the bug this script exists to prevent. Pass the real production hostname." >&2
  exit 2
fi

# --- probe --------------------------------------------------------------------
URL="https://${HOST}/health"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$URL")"
CURL_STATUS=$?

if [ "$CURL_STATUS" -ne 0 ]; then
  case "$CURL_STATUS" in
    6)  WHY="DNS: the hostname did not resolve" ;;
    7)  WHY="the connection was refused or the route is dead" ;;
    28) WHY="the request timed out" ;;
    35|60) WHY="TLS failed (handshake or certificate)" ;;
    *)  WHY="see 'man curl' EXIT CODES" ;;
  esac
  echo "UNREACHABLE: $URL — nothing answered (curl exit $CURL_STATUS: $WHY)."
  echo "This is NOT a verdict on the app: no HTTP response was received at all."
  echo "Check DNS, the tunnel/proxy and this machine's own connectivity before"
  echo "concluding anything about production."
  exit 2
fi

if [ "$CODE" = "200" ]; then
  echo "HEALTHY: $URL -> 200"
  exit 0
fi

echo "UNHEALTHY: $URL -> $CODE"
echo "The host is reachable and something answered, but not with 200. This one"
echo "IS about the running app (or the proxy in front of it)."
exit 1
