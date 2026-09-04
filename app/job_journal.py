"""A durable record of when a scheduled job last actually ran.

**The problem this solves.** The weekly inbox reviewer emails only when it finds
something. That is deliberate — a weekly "all clear" trains the reader to ignore
both it and the digest beside it — but it means silence carries two meanings: a
clean week, or a job that never ran. From an inbox those look identical.

Most of that ambiguity is already covered from outside the app: the poller pings
an external monitor every five minutes, so a dead scheduler is caught within the
hour, and the daily credential sentinel catches a dead API key by morning. What
neither covers is the narrow case of one job failing while everything around it
stays healthy. This journal closes that: every run appends a line, and the
monthly status email reports the count, where a zero is the tell.

An in-memory counter would not do, because a deploy would reset it and this
system deploys often. The journal is therefore a file on `/app/data`, the same
persistent volume that holds the DocuSign token store and the signed PDFs, so it
survives container recreation.

**Writing to the journal must never be able to break the job it is recording.**
Every failure here degrades to "no record", never to an exception in the caller.
That direction is deliberate: a lost line under-reports a healthy job, which is
a false alarm the reader resolves in a minute. The opposite trade, a journal
failure taking down the reviewer, would be a real outage caused by the thing
meant to detect outages.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_JOURNAL = Path("/app/data/job_runs.jsonl")

# One line per run. Weekly and monthly jobs generate a handful a month, so this
# holds years of history and still bounds the file at a few tens of kilobytes.
_MAX_LINES = 200

JOB_INBOX_REVIEWER = "review_dead_letters"


def record_run(job: str, *, reviewed: int = 0, outcome: str = "ok") -> None:
    """Append one run record. Never raises.

    *outcome* is free text describing how the run ended (``ok``, ``idle``,
    ``error``, ``skipped_no_key``). *reviewed* is how many items the run
    actually processed, which distinguishes a job that ran and found nothing
    from one that ran and did work.
    """
    entry = {
        "job": job,
        "at": datetime.now(UTC).isoformat(),
        "reviewed": reviewed,
        "outcome": outcome,
    }
    try:
        if not _JOURNAL.parent.exists():
            # Dev host or test runner without the mounted volume. The record is
            # worth nothing there, and the caller is worth everything.
            return
        lines = []
        if _JOURNAL.exists():
            lines = _JOURNAL.read_text().splitlines()
        lines.append(json.dumps(entry))
        _JOURNAL.write_text("\n".join(lines[-_MAX_LINES:]) + "\n")
    except Exception:  # noqa: BLE001 — bookkeeping must not break the job
        log.exception("job_journal: could not record a run of %s", job)


def read_runs_since(job: str, since: datetime) -> list[dict]:
    """Return *job*'s recorded runs at or after *since*, oldest first.

    A corrupt or half-written line is skipped rather than allowed to hide the
    rest of the history — an unreadable journal must not look like an idle job.
    """
    runs: list[dict] = []
    try:
        if not _JOURNAL.exists():
            return []
        for line in _JOURNAL.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("job") != job:
                    continue
                when = datetime.fromisoformat(entry["at"])
            except (ValueError, TypeError, KeyError):
                continue
            if when >= since:
                runs.append(entry)
    except Exception:  # noqa: BLE001 — the report must survive a bad journal
        log.exception("job_journal: could not read runs of %s", job)
    return runs
