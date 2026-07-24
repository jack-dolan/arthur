---
name: checking-production-health
description: Gathers and interprets the rental-automation production stack's health over the past N hours — app logs bucketed into known signals, scheduler-job evidence, task-state and dead-letter queries, health endpoints. Use when the user asks whether production is healthy, to check the logs, to investigate an alert email, or for a status/health report.
---

# Checking production health

One bundled script gathers everything read-only; your job is interpretation.
Signal meanings live in [reference/log-signals.md](reference/log-signals.md) —
read it before judging output, because this system's logs contain a lot of
**healthy noise** that looks alarming (and a few quiet lines that are serious).

## Run the report

From the dev machine (target values come from `.env`, never hardcoded):

```bash
VPS=$(grep '^DEPLOY_VPS_SSH=' .env | cut -d= -f2-)
DIR=$(grep '^DEPLOY_VPS_REPO_DIR=' .env | cut -d= -f2-)
ssh "$VPS" "cd $DIR && bash .claude/skills/checking-production-health/scripts/health_report.sh 24"
```

The argument is the look-back window in hours (default 24; use 168 for a
weekly review). The script is read-only: logs, `docker ps`, `SELECT`-only DB
queries, and local health probes. If the script is missing on the VPS, the
repo there is behind — deploy first (see the `deploying-to-vps` skill).

## Interpret

Work through the report top to bottom:

1. **Containers + health endpoints** — anything not `Up`, or a non-200
   health probe, outranks everything else in the report.
2. **Hard-failure signals** (tracebacks, dispatch failures, MissingGreenlet,
   keep-alive failure) — each nonzero count needs an explanation, not a
   shrug. Pull the matching lines with the grep shown in the report.
3. **Workflow signals** (poller ingests, HOA sends, alerts sent) — compare
   against expectations: bookings are sporadic, but the poller should show
   *some* cycle evidence and the daily jobs must appear once per day in a
   ≥24h window. **A missing daily-job line is itself a finding** (the
   scheduler silently skips misfires).
4. **DB state** — stuck IN_PROGRESS rows and capped-out FAILED tasks are
   exactly what the `triaging-stuck-tasks` skill exists for; hand off there
   rather than improvising resets.
5. **Dead-letter activity** — `other` rows are normal inbox noise; any
   `parse_error`, `cancellation_parse_error`, `classify_error`, or
   `duplicate` row means an owner alert should also have gone out — verify
   the corresponding "Sent … alert" line exists.

## Report back

Lead with a one-line verdict (healthy / degraded / broken), then only the
findings that need action, each with its evidence line. Don't paste the whole
report. If everything is clean, say so briefly and note the window checked.

## When a symptom is user-reported ("the dashboard looks broken")

Reproduce it **as the user sees it** before diagnosing: `curl` the live URL
and read the bytes the browser receives. Never conclude from the source tree
or from green tests — "the CSS exists in the repo" and "the CSS loads in the
browser" have already proven to be different claims in this project (the
stylesheet was mixed-content-blocked in production for weeks while every
test passed).
