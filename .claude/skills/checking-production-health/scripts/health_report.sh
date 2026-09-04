#!/usr/bin/env bash
# Read-only production health report for the rental-automation stack.
#
#   bash .claude/skills/checking-production-health/scripts/health_report.sh [HOURS]
#
# Run it ON the production host, from the repo clone. The argument is the
# look-back window in hours (default 24; 168 for a weekly review).
#
# Read-only means read-only: `docker ps`/`logs`/`exec`, SELECT-only SQL, and
# local HTTP probes. Nothing here writes, restarts, or deletes anything, and
# tests/unit/test_health_report_script.py asserts that mechanically because this
# runs unattended against production every week.
#
# TWO TOPOLOGIES, because the project supports both (see `deploying-to-vps`):
#   platform — the app is a prebuilt image run by the platform. No compose file,
#              no published app port, and log history is spread across several
#              task containers because every deploy replaces the container.
#   compose  — the app, its database and a front proxy run from a checkout here.
set -uo pipefail

HOURS="${1:-24}"

echo "=== Health report: last ${HOURS}h ($(date -u '+%Y-%m-%d %H:%MZ')) ==="

# --- topology -----------------------------------------------------------------
TOPOLOGY=platform
if [ -f docker-compose.yml ] && [ -n "$(docker compose ps -aq app 2>/dev/null)" ]; then
  TOPOLOGY=compose
fi
echo "topology: ${TOPOLOGY}"

PS_FMT='{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}'

app_containers() { # newest last; every task container, running or exited
  if [ "$TOPOLOGY" = "compose" ]; then
    docker compose ps -aq app 2>/dev/null
  else
    docker ps -a --format "$PS_FMT" 2>/dev/null \
      | awk -F'\t' '$3 ~ /home-rental-automation/ {print $1}'
  fi
}

running_app_container() {
  if [ "$TOPOLOGY" = "compose" ]; then
    docker compose ps -q app 2>/dev/null | head -1
  else
    docker ps --format "$PS_FMT" 2>/dev/null \
      | awk -F'\t' '$3 ~ /home-rental-automation/ {print $1; exit}'
  fi
}

APP_CID="$(running_app_container)"
if [ -z "$APP_CID" ]; then
  echo "!! NO RUNNING APP CONTAINER FOUND — that outranks everything else below."
fi

# --- log collection -----------------------------------------------------------
# One deploy rotates the container, so a weekly window normally spans several.
# Collect every task container's slice of the window and sort by timestamp; the
# RFC3339 prefixes sort correctly as plain text.
LOGS="$(mktemp)"
trap 'rm -f "$LOGS"' EXIT
for cid in $(app_containers); do
  docker logs --timestamps --since "${HOURS}h" "$cid" 2>&1
done | sort >"$LOGS"

count() { grep -cE "$1" "$LOGS" 2>/dev/null || true; }

echo
echo "--- Containers ---"
docker ps --format "$PS_FMT" 2>/dev/null

echo
echo "--- Actual log window (NOT the requested one) ---"
# The requested window is a ceiling, not a promise. On the platform topology the
# app's logs only reach back to the oldest surviving task container, so after a
# recent deploy a 168h request may return two hours of logs. "The daily job never
# logged" is only a finding if the window truly covers a day.
FIRST_LOG="$(head -1 "$LOGS" | cut -d' ' -f1)"
LAST_LOG="$(tail -1 "$LOGS" | cut -d' ' -f1)"
if [ -z "$FIRST_LOG" ]; then
  echo "log window: EMPTY — no app log lines in the last ${HOURS}h."
  echo "  Treat every 'absent signal' below as UNKNOWN, not as a finding."
else
  echo "log window: ${FIRST_LOG} .. ${LAST_LOG}  ($(wc -l <"$LOGS") lines)"
  echo "  Anything expected to appear BEFORE the first timestamp is out of view,"
  echo "  not missing. A deploy inside the window rotates the container."
fi

# --- health endpoints ---------------------------------------------------------
echo
echo "--- Health endpoints ---"
DOMAIN=""
[ -n "$APP_CID" ] && DOMAIN="$(docker exec "$APP_CID" printenv DOMAIN 2>/dev/null | tr -d '\r\n')"

if [ "$TOPOLOGY" = "compose" ]; then
  echo "app direct (:8000): $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/health)"
else
  # The app publishes no host port here; the proxy is the only way in. Asking
  # the proxy locally separates "the app is broken" from "the path to it is".
  ORIGIN="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
            -H "Host: ${DOMAIN:-localhost}" http://127.0.0.1/health)"
  echo "origin via local proxy: ${ORIGIN}   (200 here + a bad edge below = the CDN/tunnel, not the app)"
fi

PROBE="$(dirname "$0")/../../../../scripts/prod_health.sh"
if [ -n "$DOMAIN" ] && [ -f "$PROBE" ]; then
  echo "public edge:"
  PROD_DOMAIN="$DOMAIN" bash "$PROBE" | sed 's/^/  /'
elif [ -n "$DOMAIN" ]; then
  echo "public edge:        $(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://${DOMAIN}/health")"
fi

