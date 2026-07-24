# Home Rental Automation

A self-hosted Python service that automates the pre-arrival workflow for short-term rental properties listed on Airbnb and VRBO. When a booking confirmation email arrives, the service parses it, stores the booking, and drives a checklist of tasks (dashboard data entry, DocuSign guest form, Seam access code, Google Sheets cleaner row, HOA email) without owner intervention beyond entering the one or two fields the platforms withhold from confirmation emails. The automation scope is booking confirmation through guest check-in; everything after check-in is manual.

## Migration

This section documents how to back up and restore the application database and how to migrate the service to a new VPS host.

### Prerequisites

`POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` are defined in `.env` and consumed by the `db` service in `docker-compose.yml` via `${VAR}` substitution. The backup and restore scripts also invoke `docker compose exec db pg_dump`/`psql` with these variables, so the host shell must have them exported before running either script.

Export them from `.env` using either of these equivalent methods:

```bash
# Option 1 — source directly (works when .env is plain KEY=VALUE with no subshell export)
source .env

# Option 2 — more portable export (handles comment lines)
export $(grep -v '^#' .env | xargs)
```

In addition, `docker compose up` must be running and the `db` service must be healthy before backup or restore commands will work, because both scripts use `docker compose exec` to reach the running container.

### Backup

```bash
bash scripts/backup.sh
```

