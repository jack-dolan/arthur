"""Tests for scripts/ci_verify_host.sh — the post-deploy verification check.

CI syncs this script to /home/jack/bin/ci_verify.sh on the production host and
runs it as the last gate of a deploy. It answers one question: did a NEW
container, running the image we just deployed, come up and pass its own schema
guard?

The failure that motivated these tests is a FALSE RED. On 2026-08-03 (run
30815410291) a healthy deploy was reported as failed with exit code 141. The
script piped `docker logs` into `grep -q` / `grep -m1`, both of which exit on
the first match. When the check caught the container early — and the
schema-guard line is one of the first lines the app writes — grep exited while
`docker logs` was still streaming, `docker logs` took SIGPIPE, `pipefail`
surfaced 141 and `set -e` aborted before the script could report success. It
had been passing by luck on every previous deploy, whenever the log happened to
be small enough to fit the pipe buffer.

A false red is the specific harm the script's own comments warn about: it
teaches the operator to ignore the pipeline. So the size of the container's log
is a real input here, and `test_succeeds_when_the_container_log_is_large` is
the regression test for the bug itself.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[2]
SCRIPT = REPO / "scripts" / "ci_verify_host.sh"

EXPECTED = "ghcr.io/jack-dolan/home-rental-automation:sha-new123"
PREVIOUS = "oldcontainerid"

GUARD_LINE = (
    "2026-08-03 12:55:22,626 INFO     app.schema_guard: SCHEMA GUARD: "
    "database revision == build head (0007); starting application."
)
DOCUSIGN_LINE = (
    "2026-08-03 12:55:24,101 INFO     app.integrations.docusign.client: "
    "DocuSign target: PRODUCTION (real envelopes, real money)"
)


def _run(
    tmp_path: Path,
    *,
    ps_line: str,
    log_body: str,
    expected: str = EXPECTED,
    previous: str = PREVIOUS,
):
    """Run the script against a stub `docker` whose output the test dictates.

    `ps_line` is what `docker ps` prints (empty string for "no container").
    `log_body` is the container's whole log, so a test can make it arbitrarily
    large.

    The stub `cat`s prepared files rather than echoing inline, and deliberately
    does NOT end in `exit 0`. That matters more than it looks: a stub that
    swallows its own exit status cannot reproduce the bug these tests exist
    for. Real `docker logs` is killed by SIGPIPE when its reader stops early
    and exits 141, and `cat` of a large file behaves identically.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    ps_file = tmp_path / "ps.txt"
    ps_file.write_text(f"{ps_line}\n" if ps_line else "")
    log_file = tmp_path / "logs.txt"
    log_file.write_text(log_body)

    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  ps) exec cat "{ps_file}" ;;\n'
        f'  logs) exec cat "{log_file}" ;;\n'
        "esac\n"
    )
    docker.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    # Keep the sad paths fast: two attempts, no sleeping between them.
    env["VERIFY_ATTEMPTS"] = "2"
    env["VERIFY_INTERVAL"] = "0"

    return subprocess.run(
        ["bash", str(SCRIPT), expected, previous],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _filler(byte_target: int = 1_000_000) -> str:
    """Enough trailing output that a reader stopping early leaves a blocked writer.

    64 KiB is the usual Linux pipe capacity, so a megabyte is comfortably past
    the point where the writer must still be writing when the reader quits.
    """
    line = "INFO:     10.0.1.10:0 - \"GET /health HTTP/1.1\" 200 OK\n"
    return line * (byte_target // len(line))


def _small_log() -> str:
    return f"{GUARD_LINE}\n{DOCUSIGN_LINE}\n"


def _large_log() -> str:
    """The guard line first, then far more output than a pipe buffer holds."""
    return f"{GUARD_LINE}\n{DOCUSIGN_LINE}\n{_filler()}"


# --------------------------------------------------------------------------
# Happy paths
# --------------------------------------------------------------------------


def test_succeeds_when_a_new_container_runs_the_expected_image(tmp_path):
    result = _run(tmp_path, ps_line=f"newcid123 {EXPECTED}", log_body=_small_log())

    assert result.returncode == 0, result.stdout + result.stderr
    assert "verify: ok" in result.stdout
    assert "newcid123" in result.stdout
    assert "SCHEMA GUARD" in result.stdout


def test_succeeds_when_the_container_log_is_large(tmp_path):
    """Regression test for the 2026-08-03 false red (exit 141, SIGPIPE).

    Identical to the test above except that the container has written a lot of
    output after its schema-guard line. That must not change the verdict.
    """
    result = _run(tmp_path, ps_line=f"newcid123 {EXPECTED}", log_body=_large_log())

    assert result.returncode == 0, (
        f"exit {result.returncode} on a healthy deploy with a large container "
        f"log — 141 means SIGPIPE reached the script again.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "verify: ok" in result.stdout


def test_prints_the_docusign_banner_when_present(tmp_path):
    """Not a gate, but a wrong-target deploy must be visible in the run log."""
    result = _run(tmp_path, ps_line=f"newcid123 {EXPECTED}", log_body=_small_log())

    assert result.returncode == 0
    assert "DocuSign target: PRODUCTION" in result.stdout


def test_missing_docusign_banner_does_not_fail_the_deploy(tmp_path):
    """The banner is informational; only the schema guard gates."""
    result = _run(
        tmp_path,
        ps_line=f"newcid123 {EXPECTED}",
        log_body=f"{GUARD_LINE}\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "verify: ok" in result.stdout


# --------------------------------------------------------------------------
# Sad paths — each must actually fail
# --------------------------------------------------------------------------


def test_fails_when_no_container_is_running(tmp_path):
    result = _run(tmp_path, ps_line="", log_body=_small_log())

    assert result.returncode == 1
    assert "did not converge" in result.stderr
    assert "verify: ok" not in result.stdout


def test_fails_when_the_container_runs_the_wrong_image(tmp_path):
    """A deploy that silently kept serving an older tag is a failure."""
    stale = "ghcr.io/jack-dolan/home-rental-automation:sha-old999"
    result = _run(tmp_path, ps_line=f"newcid123 {stale}", log_body=_small_log())

    assert result.returncode == 1
    assert "did not converge" in result.stderr
    assert "verify: ok" not in result.stdout


def test_fails_when_the_container_did_not_restart(tmp_path):
    """Same container ID as before the deploy means nothing was replaced.

    This is why the previous container ID is an argument at all: comparing
    images alone passes instantly when the same tag is redeployed.
    """
    result = _run(tmp_path, ps_line=f"{PREVIOUS} {EXPECTED}", log_body=_small_log())

    assert result.returncode == 1
    assert "did not converge" in result.stderr
    assert "verify: ok" not in result.stdout


def test_fails_when_the_schema_guard_line_never_appears(tmp_path):
    """A new container on the right image that never passed its own guard."""
    result = _run(
        tmp_path,
        ps_line=f"newcid123 {EXPECTED}",
        log_body="some unrelated startup noise\n",
    )

    assert result.returncode == 1
    assert "did not converge" in result.stderr
    assert "verify: ok" not in result.stdout


def test_fails_when_the_guard_line_is_absent_from_a_large_log(tmp_path):
    """The fix must not turn every large log into a pass.

    Reading the whole log instead of stopping at the first match is the fix;
    this proves the check still discriminates once it does that.
    """
    result = _run(
        tmp_path,
        ps_line=f"newcid123 {EXPECTED}",
        log_body=_filler(),
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "did not converge" in result.stderr
    assert "verify: ok" not in result.stdout
