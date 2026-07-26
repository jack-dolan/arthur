"""Fail-safe schema-state guard for application startup.

Migrations are an explicit deployment step. Application boot only reads the
database's Alembic stamp and compares it with the revision graph bundled in
this image. In particular, it never asks Alembic to resolve a database revision
before deciding whether that revision is known: an older rollback image cannot
resolve the newer image's revision id, and that is the safe "database ahead"
case this guard must allow.
"""

from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

log = logging.getLogger("app.schema_guard")


class SchemaAction(enum.Enum):
    # Only START is representable: the behind case raises SchemaBehindError
    # rather than returning, so a caller cannot accidentally ignore it.
    START = "start"


@dataclass(frozen=True)
class RevisionCatalog:
    """The revision graph bundled in one application image."""

    ordered_revisions: tuple[str, ...]
    down_revisions: dict[str, tuple[str, ...]]
    heads: frozenset[str]

    @classmethod
    def from_alembic(cls, config_path: str | Path = "alembic.ini") -> RevisionCatalog:
        config = Config(str(config_path))
        scripts = ScriptDirectory.from_config(config)
        revisions = list(scripts.walk_revisions(base="base", head="heads"))
        revisions.reverse()

        down_revisions: dict[str, tuple[str, ...]] = {}
        for revision in revisions:
            raw_down = revision.down_revision
            if raw_down is None:
                down = ()
            elif isinstance(raw_down, tuple):
                down = raw_down
            else:
                down = (raw_down,)
            down_revisions[revision.revision] = down

        return cls(
            ordered_revisions=tuple(revision.revision for revision in revisions),
            down_revisions=down_revisions,
            heads=frozenset(scripts.get_heads()),
        )


@dataclass(frozen=True)
class SchemaCheck:
    action: SchemaAction
    database_revisions: tuple[str, ...]
    pending_revisions: tuple[str, ...] = ()
    unknown_revisions: tuple[str, ...] = ()


class SchemaBehindError(RuntimeError):
    """Raised when this build has migrations the database has not applied."""


def _applied_revisions(
    database_revisions: set[str],
    catalog: RevisionCatalog,
) -> set[str]:
    applied: set[str] = set()
    remaining = list(database_revisions)
    while remaining:
        revision = remaining.pop()
        if revision in applied:
            continue
        applied.add(revision)
        remaining.extend(catalog.down_revisions[revision])
    return applied


def check_schema_state(
    database_revisions: set[str],
    catalog: RevisionCatalog,
) -> SchemaCheck:
    """Classify the database stamp without changing the database."""
    database = set(database_revisions)
    known = set(catalog.ordered_revisions)
    unknown = tuple(sorted(database - known))
    database_display = ", ".join(sorted(database)) or "<unversioned>"

    if unknown:
        unknown_display = ", ".join(unknown)
        log.warning(
            "!!!!!!!!!!!!!!!! SCHEMA GUARD WARNING !!!!!!!!!!!!!!!! "
            "Database revision(s) %s are UNKNOWN TO THIS BUILD. "
            "The database is ahead (expected after an image rollback); "
            "starting application under the N-1 compatibility guarantee.",
            unknown_display,
        )
        return SchemaCheck(
            action=SchemaAction.START,
            database_revisions=tuple(sorted(database)),
            unknown_revisions=unknown,
        )

    if database == set(catalog.heads):
        head_display = ", ".join(sorted(catalog.heads)) or "<none>"
        log.info(
            "SCHEMA GUARD: database revision == build head (%s); "
            "starting application.",
            head_display,
        )
        return SchemaCheck(
            action=SchemaAction.START,
            database_revisions=tuple(sorted(database)),
        )

    applied = _applied_revisions(database, catalog)
    pending = tuple(
        revision for revision in catalog.ordered_revisions if revision not in applied
    )
    pending_display = ", ".join(pending) or "<none>"
    message = (
        f"SCHEMA GUARD: REFUSING TO START. Database revision "
        f"{database_display} is behind this build; pending revisions: "
        f"{pending_display}. Run the explicit migration command before "
        f"starting the application."
    )
    log.error(message)
    raise SchemaBehindError(message)


def _sync_database_url(raw_url: str) -> str:
    url = make_url(raw_url)
    if url.drivername == "postgresql+asyncpg":
        url = url.set(drivername="postgresql+psycopg2")
    return url.render_as_string(hide_password=False)


def read_database_revisions(database_url: str) -> set[str]:
    """Read the Alembic version table directly; never resolve its ids."""
    engine = create_engine(_sync_database_url(database_url), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            if not inspect(connection).has_table("alembic_version"):
                return set()
            rows = connection.execute(text("SELECT version_num FROM alembic_version"))
            return {row[0] for row in rows}
    finally:
        engine.dispose()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://rental_automation:change_me@localhost:5432/"
        "rental_automation",
    )
    catalog = RevisionCatalog.from_alembic()
    revisions = read_database_revisions(database_url)
    try:
        check_schema_state(revisions, catalog)
    except SchemaBehindError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