# --- boot evidence ------------------------------------------------------------
# Deliberately read WITHOUT --since: these lines are written once at boot, so a
# container started before the window still has to be inspectable.
echo
echo "--- Boot evidence (running container) ---"
if [ -n "$APP_CID" ]; then
  # Captured once rather than grepped three times: `grep -m1` closes the pipe
  # early, `docker logs` dies of SIGPIPE, and under `pipefail` that made a
  # SUCCESSFUL match report as a failure. It printed "no DocuSign target banner"
  # directly underneath the banner it had just found.
  BOOTLOG="$(docker logs "$APP_CID" 2>&1)"
  printf '%s\n' "$BOOTLOG" | grep -m1 'SCHEMA GUARD' \
    || echo "no SCHEMA GUARD line — the app did not run its startup guard"
  printf '%s\n' "$BOOTLOG" | grep -m1 'DocuSign target' \
    || echo "no DocuSign target banner — the sandbox/production check is missing"
  echo "registered scheduler jobs:"
  JOBS="$(printf '%s\n' "$BOOTLOG" | grep 'SCHEDULED JOBS')"
  if [ -n "$JOBS" ]; then
    printf '%s\n' "$JOBS" | sed 's/^/  /'
  else
    echo "  NONE — no job manifest at boot means no scheduler, which is silent failure"
  fi
fi

echo
echo "--- Hard-failure signals (each nonzero needs an explanation) ---"
echo "tracebacks:                 $(count 'Traceback \(most recent call last\)')"
echo "ERROR lines:                $(count ' ERROR ')"
echo "dispatch task failures:     $(count 'dispatch: task .* failed')"
echo "dispatch record-FAILED gap: $(count 'could not record FAILED')"
echo "MissingGreenlet:            $(count 'MissingGreenlet')"
echo "poller auth failures:       $(count 'Gmail auth failed')"
echo "keep-alive failures:        $(count 'keep-alive failed')"
echo "webhook parse failures:     $(count 'could not be extracted')"
echo "cancellation cleanups fail: $(count 'Cancellation: .* failed')"
echo "probable missed bookings:   $(count 'PROBABLE MISSED BOOKING')"
echo "heartbeat ping failures:    $(count 'Heartbeat ping failed')"

echo
echo "--- Workflow signals ---"
echo "poller batches processed:   $(count 'Processing [0-9]+ new message')"
echo "bookings persisted:         $(count 'Persisted (airbnb|vrbo) booking')"
echo "webhook events received:    $(count 'DocuSign webhook received')"
echo "HOA emails sent:            $(count 'Sent HOA email')"
echo "HOA sent LATE:              $(count 'sending anyway')"
echo "keep-alive succeeded:       $(count 'keep-alive succeeded')"

echo
echo "--- Scheduled-job heartbeats (absence in a covered window IS a finding) ---"
for job in complete_past_bookings verify_credentials check_daily_reminders \
           requeue_stalled_automations verify_access_codes \
           check_classifier_drift review_dead_letters send_monthly_status_email; do
  printf '%-32s %s\n' "$job:" "$(count "${job}: run started")"
done

echo
echo "--- Owner alerts sent ---"
grep -oE 'Sent [a-zA-Z-]+( [a-zA-Z-]+)* alert' "$LOGS" | sort | uniq -c || true
echo "(also) new-booking alerts:  $(count 'Sent new-booking alert')"

# --- database -----------------------------------------------------------------
# Credentials come from the app's OWN environment, so this file names no
# infrastructure identifier and keeps working if the platform renames a service.
PG_CID=""
PG_USER=""
PG_DB=""
if [ -n "$APP_CID" ]; then
  DB_URL="$(docker exec "$APP_CID" printenv DATABASE_URL 2>/dev/null | tr -d '\r\n')"
  PG_USER="$(printf '%s' "$DB_URL" | sed -E 's#^[^:]+://([^:]+):.*#\1#')"
  PG_HOST="$(printf '%s' "$DB_URL" | sed -E 's#^[^@]+@([^:/]+).*#\1#')"
  PG_DB="$(printf '%s' "$DB_URL" | sed -E 's#.*/([^/?]+)$#\1#')"
  PG_CID="$(docker ps --format "$PS_FMT" 2>/dev/null \
            | awk -F'\t' -v h="$PG_HOST" '$2 ~ "^"h {print $1; exit}')"
fi

sql() {
  if [ -z "$PG_CID" ]; then echo "no-database-container"; return; fi
  docker exec -i "$PG_CID" psql -U "$PG_USER" -d "$PG_DB" -Atc "$1" 2>/dev/null \
    || echo "query-failed"
}

echo
echo "--- DB: task-state warnings ---"
echo "IN_PROGRESS >24h (triage before ANY reset — see triaging-stuck-tasks):"
sql "SELECT task_type, booking_id, updated_at FROM booking_tasks WHERE state='in_progress' AND updated_at < now() - interval '24 hours';"
echo "FAILED automations on active future bookings (attempts):"
sql "SELECT bt.task_type, bt.attempt_count, b.check_in_date FROM booking_tasks bt JOIN bookings b ON b.id = bt.booking_id WHERE bt.state='failed' AND b.status='active' AND b.check_in_date >= CURRENT_DATE;"

echo
echo "--- DB: dead-letter activity (last ${HOURS}h, by disposition) ---"
sql "SELECT disposition, count(*) FROM processed_messages WHERE created_at > now() - interval '${HOURS} hours' GROUP BY disposition;"

echo
echo "--- DB: workload snapshot ---"
echo "active bookings:            $(sql "SELECT count(*) FROM bookings WHERE status='active';")"
echo "next 3 check-ins:"
sql "SELECT check_in_date, platform FROM bookings WHERE status='active' AND check_in_date >= CURRENT_DATE ORDER BY check_in_date LIMIT 3;"

echo
echo "--- Job journal (survives deploys; the weekly reviewer's proof of life) ---"
if [ -n "$APP_CID" ]; then
  docker exec "$APP_CID" sh -c 'tail -n 5 /app/data/job_runs.jsonl 2>/dev/null' \
    || echo "journal unreadable"
fi

echo
echo "--- Recent ERROR/WARNING tail (up to 15 lines) ---"
grep -E ' (ERROR|WARNING) ' "$LOGS" | tail -15 || true

echo
echo "=== End of report. Interpret with reference/log-signals.md ==="
