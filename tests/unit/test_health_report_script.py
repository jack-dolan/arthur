"""Tests for the checking-production-health bundled report script.

Two properties matter enough to be mechanical rather than reviewed:

1. **It is read-only.** The weekly unattended audit runs this script against
   production with no human watching. Every docker verb it uses and every SQL
   statement it issues is asserted here, so a future edit that adds a write has
   to delete a test to do it.
2. **It works on the platform (image) topology**, where there is no compose
   file, no published app port, and the app's log history is spread across
   several Swarm task containers.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[2]
SCRIPT = REPO / ".claude" / "skills" / "checking-production-health" / "scripts" / "health_report.sh"

APP_IMAGE = "ghcr.io/jack-dolan/home-rental-automation:sha-abc1234"

# A fake `docker` that answers the handful of read-only queries the script makes
# and records every invocation for the assertions below.
DOCKER_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_TRACE"
case "$1" in
  ps)
    case "$*" in
      *-a*) printf 'c_old\tapp-svc.1.old\t%s\tExited (0) 2 hours ago\n' "$IMAGE"
            printf 'c_new\tapp-svc.1.new\t%s\tUp 10 minutes\n' "$IMAGE"
            printf 'c_pg\tpg-service-xyz.1.abc\tpostgres:17\tUp 5 days\n' ;;
      *)    printf 'c_new\tapp-svc.1.new\t%s\tUp 10 minutes\n' "$IMAGE"
            printf 'c_pg\tpg-service-xyz.1.abc\tpostgres:17\tUp 5 days\n' ;;
    esac ;;
  logs)
    T=2026-07-30T21:42:41.000000000Z
    echo "$T app.schema_guard: SCHEMA GUARD: database revision == build head (0007); starting."
    echo "$T app.main: SCHEDULED JOBS: poll_booking_feed next fires 2026-07-30 22:00:00-04:00"
    echo "$T app.integrations.docusign: DocuSign target: PRODUCTION"
    echo "$T app.ingestion.poller: Processing 2 new messages" ;;
  exec)
    case "$*" in
      *DATABASE_URL*) echo 'postgresql+asyncpg://ruser:rpass@pg-service-xyz:5432/rdb' ;;
      *DOMAIN*)       echo 'arthur.example.com' ;;
      *job_runs*)     echo '{"job": "review_dead_letters", "reviewed": 3, "outcome": "ok"}' ;;
      *psql*)         echo 'stub-row' ;;
    esac ;;
  compose) exit 1 ;;
esac
exit 0
"""

CURL_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$CURL_TRACE"
printf 200
"""


def _run(tmp_path: Path, *args: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER_STUB)
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "curl").write_text(CURL_STUB)
    (bin_dir / "curl").chmod(0o755)

    work = tmp_path / "work"
    work.mkdir()

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["DOCKER_TRACE"] = str(tmp_path / "docker-trace")
    env["CURL_TRACE"] = str(tmp_path / "curl-trace")
    env["IMAGE"] = APP_IMAGE

    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    trace = (tmp_path / "docker-trace").read_text().splitlines()
    return result, trace


def test_report_runs_on_the_platform_topology_and_names_it(tmp_path):
    result, _ = _run(tmp_path, "24")

    assert result.returncode == 0, result.stderr
    assert "platform" in result.stdout.lower()
    for section in ("Containers", "Health endpoints", "Hard-failure signals",
                    "Workflow signals", "DB:", "log window"):
        assert section in result.stdout, f"missing section: {section}"


def test_every_docker_verb_is_read_only(tmp_path):
    """A write here would run unattended against production every week."""
    _, trace = _run(tmp_path, "24")

    allowed = {"ps", "logs", "exec", "compose", "inspect", "version"}
    for line in trace:
        verb = line.split()[0]
        assert verb in allowed, f"non-read-only docker verb: {line}"

    # And what it runs INSIDE a container is just as much a write risk as the
    # verb itself. Only three commands are ever exec'd, none of which mutates.
    for line in trace:
        if not line.startswith("exec"):
            continue
        words = [w for w in line.split() if w not in ("exec", "-i")]
        assert words[1] in ("printenv", "psql", "sh"), f"unexpected exec: {line}"


def test_every_sql_statement_is_a_select(tmp_path):
    _, trace = _run(tmp_path, "24")

    statements = [line for line in trace if "psql" in line]
    assert statements, "expected the report to query the database"
    for line in statements:
        assert "-Atc" in line
        after = line.split("-Atc", 1)[1].strip().lstrip("'\"")
        assert after.upper().startswith("SELECT"), f"non-SELECT statement: {line}"


def test_log_gathering_honours_the_requested_window(tmp_path):
    _, trace = _run(tmp_path, "168")

    log_calls = [line for line in trace if line.startswith("logs")]
    assert log_calls
    assert any("--since 168h" in line for line in log_calls)


def test_logs_are_collected_from_every_task_container_not_just_the_running_one(tmp_path):
    """A deploy rotates the container; a week's history spans several of them."""
    _, trace = _run(tmp_path, "168")

    windowed = [line for line in trace if line.startswith("logs") and "--since" in line]
    assert any("c_old" in line for line in windowed)
    assert any("c_new" in line for line in windowed)


def test_report_states_how_much_log_history_actually_exists(tmp_path):
    """Absence of a daily job is only a finding if the window really is covered."""
    result, _ = _run(tmp_path, "168")

    assert "log window" in result.stdout
    assert "2026-07-30T21:42" in result.stdout
