# Operations

How the service is deployed, backed up, monitored, and moved to another host.
The README covers what the system *is*; this page covers running it.

---

## Deployment topology

One Docker Compose stack on a single VPS:

| Service | Image | Role |
|---|---|---|
| `app` | built from `Dockerfile` | FastAPI + APScheduler in one process |
| `db` | `postgres:17` | application database (named volume `pgdata`) |
| `caddy` | `caddy:2` | TLS termination and reverse proxy on 80/443 |

The app publishes only on `127.0.0.1:8000`. Public traffic must arrive through
Caddy, which reaches the app over the internal Docker network — publishing on
`0.0.0.0` would expose the dashboard session cookie in plaintext HTTP.

`docker-entrypoint.sh` runs `alembic upgrade head` before starting uvicorn, so
a fresh database migrates itself on first boot and a deploy carrying a new
migration applies it automatically.

On startup the lifespan handler runs a credential guard: it refuses to boot if
any required credential is missing, if `SECRET_KEY` is still the placeholder,
or if `DATABASE_URL` still contains `change_me` — naming the offending field in
the error. A service that runs unattended should fail loudly at boot rather
than quietly at 3 a.m.

The Caddy service is also the host's front proxy for a second, unrelated site,
which is why `docker-compose.yml` joins an external network and mounts an
extra site-config directory. Neither is needed for a single-tenant deployment.

### Deploying a change

```bash
docker compose up -d --build app
```

App-only by choice: recreating `caddy` briefly drops TLS for everything on the
host, and no app change requires it.

### Editing `config.yaml` on a running stack

`config.yaml` is a **bind-mounted single file**. Editors that write via atomic
rename (`sed -i`, most editors) change the file's inode, and the running
container stays pinned to the old one — so a plain `docker compose restart`
will **not** pick up the change. After editing `config.yaml`:

```bash
docker compose up -d --force-recreate app
```

---

## Sandbox vs production

Two integrations have a sandbox/production distinction, both selected by `.env`:

- **DocuSign** — `DOCUSIGN_SANDBOX=true` targets the demo hosts; `false` targets
  production. In production, DocuSign is **multi-region**:
  `DOCUSIGN_API_BASE_URI` must be this account's own REST base from OAuth
  userinfo (e.g. `https://na4.docusign.net/restapi`) — a global host like
  `www.docusign.net` will fail for a non-www account. The five `DOCUSIGN_*`
  credentials, the Connect **HMAC key**, and `config.yaml`'s
  `docusign_template_id` are all account-scoped: production values differ from
  sandbox and do **not** transfer at go-live (redirect URIs and secrets must be
  re-added in the production Apps & Keys).
- **Seam** — the API key *is* the environment. The production key points at the
  production workspace and the real lock (`seam_device_id` in `config.yaml`).

**Confirm which environment is live** — every boot logs it:

```bash
docker compose logs app | grep "DocuSign target"
# PRODUCTION → "... PRODUCTION (real envelopes, real money) | oauth=account.docusign.com | api=https://na4.docusign.net/restapi"
# SANDBOX    → "... SANDBOX (demo tier) | oauth=account-d.docusign.com | api=https://demo.docusign.net/restapi"
```

### Reverting to sandbox

Restore the sandbox values in `.env` and `config.yaml` and recreate the app:

1. In `.env`: restore the five sandbox `DOCUSIGN_*` values + the sandbox
   `DOCUSIGN_HMAC_KEY` + the sandbox `SEAM_API_KEY`; set
   `DOCUSIGN_SANDBOX=true`; clear `DOCUSIGN_API_BASE_URI=`.
2. In `config.yaml`: restore the sandbox `docusign_template_id` and
   `seam_device_id`.
3. `docker compose up -d --build app`, then confirm the banner reads
   **SANDBOX**.

(The swap is symmetric — going live is the same steps in reverse.) Note that a
sandbox credential set is not recorded anywhere off-host, so rolling back
requires re-minting sandbox credentials from scratch.

---

## DocuSign token upkeep

The production **refresh token expires 30 days after its last use**. The weekly
`refresh_docusign_token` keep-alive job resets that clock — a token-only
exchange, no envelope, no cost — and every rotation is persisted to
**`/app/data/docusign_refresh_token`** on the `pdf_data` volume, so rotations
**survive container restarts**. The exchange prefers that stored token over the
(possibly stale) `.env` one and falls back to `.env` automatically if the
stored token is ever rejected. A failed keep-alive **emails the owner** (weekly
at most).

Re-minting is therefore only needed if the keep-alive alert fires repeatedly,
the `pdf_data` volume is lost, or the stack is down for more than 30 days:

