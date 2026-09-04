#!/usr/bin/env bash
set -uo pipefail

# Weekly headless production-health audit (CI/CD Step 13, 2026-07-30).
#
# Deterministic monitoring already exists: healthchecks.io dead-man switches, an
# external uptime monitor, and a monthly systems-normal email. What none of them
# do is INTERPRET. This job runs the `checking-production-health` skill the same
# way a human session would, once a week, and emails the interpreted report.
#
# The pieces, and why each is where it is:
#
#   Claude Code CLI on the host   The skill lives in the repo clone, and skills
#                                 are read from the working directory. Running
#                                 in the container would need the repo there.
#   Report body -> docker exec    The host has no mail credentials and should
#                                 not get any. `app.send_operator_report` runs
#                                 inside the app container, where the alerts
#                                 Gmail account already lives. No new secret.
#   Non-zero exit on any failure  The crontab chains the healthchecks.io ping
#                                 behind this script with `&&`. Failing loudly
#                                 means the ping is withheld and the dead-man
#                                 switch fires. A dead auditor must be loud.
#
# Credentials come from ~/.secrets/rental-automation-claude-audit.env, sourced
# by the crontab line. That file is per-app on purpose: another app on this host
# gets its own token and its own healthchecks check, and revoking one leaves the
# other alone.

REPO="${AUDIT_REPO_DIR:-$HOME/workspace/home-rental-automation}"
CLAUDE_BIN="${AUDIT_CLAUDE_BIN:-$HOME/.local/bin/claude}"
MODEL="${AUDIT_MODEL:-sonnet}"
WINDOW_HOURS="${AUDIT_WINDOW_HOURS:-168}"
RUN_TIMEOUT="${AUDIT_TIMEOUT:-900}"

fail() { echo "health-audit: $*" >&2; exit 1; }

# --- preconditions ------------------------------------------------------------
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || fail "CLAUDE_CODE_OAUTH_TOKEN is not set (source the audit env file)"
[ -d "$REPO/.git" ]  || fail "no repo clone at $REPO"
[ -x "$CLAUDE_BIN" ] || fail "claude CLI not found at $CLAUDE_BIN"
command -v docker >/dev/null || fail "docker not available"

# The CLI resolves ANTHROPIC_API_KEY BEFORE the OAuth token. If one is ever
# exported into this job's environment, every run silently bills the pay-as-you-
# go API account instead of the subscription, with no visible difference in the
# output. Unset it here so that can never happen by accident.
unset ANTHROPIC_API_KEY

cd "$REPO" || fail "cannot enter $REPO"

# --- keep the clone current, but never fail the audit over it -----------------
# The skill IS the repo, so a stale clone means stale interpretation rules. A
# failed pull is not worth losing a week's audit over, so it is a warning and
# the report footer records exactly which commit ran.
if ! git pull --ff-only --quiet 2>/dev/null; then
  echo "health-audit: warning — could not fast-forward the clone; running on $(git rev-parse --short HEAD)" >&2
fi
HEAD_SHA="$(git rev-parse --short HEAD)"
TREE_STATE="clean"
[ -n "$(git status --porcelain)" ] && TREE_STATE="DIRTY (local edits are in play)"

# --- the prompt ---------------------------------------------------------------
# Deliberately bounded: one script run, one interpretation pass, a fixed output
# shape. This should be minutes, not an hour.
read -r -d '' PROMPT <<EOF
Run the production health audit for the last 7 days.

YOU ARE ALREADY ON THE PRODUCTION HOST, in the operations repo clone. Run the
report script LOCALLY. Do not SSH anywhere, and do not look for a .env file:
this clone has none by design, and the script does not need one. If a command
seems to need credentials you cannot find, run the script anyway and read what
it prints.

1. Invoke the checking-production-health skill in this repo
   (.claude/skills/checking-production-health/SKILL.md). Run its bundled report
   script with a ${WINDOW_HOURS}-hour window, and interpret the output using
   .claude/skills/checking-production-health/reference/log-signals.md.
2. You are READ-ONLY. Do not create, edit or delete any file. Do not restart,
   deploy, migrate, or change anything about production. Gather evidence and
   interpret it — nothing else.
3. Write the report to stdout and nothing else. No preamble, no sign-off.

Format, exactly:
- The FIRST line is exactly one of: healthy / needs attention / broken
  (lowercase, alone on the line). It becomes the email subject.
