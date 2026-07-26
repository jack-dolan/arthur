from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _fake_command(bin_dir: Path, name: str) -> None:
    command = bin_dir / name
    command.write_text(
        "#!/bin/sh\n"
        f'printf "{name} %s\\n" "$*" >> "$ENTRYPOINT_TRACE"\n'
    )
    command.chmod(0o755)


def _run_entrypoint(tmp_path: Path, *args: str) -> list[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("python", "uvicorn", "alembic"):
        _fake_command(bin_dir, name)

    trace = tmp_path / "trace"
    environment = dict(os.environ)
    environment["PATH"] = f"{bin_dir}:{environment['PATH']}"
    environment["ENTRYPOINT_TRACE"] = str(trace)
    result = subprocess.run(
        ["sh", "docker-entrypoint.sh", *args],
        cwd=Path(__file__).parents[2],
        env=environment,
        check=False,
    )
    assert result.returncode == 0
    return trace.read_text().splitlines()


def test_app_boot_checks_schema_without_running_migrations(tmp_path):
    trace = _run_entrypoint(tmp_path)

    assert trace[0] == "python -m app.schema_guard"
    assert trace[1].startswith("uvicorn app.main:app")
    assert all(not line.startswith("alembic ") for line in trace)


def test_migrate_command_runs_alembic_without_starting_app(tmp_path):
    trace = _run_entrypoint(tmp_path, "migrate")

    assert trace == ["alembic upgrade head"]