```bash
# From a laptop; the redirect URI http://localhost:8765/callback must be
# registered in the PRODUCTION Apps & Keys.
ssh -t -L 8765:localhost:8765 <user>@<host> \
  'cd <repo-dir> && python3 scripts/manual/get_docusign_refresh_token.py production'
```

Then `docker compose up -d --force-recreate app` so the app loads the new token.

---

## Backups

### Prerequisites

`POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` are defined in `.env`
and consumed by the `db` service in `docker-compose.yml` via `${VAR}`
substitution. The backup and restore scripts also invoke
`docker compose exec db pg_dump`/`psql` with these variables, so the host shell
must have them exported before running either script:

```bash
# Option 1 — source directly (works when .env is plain KEY=VALUE)
source .env

# Option 2 — more portable (skips comment lines)
export $(grep -v '^#' .env | xargs)
```

`docker compose up` must be running and the `db` service healthy, because both
scripts reach the database through `docker compose exec`.

### Taking a backup

```bash
bash scripts/backup.sh
```

Two files are produced: `backup_YYYYMMDD_HHMMSS.sql` (a `pg_dump` in plain SQL
format — no `-Fc`, so it is human-readable and restorable with `psql`) and
`pdfs_YYYYMMDD_HHMMSS.tar.gz` (a tar of the `pdf_data` volume's `pdfs/`
directory — the signed DocuSign forms, which live outside Postgres and would
otherwise be in no backup at all). Both are written to `BACKUP_DIR` (default:
current directory) and `chmod 600`.

Both filename patterns are explicitly gitignored. Two env vars tune the script:
`BACKUP_DIR` (destination, created if missing) and `RETENTION_DAYS` (default
`14` — older backups are pruned by filename pattern, scoped to `BACKUP_DIR`).

### Nightly cron

`scripts/backup.sh` is operator-invoked; nothing in the repo schedules it. In
production a crontab entry chains the local backup, the off-site upload, and a
heartbeat ping:

```cron
0 3 * * * cd <repo-dir> && \
  bash -c 'set -a; source .env; set +a; \
    BACKUP_DIR=$HOME/backups/rental RETENTION_DAYS=14 bash scripts/backup.sh && \
    BACKUP_DIR=$HOME/backups/rental bash scripts/offsite_backup.sh && \
    curl -fsS --retry 3 "$HEALTHCHECKS_PING_URL_BACKUP"' \
  >> $HOME/backups/rental/cron.log 2>&1
```

The chain is deliberate: the heartbeat fires only after **both** the local
backup and the off-site upload succeed, so a silent failure of either shows up
as heartbeat silence. Install this version only once the R2 (`R2_*`,
`BACKUP_ENCRYPTION_PASSPHRASE`) and `HEALTHCHECKS_PING_URL_BACKUP` values are
filled in `.env` — with them missing, `offsite_backup.sh` exits non-zero and
every night would page you.

`BACKUP_DIR` keeps output out of the git working tree. Local backups alone are
**not** disaster recovery — they live on the same host as the data they protect,
which is what the off-site copy below is for.

### Off-site backups (Cloudflare R2)

`scripts/offsite_backup.sh` gpg-encrypts and uploads the newest DB dump, PDF
tarball, `.env`, and `config.yaml` to a private R2 bucket, verifies the upload,
and prunes each file type to the newest `OFFSITE_RETENTION_COUNT` (default 25)
versions. Config lives in `.env` (`R2_*`, `BACKUP_ENCRYPTION_PASSPHRASE`);
requires `rclone` and `gpg` on the host. **Keep the passphrase in a password
manager** — without it the off-site copies are unreadable.

Restore a file:

```bash
rclone lsf r2off:$R2_BUCKET
gpg -d --passphrase "$BACKUP_ENCRYPTION_PASSPHRASE" --batch db_YYYY….sql.gpg > restore.sql
```

---

## Restore

```bash
bash scripts/restore.sh backup_YYYYMMDD_HHMMSS.sql
```

The script runs `docker compose exec -T db psql -U "${POSTGRES_USER}" "${POSTGRES_DB}" < $1`.
It uses `psql`, not `pg_restore` — plain SQL format and custom format are not
interchangeable.

Restore is destructive on conflicting rows. Restoring into a non-empty database
may fail on duplicate key violations or produce duplicate data. For a clean
restore, drop and recreate the target database first, or target a fresh
container.

