"""Durable record of when a scheduled job last actually ran.

Why this exists: the weekly inbox reviewer is silent on a clean week, which is
correct, but it means silence has two meanings — nothing found, or the job never
ran. The monthly status email reports the run count so a zero is visible. That
only works if the count survives a container restart, so it is a file on the
same persistent volume the DocuSign token store uses, not an in-memory counter.

The journal must never be able to break a job: every failure path degrades to
"no record" rather than raising into the caller.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.job_journal import (
    JOB_INBOX_REVIEWER,
    read_runs_since,
    record_run,
)


@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / "job_runs.jsonl"
    monkeypatch.setattr("app.job_journal._JOURNAL", path)
    return path


def test_a_run_is_recorded_and_read_back(journal):
    record_run(JOB_INBOX_REVIEWER, reviewed=4, outcome="ok")

    runs = read_runs_since(JOB_INBOX_REVIEWER, datetime.now(UTC) - timedelta(days=1))
    assert len(runs) == 1
    assert runs[0]["reviewed"] == 4
    assert runs[0]["outcome"] == "ok"


def test_runs_outside_the_window_are_excluded(journal):
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    journal.write_text(
        json.dumps({"job": JOB_INBOX_REVIEWER, "at": old, "reviewed": 9, "outcome": "ok"})
        + "\n"
    )
    record_run(JOB_INBOX_REVIEWER, reviewed=2, outcome="ok")

    runs = read_runs_since(JOB_INBOX_REVIEWER, datetime.now(UTC) - timedelta(days=30))
    assert [r["reviewed"] for r in runs] == [2]


def test_other_jobs_are_not_counted(journal):
    record_run("some_other_job", reviewed=7, outcome="ok")

    runs = read_runs_since(JOB_INBOX_REVIEWER, datetime.now(UTC) - timedelta(days=30))
    assert runs == []


def test_an_idle_run_still_counts_as_a_run(journal):
    """A quiet week must be distinguishable from a dead job. That is the whole
    point of the journal, so a run that reviewed nothing is still a run."""
    record_run(JOB_INBOX_REVIEWER, reviewed=0, outcome="idle")

    runs = read_runs_since(JOB_INBOX_REVIEWER, datetime.now(UTC) - timedelta(days=30))
    assert len(runs) == 1
    assert runs[0]["outcome"] == "idle"


def test_a_corrupt_line_does_not_lose_the_rest(journal):
    journal.write_text("this is not json\n")
    record_run(JOB_INBOX_REVIEWER, reviewed=1, outcome="ok")

    runs = read_runs_since(JOB_INBOX_REVIEWER, datetime.now(UTC) - timedelta(days=30))
    assert len(runs) == 1


def test_recording_never_raises_when_the_volume_is_absent(tmp_path, monkeypatch):
    """Dev hosts and tests have no /app/data. A missing volume must cost the
    record, never the job that was trying to write it."""
    monkeypatch.setattr(
        "app.job_journal._JOURNAL", tmp_path / "nope" / "deeper" / "job_runs.jsonl"
    )
    record_run(JOB_INBOX_REVIEWER, reviewed=1, outcome="ok")  # must not raise
    assert read_runs_since(JOB_INBOX_REVIEWER, datetime.now(UTC)) == []


def test_the_journal_is_pruned_so_it_cannot_grow_without_bound(journal):
    for _ in range(400):
        record_run(JOB_INBOX_REVIEWER, reviewed=1, outcome="ok")

    assert len(journal.read_text().splitlines()) <= 200
