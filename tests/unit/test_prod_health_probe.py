"""Tests for scripts/prod_health.sh — the production health probe.

The probe exists because the `deploying-to-vps` skill used to build the URL
from the DEV box's `.env`, where `DOMAIN=localhost`. The curl then failed to
connect and printed a bare `000`, which reads as "production is down" at the
worst possible moment. Two behaviours are therefore load-bearing here:

1. the hostname is resolved from something that describes PRODUCTION, and a
   development value is refused rather than probed;
2. "nothing answered" and "the app answered badly" produce different words and
   different exit codes.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[2]
SCRIPT = REPO / "scripts" / "prod_health.sh"


def _fake_curl(bin_dir: Path, *, body: str) -> None:
    """Install a stub `curl` whose behaviour the test dictates."""
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\n" + body)
    curl.chmod(0o755)


def _run(tmp_path: Path, *args: str, curl_body: str, **environment: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_curl(bin_dir, body=curl_body)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    # Never let the real host-resolution paths run inside a unit test.
    env.pop("DEPLOY_VPS_SSH", None)
    env.update(environment)

    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_healthy_host_reports_healthy_and_exits_zero(tmp_path):
    result = _run(tmp_path, "arthur.example.com", curl_body='printf 200\n')

    assert result.returncode == 0
    assert "HEALTHY" in result.stdout
    assert "https://arthur.example.com/health" in result.stdout
    assert "200" in result.stdout


def test_non_200_is_unhealthy_and_names_the_status_code(tmp_path):
    result = _run(tmp_path, "arthur.example.com", curl_body='printf 502\n')

    assert result.returncode == 1
    assert "UNHEALTHY" in result.stdout
    assert "502" in result.stdout


def test_connection_failure_says_unreachable_and_never_prints_a_bare_000(tmp_path):
    """A connection failure is not a status code and must not look like one."""
    result = _run(tmp_path, "no-such-host.invalid", curl_body='printf 000\nexit 6\n')

    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert "UNREACHABLE" in combined
    assert "no-such-host.invalid" in combined
    # The whole point: an unreachable host is not reported as an app verdict.
    assert "UNHEALTHY" not in combined
    assert combined.strip() != "000"


def test_localhost_is_refused_rather_than_probed(tmp_path):
    """The original bug, made impossible: a dev value can never be the target."""
    result = _run(tmp_path, curl_body='printf 200\n', PROD_DOMAIN="localhost")

    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert "localhost" in combined
    assert "HEALTHY" not in combined


def test_unresolvable_hostname_source_fails_loudly(tmp_path):
    """No argument, no override, no way to ask production: say so, don't guess."""
    result = _run(tmp_path, curl_body='printf 200\n', PROD_HEALTH_NO_RESOLVE="1")

    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert "hostname" in combined.lower()
    assert "HEALTHY" not in combined


def test_probe_targets_https_and_the_health_path(tmp_path):
    """Guards the URL shape itself, which is what the old snippet got wrong."""
    trace = tmp_path / "curl-args"
    result = _run(
        tmp_path,
        "arthur.example.com",
        curl_body=f'printf "%s\\n" "$*" >> {trace}\nprintf 200\n',
    )

    assert result.returncode == 0
    assert "https://arthur.example.com/health" in trace.read_text()