The dump sets object ownership to the `POSTGRES_USER` role (via `OWNER TO`) but
does not `CREATE ROLE` it, so the restore must run against a database whose
owning role already exists. That is the case in the standard flow below:
`docker compose up` creates the role from `POSTGRES_USER` when the `db` volume
is first initialized, before the restore step.

To restore the PDF volume:

```bash
docker compose exec -T app tar xzf - -C /app/data < pdfs_YYYYMMDD_HHMMSS.tar.gz
```

This is additive — fine for disaster recovery into an empty volume, but check
for conflicts first if the volume already has PDFs.

---

## Full VPS migration

Portability is a design requirement: the service must be movable to a new host
with no vendor-specific steps.

1. **On the old host:** export env vars and create a backup.

   ```bash
   source .env && bash scripts/backup.sh
   ```

2. **Copy to the new host** via `scp` or `rsync`:
   - the repository (`git clone`, or copy the directory)
   - `.env`
   - `config.yaml`
   - the `backup_*.sql` and `pdfs_*.tar.gz` files from step 1

3. **On the new host:** install Docker and Docker Compose, then start the stack.

   ```bash
   docker compose up -d
   ```

   The `app` service refuses to start if any required credential is missing and
   names the missing field. Resolve credential gaps before proceeding.

4. **On the new host:** restore the database and the PDF volume.

   ```bash
   source .env && bash scripts/restore.sh backup_YYYYMMDD_HHMMSS.sql
   docker compose exec -T app tar xzf - -C /app/data < pdfs_YYYYMMDD_HHMMSS.tar.gz
   ```

5. **Verify:**

   ```bash
   # Caddy 308-redirects http -> https, so follow redirects and target HTTPS.
   curl -L http://<your-domain>/health
   # On localhost (Caddy serves an internal self-signed cert), add -k:
   curl -kL https://localhost/health
   # Or bypass Caddy and hit the app directly:
   curl http://localhost:8000/health
   ```

   Expect `200` with body `{"status":"ok"}`. A bare `curl http://localhost/health`
   returns `308` — the redirect to HTTPS — which is expected, not an error.
   Also check `docker compose logs app` for a clean startup.

### What the scripts do not migrate

- **APScheduler state** — jobs are registered in memory at startup and
  re-register on each start. Nothing to migrate.
- **OAuth refresh tokens** — they live in `.env`, which is copied in step 2.

Signed DocuSign PDFs used to be on this list; `scripts/backup.sh` now tars them
alongside the SQL dump, so steps 1, 2 and 4 cover them like any other backup
file.

---

## Monitoring

The app cannot report its own death — every alert it sends travels through its
own Gmail token — so the outermost layer of monitoring is external.

- **Uptime** (UptimeRobot): checks `/health` from outside the host; catches
  app, host, Caddy and TLS failures.
- **Heartbeats** (healthchecks.io): jobs ping a unique URL only on **success**,
  and the monitor alerts when pings stop, through its own channel:

  | Check | Pinged by | Expected cadence |
  |---|---|---|
  | poller | every completed poll cycle | 5 min (alert after ~15–20 min silence) |
  | credential-sentinel | daily `verify_credentials`, all checks passing | daily |
  | docusign-keepalive | weekly keep-alive success | weekly (Mon 03:30 ET) |
  | nightly-backup | the backup crontab, after local + off-site succeed | daily |

  Ping URLs live in `.env` (`HEALTHCHECKS_PING_URL_*`); empty means pings are
  silently skipped, so the app runs fine before the monitor is configured.
  After editing `.env`, run `docker compose up -d --force-recreate app`.

In-app monitoring jobs complement this:

| Job | Schedule | What it proves |
|---|---|---|
| `verify_credentials` | daily 07:00 ET | every integration credential still works (read-only probes) |
| `requeue_stalled_automations` | daily 08:30 ET | FAILED tasks are retried; orphaned PENDING tasks are re-dispatched; a digest goes to the owner |
| `verify_access_codes` | daily 09:00 ET | door codes for upcoming stays actually exist on the lock (Seam programs devices asynchronously) |
| `check_classifier_drift` | Sun 09:00 ET | platform emails falling through to OTHER get a human-review digest — the silent failure mode when a platform changes its email format |
| `send_monthly_status_email` | 1st, 08:00 ET | the alert send path works end to end; the report's **absence** is itself an alarm |

---

## Recovering a broken credential

Credential expiry is the most common failure mode in a system built on four
OAuth-shaped integrations. Symptoms (`invalid_grant`, 401s, a sentinel alert, a
DocuSign keep-alive failure email) and the repair procedure for each integration
are documented in the `recovering-credentials` skill under `.claude/skills/`.
