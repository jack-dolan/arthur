#!/usr/bin/env bash
# Read-only production health report for the rental-automation stack.
# Run from the repo root ON the VPS:  bash .../health_report.sh [HOURS]
# Gathers container status, health probes, app-log signal counts, and
# SELECT-only DB queries. Makes NO writes and NO external API calls.
set -uo pipefail

# 24h covers one full cycle of every daily job; pass 168 for a weekly view.
HOURS="${1:-24}"

if [ ! -f docker-compose.yml ] || [ ! -f .env ]; then
  echo "ERROR: run from the repo root on the VPS (needs docker-compose.yml + .env)" >&2
  exit 1
fi
set -a; . ./.env; set +a

LOGS="$(mktemp)"
trap 'rm -f "$LOGS"' EXIT
docker compose logs app --since "${HOURS}h" >"$LOGS" 2>&1

count() { grep -cE "$1" "$LOGS" 2>/dev/null || true; }

sql() {
  docker compose exec -T db psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    -Atc "$1" 2>/dev/null || echo "query-failed"
}

echo "=== Health report: last ${HOURS}h ($(date -u '+%Y-%m-%d %H:%MZ')) ==="

echo
echo "--- Containers ---"
docker ps --format '{{.Names}}  {{.Status}}'

echo
echo "--- Health endpoints ---"
echo "app direct (:8000): $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/health)"
if [ -n "${DOMAIN:-}" ] && [ "${DOMAIN}" != "localhost" ]; then
  echo "public via TLS:     $(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://${DOMAIN}/health")"
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

echo
echo "--- Workflow signals ---"
echo "poller batches processed:   $(count 'Processing [0-9]+ new message')"
echo "bookings persisted:         $(count 'Persisted (airbnb|vrbo) booking')"
echo "webhook events received:    $(count 'DocuSign webhook received')"
echo "HOA emails sent:            $(count 'Sent HOA email')"
echo "HOA sent LATE:              $(count 'sending anyway')"
echo "keep-alive succeeded:       $(count 'keep-alive succeeded')"
echo "requeue job heartbeat:      $(count 'requeue_stalled_automations: run started')"
echo "code-verify job heartbeat:  $(count 'verify_access_codes: run started')"
echo "daily reminders heartbeat:  $(count 'check_daily_reminders: run started')"

echo
echo "--- Owner alerts sent ---"
grep -oE 'Sent [a-zA-Z-]+( [a-zA-Z-]+)* alert' "$LOGS" | sort | uniq -c || true
echo "(also) new-booking alerts:  $(count 'Sent new-booking alert')"

echo
echo "--- DB: task-state warnings ---"
echo "IN_PROGRESS >24h (triage before ANY reset — see triaging-stuck-tasks):"
sql "SELECT task_type, booking_id, updated_at FROM booking_tasks
     WHERE state='in_progress' AND updated_at < now() - interval '24 hours';"
echo "FAILED automations on active future bookings (attempts):"
sql "SELECT bt.task_type, bt.attempt_count, b.check_in_date FROM booking_tasks bt
     JOIN bookings b ON b.id = bt.booking_id
     WHERE bt.state='failed' AND b.status='active' AND b.check_in_date >= CURRENT_DATE;"

echo
echo "--- DB: dead-letter activity (last ${HOURS}h, by disposition) ---"
sql "SELECT disposition, count(*) FROM processed_messages
     WHERE created_at > now() - interval '${HOURS} hours' GROUP BY disposition;"

echo
echo "--- DB: workload snapshot ---"
echo "active bookings:            $(sql "SELECT count(*) FROM bookings WHERE status='active';")"
echo "next 3 check-ins:"
sql "SELECT check_in_date, platform FROM bookings WHERE status='active'
     AND check_in_date >= CURRENT_DATE ORDER BY check_in_date LIMIT 3;"

echo
echo "--- Recent ERROR/WARNING tail (up to 15 lines) ---"
grep -E ' (ERROR|WARNING) ' "$LOGS" | tail -15 || true

echo
echo "=== End of report. Interpret with reference/log-signals.md ==="