The script produces two files: `backup_YYYYMMDD_HHMMSS.sql` (a `pg_dump` of the database, plain SQL format — no `-Fc` flag, so it is human-readable and restorable with `psql`) and `pdfs_YYYYMMDD_HHMMSS.tar.gz` (a tar of the `pdf_data` volume's `pdfs/` directory — the signed DocuSign forms, which live outside Postgres and would otherwise be in no backup at all — F-A, bug hunt 2026-07-22). Both are written to `BACKUP_DIR` (default: current directory) and chmod 600.

Neither file is gitignored by name pattern accident — `backup_*.sql` and `pdfs_*.tar.gz` are explicitly excluded in `.gitignore`. You are responsible for transferring them to secure, off-host storage if you need recovery beyond this VPS (the nightly cron below only protects against local mistakes/corruption, not a lost host).

Two env vars tune it: `BACKUP_DIR` (destination directory, created if missing) and `RETENTION_DAYS` (default `14` — backups older than this are pruned by filename pattern, scoped to `BACKUP_DIR`).

**Nightly cron (production):** a crontab entry runs this automatically — see "Production operations" below for the exact line and current status on `the-vps`.

### Restore

```bash
bash scripts/restore.sh backup_YYYYMMDD_HHMMSS.sql
```

The script invokes `docker compose exec -T db psql -U "${POSTGRES_USER}" "${POSTGRES_DB}" < $1`. It uses `psql`, not `pg_restore` — plain SQL format and custom format are not interchangeable.

Restore is destructive on conflicting rows. Restoring into a non-empty database may fail on duplicate key violations or produce duplicate data. For a clean restore, either drop and recreate the target database first, or target a fresh container.

The dump sets object ownership to the `POSTGRES_USER` role (via `OWNER TO`) but does not `CREATE ROLE` it. Restore therefore must run against a database whose owning role already exists — which is the case for the standard flow below, because `docker compose up` creates that role from `POSTGRES_USER` when the `db` volume is first initialized, before the restore step.

To restore the PDF volume from a `pdfs_YYYYMMDD_HHMMSS.tar.gz` backup:

```bash
docker compose exec -T app tar xzf - -C /app/data < pdfs_YYYYMMDD_HHMMSS.tar.gz
```

This is additive (extracts on top of whatever is already in the volume) — fine for disaster recovery into an empty volume, but check for conflicts first if the volume already has PDFs.

### Full VPS Migration Procedure

1. **On the old host:** export env vars and create a backup.

   ```bash
   source .env && bash scripts/backup.sh
   ```

   This produces `backup_YYYYMMDD_HHMMSS.sql` and `pdfs_YYYYMMDD_HHMMSS.tar.gz` in the current directory (the latter is the signed-PDF volume — see "What is NOT migrated by these scripts" below).

2. **Copy the following to the new host** via `scp`, `rsync`, or manual transfer:
   - The repository (either `git clone` from remote or `scp` the directory)
   - `.env`
   - `config.yaml`
   - The `backup_*.sql` and `pdfs_*.tar.gz` files produced in step 1

3. **On the new host:** install Docker and Docker Compose, then start the stack.

   ```bash
   docker compose up -d
   ```

   This brings up the `app` and `db` services. The `app` service will refuse to start if any required credential is missing in `.env` — it will name the missing field in the error. Resolve credential gaps before proceeding.

4. **On the new host:** export env vars and restore the database, then restore the PDF volume.

   ```bash
   source .env && bash scripts/restore.sh backup_YYYYMMDD_HHMMSS.sql
   docker compose exec -T app tar xzf - -C /app/data < pdfs_YYYYMMDD_HHMMSS.tar.gz
   ```

5. **Verify:** confirm the service is healthy.

   ```bash
   # Caddy 308-redirects http -> https, so follow redirects and target HTTPS.
   # On a real domain with a valid certificate:
   curl -L http://<your-domain>/health
   # On localhost (Caddy serves an internal self-signed cert), add -k:
   curl -kL https://localhost/health
   # Or bypass Caddy and hit the app directly (port 8000 is published):
   curl http://localhost:8000/health
   ```

   The final response should be `200` with body `{"status":"ok"}`. Note that a bare
   `curl http://localhost/health` returns `308` — the redirect to HTTPS — **not** `200`;
   that is expected, not an error. Also check application logs (`docker compose logs app`)
   for a clean startup. On boot the Docker entrypoint runs `alembic upgrade head` before
   uvicorn, so a fresh database is migrated automatically. The `app/main.py` lifespan then
   runs the credential guard: it validates the required API credentials (including the
   `GOOGLE_OAUTH_CLIENT_ID`/`SECRET` for dashboard Google login) and rejects a default or
   placeholder `SECRET_KEY` or `DATABASE_URL`, refusing to start with a clear error naming
   the offending field if any check fails. (Dashboard access is per-user Google login with
   an email allowlist in `config.yaml`; there is no shared dashboard password.)

### What is NOT migrated by these scripts

- **APScheduler state**: Jobs are registered in-memory at startup and re-register automatically on each start. No migration needed.
- **OAuth refresh tokens**: These live in `.env`, which is copied in step 2. They survive migration as long as `.env` is transferred.

Signed DocuSign PDFs (`pdf_data` Docker named volume, mounted at `/app/data`; written under `/app/data/pdfs/<booking-id>.pdf`) used to be in this "not migrated" list — `scripts/backup.sh` now tars them alongside the SQL dump (F-A, bug hunt 2026-07-22), so step 1/2/4 above cover them like any other backup file.

## Production operations

The live deployment runs on **`the-vps`** at **`https://arthur.example.com`** (reached over Tailscale SSH; repo at `~/Documents/workspace/home-rental-automation`).

### Sandbox vs production

Two integrations have a sandbox/production distinction, both selected by `.env`:

- **DocuSign** — `DOCUSIGN_SANDBOX=true` targets the demo hosts; `false` targets production. In production, DocuSign is **multi-region**: `DOCUSIGN_API_BASE_URI` must be this account's own REST base from OAuth userinfo (e.g. `https://na4.docusign.net/restapi`) — a global host like `www.docusign.net` will fail for a non-www account. The five `DOCUSIGN_*` credentials, the Connect **HMAC key**, and `config.yaml`'s `docusign_template_id` are all account-scoped: production values differ from sandbox and do **not** transfer at go-live (redirect URIs and secrets must be re-added in the production Apps & Keys).
- **Seam** — the API key *is* the environment. The production key points at the production workspace and the real lock (`seam_device_id` in `config.yaml`).

**Confirm which environment is live** — every boot logs it:

```bash
docker compose logs app | grep "DocuSign target"
# PRODUCTION → "... PRODUCTION (real envelopes, real money) | oauth=account.docusign.com | api=https://na4.docusign.net/restapi"
# SANDBOX    → "... SANDBOX (demo tier) | oauth=account-d.docusign.com | api=https://demo.docusign.net/restapi"
```

### Rollback (aborting go-live)

To revert to sandbox — e.g. if a production issue needs isolating — restore the sandbox values in `.env` and `config.yaml` and recreate the app:

1. In `.env`: restore the five sandbox `DOCUSIGN_*` values + the sandbox `DOCUSIGN_HMAC_KEY` + the sandbox `SEAM_API_KEY`; set `DOCUSIGN_SANDBOX=true`; clear `DOCUSIGN_API_BASE_URI=`.
2. In `config.yaml`: restore the sandbox `docusign_template_id` and `seam_device_id`.
3. `docker compose up -d --build app`, then confirm the banner reads **SANDBOX**.

(The swap is symmetric — going live is the same steps in reverse.) Note: the sandbox credential set is **not** recorded anywhere off-host — rolling back to sandbox requires re-minting sandbox credentials (DocuSign demo app, Seam sandbox workspace) from scratch.

### DocuSign token upkeep

The production **refresh token expires 30 days after its last use**. The weekly `refresh_docusign_token` keep-alive job resets that clock (a token-only exchange — no envelope, no cost), and every rotation is persisted to **`/app/data/docusign_refresh_token`** on the `pdf_data` volume, so rotations **survive container restarts** — the exchange prefers that stored token over the (possibly stale) `.env` one, and falls back to `.env` automatically if the stored token is ever rejected. A failed keep-alive **emails the owner** (weekly at most).

Re-minting is therefore only needed if the keep-alive alert fires repeatedly, the `pdf_data` volume is lost, or the stack is down >30 days:

```bash
# From your laptop (the redirect URI http://localhost:8765/callback must be
# registered in the PRODUCTION Apps & Keys):
ssh -t -L 8765:localhost:8765 jack@the-vps \
  'cd ~/Documents/workspace/home-rental-automation && \
   python3 scripts/manual/get_docusign_refresh_token.py production'
```

Then `docker compose up -d --force-recreate app` so the app loads the new token.

### Nightly backups

`scripts/backup.sh` is operator-invoked — nothing in the repo schedules it. On `the-vps` a crontab entry runs it nightly (F-A, bug hunt 2026-07-22):

```cron
0 3 * * * cd ~/Documents/workspace/home-rental-automation && \
  bash -c 'set -a; source .env; set +a; \
    BACKUP_DIR=$HOME/backups/home-rental-automation RETENTION_DAYS=14 bash scripts/backup.sh && \
    BACKUP_DIR=$HOME/backups/home-rental-automation bash scripts/offsite_backup.sh && \
    curl -fsS --retry 3 "$HEALTHCHECKS_PING_URL_BACKUP"' \
  >> $HOME/backups/home-rental-automation/cron.log 2>&1
```

The chain is deliberate: the heartbeat ping fires only after BOTH the local
backup and the off-site upload succeed, so a silent failure of either shows up
as heartbeat silence at healthchecks.io. Install this version only once the
R2 (`R2_*`, `BACKUP_ENCRYPTION_PASSPHRASE`) and `HEALTHCHECKS_PING_URL_BACKUP`
values are filled in `.env` — with them missing, `offsite_backup.sh` exits
non-zero and every night would page you.

Install with `crontab -e` on the VPS. `BACKUP_DIR` keeps output out of the git working tree; `RETENTION_DAYS=14` matches the script's default (set explicitly here so the crontab line is self-documenting). This protects against local mistakes and corruption — it is **not** off-host disaster recovery; the backups live on the same VPS as the data they're backing up, so a lost host loses both. Periodically copy `$HOME/backups/home-rental-automation` off-host (e.g. `rsync` to a laptop or a second location) if that risk matters to you.

### External monitoring (heartbeats + uptime)

The app cannot report its own death — every alert it sends travels through its
own Gmail token — so monitoring is external (sustainability audit 2026-07-23):

- **Uptime** (UptimeRobot): checks `https://arthur.example.com/health` (and
  the wedding domain) from outside; catches app/host/Caddy/TLS failures.
- **Heartbeats** (healthchecks.io): jobs ping a unique URL only on **success**;
  the monitor alerts when pings stop, via its own email/push. Four checks:

  | Check | Pinged by | Expected cadence |
  |---|---|---|
  | poller | every completed poll cycle | 5 min (alert after ~15–20 min silence) |
  | credential-sentinel | daily `verify_credentials` job, all checks passing | daily |
  | docusign-keepalive | weekly keep-alive success | weekly (Mon 03:30 ET) |
  | nightly-backup | the backup crontab line, after local + off-site backup succeed | daily |

  The ping URLs live in `.env` (`HEALTHCHECKS_PING_URL_*`); empty = pings are
  silently skipped, so the app runs fine before the monitor is configured.
  After editing `.env` on the VPS, `docker compose up -d --force-recreate app`.

Related in-app monitoring: `verify_credentials` (07:00 ET) actively probes all
Google tokens + the dashboard OAuth client + Seam daily, read-only;
`check_classifier_drift` (Sun 09:00 ET) emails a weekly review digest of
platform-domain emails that fell to OTHER (possible email-format drift — read
it); `send_monthly_status_email` (1st, 08:00 ET) sends a systems-normal report
whose **absence** is itself an alarm.

### Off-site backups (Cloudflare R2)

`scripts/offsite_backup.sh` (chained after `backup.sh` in the crontab)
gpg-encrypts and uploads the newest DB dump, PDF tarball, `.env`, and
`config.yaml` to a private R2 bucket, verifies the upload, and prunes each
file type to the newest `OFFSITE_RETENTION_COUNT` (default 25) versions.
Config lives in `.env` (`R2_*`, `BACKUP_ENCRYPTION_PASSPHRASE`); requires
`rclone` and `gpg` on the host. **Keep the passphrase in your password
manager** — without it the off-site copies are unreadable.

Restore a file:

```bash
rclone lsf r2off:$R2_BUCKET          # (or use the Cloudflare dashboard)
gpg -d --passphrase "$BACKUP_ENCRYPTION_PASSPHRASE" --batch db_YYYY….sql.gpg > restore.sql
```

Full-host recovery = README migration procedure, sourcing the four newest
objects from the bucket instead of the dead VPS.

### ⚠️ This host is multi-tenant (shared Caddy)

`the-vps` also serves the **wedding website** (`wedding.example.com`) through *this* project's Caddy, which is the shared front proxy for the host. Before any `docker compose` here:

- Prefer **`docker compose up -d --build app`** (app only) — recreating `caddy` briefly drops the wedding site too.
- `docker compose up` **fails if the external `wedding_default` network is absent** — start the wedding stack first (`cd ~/Documents/workspace/wedding-website && docker compose up -d`); never remove the network reference.
- **Never edit `/home/jack/caddy-sites/*`** (owned by the wedding deploy pipeline) and **never rename** this compose project or the `caddy` service (the wedding deploy reloads it by container name).

### Editing `config.yaml` on a running stack

`config.yaml` is a **bind-mounted single file**. Editors that write via atomic rename (`sed -i`, most editors) change the file's inode, and the running container stays pinned to the old one — so a plain `docker compose restart` will **not** pick up the change. After editing `config.yaml`, run **`docker compose up -d --force-recreate app`**.

## Quick Start (Local Development)

```bash
cp .env.template .env          # fill in all credential values
cp config.example.yaml config.yaml   # fill in property-specific settings
docker compose up
```

See `CLAUDE.md` for full development guidelines, TDD requirements, and the tech stack reference.

## Further Reading

- `CLAUDE.md` — development conventions, TDD requirements, tech stack, domain rules
- `CONTEXT.md` — domain glossary and task graph
- `docs/implementation-plan.md` — phase-by-phase build plan
- `GETTING-TO-PRODUCTION.md` — the 22-step build-to-production runbook and its full Session Log: every bug found, decision made, and incident hit on the way to a live service
- `TESTING.md` — suite layout, offline-vs-live selection, and the E2E go-live gate
- `docs/risk-register.md` — every documented risk with its final disposition
