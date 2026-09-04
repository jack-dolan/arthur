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

The script always runs **on the production host**. How you invoke it depends on
where *you* are, so establish that first:

```bash
docker ps --format '{{.Image}}' | grep -q home-rental-automation && echo "ON THE HOST" || echo "not the host"
```

**On the host** (the weekly audit runs here, and so does any SSH session there)
— run it directly from the ops clone:

```bash
cd ~/workspace/home-rental-automation
bash .claude/skills/checking-production-health/scripts/health_report.sh 24
```

Do **not** go looking for a `.env` here. The ops clone has none and does not
need one: the script reads what it needs from the running container's own
environment. An absent `.env` is the expected state on this host, not a blocker.

**From the dev machine** — reach the host over SSH:

```bash
VPS=$(grep '^DEPLOY_VPS_SSH=' .env | cut -d= -f2-)
DIR=$(grep '^DEPLOY_VPS_REPO_DIR=' .env | cut -d= -f2-)
ssh "$VPS" "cd $DIR && bash .claude/skills/checking-production-health/scripts/health_report.sh 24"
```

The argument is the look-back window in hours (default 24; use 168 for a
weekly review). The script is read-only: `docker ps`/`logs`/`exec`, SELECT-only
queries, and local HTTP probes. Nothing it does can change production, and
`tests/unit/test_health_report_script.py` asserts that mechanically.

If the script is missing or errors on the host, that clone is behind — `git
pull` it. On the platform topology that clone exists **only** for operational
work like this; the running app has no checkout and never reads it.

**The weekly unattended audit runs this exact script** (see "The weekly audit"
below), so a change here changes what lands in Jack's inbox on Monday.

## Interpret

Work through the report top to bottom:

1. **Containers + health endpoints** — anything not `Up`, or a non-200 probe,
   outranks everything else. On the platform topology there are two probes and
   the pair is the diagnosis: `origin via local proxy: 200` with a failing
   public edge means the CDN or tunnel is the outage, not the app. The edge
   probe distinguishes `UNHEALTHY` (something answered, badly) from
   `UNREACHABLE` (nothing answered) — never report the second as an app outage.
2. **The actual log window** — read it before you conclude anything from an
   absence. The requested window is a ceiling, not a promise: every deploy
   replaces the app container, and on the platform topology the host keeps only
   the last few, so a 168h request shortly after a deploy can return two hours
   of logs. In that case "no daily job ran" is **unknown**, not a finding. Say
   which it is.
3. **Boot evidence** — the schema-guard line, the DocuSign PRODUCTION banner,
   and the `SCHEDULED JOBS` manifest. The manifest is the only proof a job is
   registered and when it next fires; APScheduler's own logger is not
   configured, so nothing else reports it. Eleven jobs is the current full set.
4. **Hard-failure signals** (tracebacks, dispatch failures, MissingGreenlet,
   keep-alive failure, probable missed bookings) — each nonzero count needs an
   explanation, not a shrug. Pull the matching lines with `grep` over the logs.
5. **Workflow + scheduled-job heartbeats** — bookings are sporadic, but in a
   window that genuinely covers ≥24h the daily jobs must each appear once.
   **A missing daily-job line in a covered window is itself a finding** (the
   scheduler silently skips misfires).
6. **DB state** — stuck IN_PROGRESS rows and capped-out FAILED tasks are
   exactly what the `triaging-stuck-tasks` skill exists for; hand off there
   rather than improvising resets.
7. **Dead-letter activity** — `other` rows are normal inbox noise; any
   `parse_error`, `cancellation_parse_error`, `classify_error`, or `duplicate`
   row means an owner alert should also have gone out — verify the
   corresponding "Sent … alert" line exists.
8. **Job journal** — the weekly inbox reviewer's proof of life, and the one
   record that survives a deploy. No entry in eight days means it has not run.

## Report back

Lead with a one-line verdict, then only the findings that need action, each
with its evidence line. Don't paste the whole report. If everything is clean,
say so briefly and note the window actually checked.

The three verdicts are fixed words, because the weekly audit email puts them in
its subject line and they must mean the same thing every week:

- **healthy** — nothing needs a human.
- **needs attention** — something is wrong or unexplained, but bookings are
  still being ingested and automations still run.
- **broken** — guest-affecting right now: the app is down, the poller cannot
  read the inbox, or an automation the guest depends on is failing.

## The weekly audit

A cron job on the production host runs this skill unattended every Monday and
emails the report to the owner. It is deliberately the same script and the same
interpretation rules as a hand-run check — if you improve either, the Monday
email improves too. Details, including how to run its exact command by hand,
are in `docs/operations.md` → "The weekly health audit".

## When a symptom is user-reported ("the dashboard looks broken")

Reproduce it **as the user sees it** before diagnosing: `curl` the live URL
and read the bytes the browser receives. Never conclude from the source tree
or from green tests — "the CSS exists in the repo" and "the CSS loads in the
browser" have already proven to be different claims in this project (the
stylesheet was mixed-content-blocked in production for weeks while every
test passed).
