#!/usr/bin/env python3
"""Render and Squawk-lint Alembic revisions added since a base commit.

Applied migration history is immutable: modifying a revision that exists in
the base commit is an immediate failure. New revisions are rendered one at a
time in Alembic offline mode and checked with Squawk's default rules.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SKIP_MARKER = re.compile(
    r"^# migrations-lint: offline-skip - (?P<justification>\S.*)$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Revision:
    path: Path
    revision: str
    down_revision: str | None
    skip_justification: str | None


def _git(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable not found")
    # S603: argv only, no shell; args are fixed git diff/revision arguments.
    result = subprocess.run(  # noqa: S603
        [git, *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _literal_assignments(source: str) -> dict[str, object]:
    values: dict[str, object] = {}
    for node in ast.parse(source).body:
        target = None
        value_node = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value_node = node.value
        if (
            isinstance(target, ast.Name)
            and target.id in {"revision", "down_revision"}
            and value_node is not None
        ):
            values[target.id] = ast.literal_eval(value_node)
    return values


def parse_revision(path: Path) -> Revision:
    source = path.read_text()
    values = _literal_assignments(source)
    revision = values.get("revision")
    down_revision = values.get("down_revision")
    if not isinstance(revision, str):
        raise ValueError(f"{path}: revision must be a literal string")
    if down_revision is not None and not isinstance(down_revision, str):
        raise ValueError(
            f"{path}: only linear down_revision strings are supported; "
            "split or manually justify a branch/merge revision"
        )
    marker = SKIP_MARKER.search(source)
    return Revision(
        path=path,
        revision=revision,
        down_revision=down_revision,
        skip_justification=marker.group("justification") if marker else None,
    )


def changed_migrations(base: str, head: str) -> tuple[list[Path], list[Path]]:
    output = _git(
        "diff",
        "--no-renames",
        "--name-status",
        "--diff-filter=ADM",
        f"{base}..{head}",
        "--",
        "alembic/versions/*.py",
    )
    added: list[Path] = []
    modified: list[Path] = []
    for line in output.splitlines():
        status, raw_path = line.split("\t", 1)
        path = Path(raw_path)
        if status == "A":
            added.append(path)
        else:
            modified.append(path)
    return added, modified


def lint_revision(revision: Revision, squawk: Path) -> int:
    if revision.skip_justification:
        print(
            f"::warning file={revision.path}::SKIPPED offline migration lint "
            f"for {revision.revision}: {revision.skip_justification}"
        )
        return 0

    lower = revision.down_revision or "base"
    revision_range = f"{lower}:{revision.revision}"
    print(f"Rendering {revision.path}: alembic upgrade {revision_range} --sql")

    with tempfile.TemporaryDirectory(prefix="migration-sql-") as temp_dir:
        sql_path = Path(temp_dir) / f"{revision.revision}.sql"
        alembic = shutil.which("alembic")
        if alembic is None:
            print("::error::alembic executable not found")
            return 1
        environment = dict(os.environ)
        environment.setdefault(
            "DATABASE_URL",
            "postgresql+asyncpg://ci:ci@127.0.0.1:1/offline",
        )
        with sql_path.open("w") as sql_file:
            # S603: argv only, no shell; the range comes from literal revision
            # metadata in the checked-out migration.
            render = subprocess.run(  # noqa: S603
                [
                    alembic,
                    "upgrade",
                    revision_range,
                    "--sql",
                ],
                stdout=sql_file,
                env=environment,
                text=True,
            )
        if render.returncode:
            print(
                f"::error file={revision.path}::Alembic offline rendering "
                "failed. If this revision genuinely cannot render offline, add "
                "'# migrations-lint: offline-skip - <one-line justification>' "
                "inside the revision."
            )
            return render.returncode

        with sql_path.open() as sql_file:
            # S603: the executable is the pinned, checksum-verified workflow
            # input; argv is fixed and no shell is involved.
            lint = subprocess.run(  # noqa: S603
                [
                    str(squawk),
                    "--pg-version=17.0",
                    "--stdin-filepath",
                    str(revision.path.with_suffix(".sql")),
                ],
                stdin=sql_file,
                text=True,
            )
        return lint.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--squawk", required=True, type=Path)
    args = parser.parse_args()

    added, modified = changed_migrations(args.base, args.head)
    failed = False

    for path in modified:
        print(
            f"::error file={path}::Applied migration history is immutable; "
            "add a new revision instead of editing, deleting, or renaming "
            "this file."
        )
        failed = True

    if not added and not modified:
        print("No Alembic revisions added or changed in this diff.")
        return 0

    for path in added:
        try:
            revision = parse_revision(path)
        except (SyntaxError, ValueError) as exc:
            print(f"::error file={path}::{exc}")
            failed = True
            continue
        if lint_revision(revision, args.squawk):
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
