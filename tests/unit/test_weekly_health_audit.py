"""Tests for scripts/weekly_health_audit.sh, driven through a stubbed CLI.

The first end-to-end run on 2026-07-30 delivered a real report but titled the
email "unclear verdict", because the verdict parser demanded a first line equal
to one of three words and the model had written the verdict followed by more
text. The subject line is the whole point of the verdict — it is what lets the
email be triaged without opening it — so the parse is pinned here against every
shape a model plausibly emits.

Priority matters as much as matching: "not healthy" must never be read as
healthy, so the bad verdicts are tested before the good one.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]
SCRIPT = REPO / "scripts" / "weekly_health_audit.sh"

DOCKER_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_TRACE"
case "$1" in
  ps) printf 'c_app\\tghcr.io/owner/home-rental-automation:sha-abc\\n' ;;
  exec) cat > /dev/null ;;
esac
exit 0
"""


def _run(report: str, tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    claude = bin_dir / "claude"
    claude.write_text("#!/bin/sh\ncat <<'REPORT_EOF'\n" + report + "\nREPORT_EOF\n")
    claude.chmod(0o755)

    docker = bin_dir / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)

    repo = tmp_path / "clone"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=repo,
                   check=True, env={**os.environ, "GIT_AUTHOR_NAME": "t",
                                    "GIT_AUTHOR_EMAIL": "t@example.com",
                                    "GIT_COMMITTER_NAME": "t",
                                    "GIT_COMMITTER_EMAIL": "t@example.com"})

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DOCKER_TRACE"] = str(tmp_path / "docker-trace")
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "placeholder-not-a-real-token"
    env["AUDIT_REPO_DIR"] = str(repo)
    env["AUDIT_CLAUDE_BIN"] = str(claude)
    env["HOME"] = str(tmp_path)
    (tmp_path / "logs").mkdir()

    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True,
                            text=True, check=False)
    # Absent when the script bailed before reaching docker, which is itself a
    # behaviour under test.
    trace_file = tmp_path / "docker-trace"
    return result, trace_file.read_text() if trace_file.exists() else ""


def _subject(trace: str) -> str:
    line = next(ln for ln in trace.splitlines() if "--subject" in ln)
    return line.split("--subject", 1)[1].strip()


@pytest.mark.parametrize(
    "first_line,expected",
    [
        ("healthy", "healthy"),
        ("Healthy", "healthy"),
        ("**healthy**", "healthy"),
        ("# healthy", "healthy"),
        ("healthy - nothing needs action in the last 168h", "healthy"),
        ("needs attention", "needs attention"),
        ("Needs attention: one stuck task", "needs attention"),
        ("broken", "broken"),
        ("BROKEN — the poller cannot authenticate", "broken"),
        ("not healthy, needs attention", "needs attention"),
    ],
)
def test_verdict_is_extracted_into_the_subject(first_line, expected, tmp_path):
    result, trace = _run(f"{first_line}\n\nSome body text.", tmp_path)

    assert result.returncode == 0, result.stderr
    assert _subject(trace) == f"Weekly production health: {expected}"


def test_unrecognisable_verdict_is_flagged_rather_than_guessed(tmp_path):
    result, trace = _run("The system appears to be fine.\n\nBody.", tmp_path)

    assert result.returncode == 0
    assert "unclear verdict" in _subject(trace)


def test_empty_report_fails_so_the_heartbeat_is_withheld(tmp_path):
    result, _ = _run("", tmp_path)

    assert result.returncode != 0
    assert "empty" in result.stderr.lower()


def test_the_report_is_kept_on_disk_for_diagnosis(tmp_path):
    _run("healthy\n\nAll clear.", tmp_path)

    kept = tmp_path / "logs" / "last-health-audit.md"
    assert kept.exists()
    assert "All clear." in kept.read_text()