- Then one short paragraph summarising the state, which MUST name the log
  window actually covered — the report prints it, and a window shorter than
  requested makes an absent signal unknown rather than a finding.
- Then, only if something needs a human, a numbered list. For each item: what
  it is in plain language, the evidence line it rests on, and what to do.
- If everything is clean, say so in two sentences and stop.

Do not paste the raw report. Never include guest names, phone numbers, email
addresses or booking confirmation codes — name a booking by its id shape.
EOF

# --- run ----------------------------------------------------------------------
REPORT="$(mktemp)"
trap 'rm -f "$REPORT"' EXIT

timeout "$RUN_TIMEOUT" "$CLAUDE_BIN" -p "$PROMPT" \
  --model "$MODEL" \
  --allowedTools Bash Read Grep Glob \
  --disallowedTools Write Edit MultiEdit NotebookEdit WebFetch WebSearch \
  >"$REPORT" 2>/tmp/health-audit-stderr.$$
STATUS=$?
if [ "$STATUS" -eq 124 ]; then
  fail "the audit run exceeded ${RUN_TIMEOUT}s and was killed"
elif [ "$STATUS" -ne 0 ]; then
  fail "claude exited $STATUS: $(tail -3 /tmp/health-audit-stderr.$$ 2>/dev/null)"
fi
rm -f /tmp/health-audit-stderr.$$

grep -q '[^[:space:]]' "$REPORT" || fail "the audit produced an empty report"

# --- verdict ------------------------------------------------------------------
# The verdict becomes the subject line, which is what lets the email be triaged
# without opening it. The first version demanded a first line exactly equal to
# one of three words, which is more than a model reliably gives you: prefix
# match first, because it is precise, then fall back to a priority substring
# scan.
#
# ORDER IS DELIBERATE, and prefix-before-substring is what makes it safe. A
# healthy report saying "nothing needs attention" prefix-matches healthy and
# never reaches the substring scan; an off-spec line like "not healthy, needs
# attention" has no prefix match and is caught by the scan as needs attention.
# The dangerous direction is titling a broken week healthy, so the bad verdicts
# are always tested first.
FIRST_LINE="$(grep -m1 '[^[:space:]]' "$REPORT" \
              | tr -d '\r' | tr '[:upper:]' '[:lower:]' \
              | sed 's/[^a-z ]/ /g' | tr -s ' ' | sed 's/^ //; s/ $//')"

VERDICT="unclear verdict"
case "$FIRST_LINE" in
  broken*)           VERDICT="broken" ;;
  "needs attention"*) VERDICT="needs attention" ;;
  healthy*)          VERDICT="healthy" ;;
  *broken*)          VERDICT="broken" ;;
  *"needs attention"*) VERDICT="needs attention" ;;
  *healthy*)         VERDICT="healthy" ;;
esac

{
  echo
  echo "---"
  echo "Window: last ${WINDOW_HOURS}h. Audited $(date -u '+%Y-%m-%d %H:%M UTC') on $(hostname)."
  echo "Repo: ${HEAD_SHA} (${TREE_STATE}). Model: ${MODEL}."
  echo "This email arrives weekly. Its absence is itself the alarm — the"
  echo "healthchecks.io check 'rental weekly health audit' fires after 8 days"
  echo "of silence."
} >>"$REPORT"

# --- keep a copy on disk ------------------------------------------------------
# So the last run can be read without re-running it, which costs a Claude call
# and several minutes. The first real run reported an unusable verdict and had
# to be diagnosed by re-running blind, because the report had been deleted.
# One file, overwritten each week.
mkdir -p "$HOME/logs"
cp "$REPORT" "$HOME/logs/last-health-audit.md" 2>/dev/null || true

# --- deliver ------------------------------------------------------------------
APP_CID="$(docker ps --format '{{.ID}}\t{{.Image}}' \
           | awk -F'\t' '$2 ~ /home-rental-automation/ {print $1; exit}')"
[ -n "$APP_CID" ] || fail "no running app container to send the report from"

docker exec -i "$APP_CID" python -m app.send_operator_report \
  --subject "Weekly production health: ${VERDICT}" <"$REPORT" \
  || fail "the report was generated but could not be emailed"

echo "health-audit: ok — verdict '${VERDICT}', repo ${HEAD_SHA}"
