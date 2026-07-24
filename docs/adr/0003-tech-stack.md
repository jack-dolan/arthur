# ADR 0003: Tech Stack

## Status
Accepted

## Context
The system runs on a VPS and must be easily migratable to a different VPS in the future without data loss or manual re-configuration.

## Decision
- **Language**: Python
- **Web framework**: FastAPI with Jinja2 server-rendered templates (v1 dashboard is read-only HTML pages; no frontend build step needed)
- **Database**: PostgreSQL (handles concurrent writes from background jobs cleanly; provenance tracking schema benefits from relational constraints)
- **Schema migrations**: Alembic (ensures clean restore on a new host)
- **Background scheduler**: APScheduler embedded in the FastAPI app (sufficient for current scale; no separate worker process)
- **Deployment**: Docker Compose — app, PostgreSQL, and any future services run as containers

## Portability
Migration procedure:
1. `pg_dump` the database on the old host
2. Copy the repo and `.env` file to the new host
3. `pg_restore` on the new host
4. `docker compose up`

All secrets are stored in `.env` (never committed to git). All configuration is code or environment variables — no manual steps live outside the repo.

## Consequences
- Docker adds a small operational layer but eliminates "it worked on the old server" migration problems.
- APScheduler is embedded; if the app process crashes, scheduled jobs stop. Acceptable for v1 — the consequence is a delayed (not lost) task, not data corruption.
- Alembic migration files must be committed with every schema change.
