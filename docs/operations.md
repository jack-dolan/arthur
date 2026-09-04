# Operations

How the service is deployed, backed up, monitored, and moved to another host.
The README covers what the system *is*; this page covers running it.

Two deployment topologies are supported, and they differ enough that the wrong
instructions are worse than none. **This page documents the image topology,
which is what production runs.** Where the Compose topology differs, it says so;
the Portability section of the repo conventions doc is the short statement of
both.

---

## Deployment topology

Production runs on a single VPS under a **deploy platform** (Dokploy), not as a
hand-managed Compose project. The distinction matters for almost every command
below.

| Piece | What it is |
|---|---|
| app | a platform *Application* whose source is a **registry image**, pinned to an explicit `sha-` tag |
| database | a platform-managed **PostgreSQL service** on the shared overlay network |
| ingress | the platform's **Traefik**, which owns ports 80 and 443 |
| public path | an **outbound-only Cloudflare tunnel**; the box accepts no inbound web connections at all |

There is **no repository checkout that the app runs from, no compose file, and
no `.env` file that the app reads.** The host holds an operational clone of this
repository, but that exists for backup and audit scripts, not for serving.

Consequences worth stating plainly, because each one has cost time:

- **`docker compose` commands do not apply.** There is no compose project to
  `exec` into, `build`, or `up`. Anything in an older runbook that begins
  `docker compose` is describing the other topology.
- **TLS is terminated at Cloudflare's edge.** The origin leg is plain HTTP and
  never leaves the machine. There is no Caddy, no certificate on the box, and no
  ACME challenge to worry about.
- **The environment lives in the platform's own store**, encrypted at rest by
  the platform. A copy is mirrored to a file on the host for the backup and
  migration scripts to read; see "Editing the environment".

`docker-entrypoint.sh` never migrates during app boot. It runs a read-only
schema guard before uvicorn: exact head starts, a revision unknown to the build
warns and starts (the expected rollback case), and a known older revision
refuses with the pending revision ids.

On startup the lifespan handler runs a credential guard: it refuses to boot if
any required credential is missing, if `SECRET_KEY` is still the placeholder,
or if `DATABASE_URL` still contains `change_me`, naming the offending field in
the error. A service that runs unattended should fail loudly at boot rather
than quietly at 3 a.m.

### Deploying a change

**Push to `main`. That is the whole procedure.** There is no manual deploy step
and no SSH to perform by hand.

`.github/workflows/build.yml` then does the following, in this order:

1. Builds the image **once** into the runner's local Docker daemon, scans it,
   and smoke-tests it. Only then does it push to the registry, so the bits that
   passed the gate are the bits that get published.
2. Joins the tailnet as an ephemeral, tagged node. The production host accepts
   no inbound SSH from the internet.
3. Fast-forwards the host's ops clone and reinstalls the four helper scripts it
   owns into `~/bin`. This is what keeps "the deployed script" and "the
   committed script" the same thing. It refuses to continue if that clone has
   local edits.
4. **Runs the explicit migration**, from the new image, before the new image
   starts as the application.
5. Points the Application at the new `sha-` tag and triggers the deploy.
6. Verifies three independent facts: a **new** container is running, it is on
   the expected image, and it logged its schema guard agreeing with the
   database. Then checks that the public URL answers `200` from outside.

**Why the migration runs first, not after.** Every revision leaves the schema
usable by the previous release too (N-1 compatibility, see `the repo conventions doc`). So the
currently-running old container keeps serving correctly against the new schema
while the deploy proceeds. That ordering is also what makes rollback complete:
redeploying the older image is the entire rollback, with no database action.
Deploying first is not an alternative, because the new container's boot guard
would see a database behind its build head and refuse to start.

**Rolling back** is editing the Application's image tag to an older `sha-` tag
in the platform panel and redeploying. The retention workflow keeps the newest
10, which is the rollback depth. The database is never restored as part of a
code rollback; that would discard newer writes. The `deploying-to-vps` skill has
the full procedure.

### Editing `config.yaml`

`config.yaml` is **not** in the repository and is not baked into the image. The
platform stores it as a mounted file and bind-mounts it into the container at
`/app/config.yaml`.

Edit it in the platform panel, under the Application's file mounts.

**A config change takes effect immediately — no redeploy.** This is the
opposite of the environment (below), and the difference is worth knowing at 2
a.m. `load_config()` re-reads the file on every call rather than caching it, and
the platform writes the file in place, so the running container sees the new
content on its next use. Measured 2026-08-02: the file was edited at 15:46:57
against a container that had started at 15:35:05, and that same container read
the new value.

The one way this bites: a single-file bind mount pins the container to the
file's **inode**, so anything that replaces the file by atomic rename (`sed -i`,
most editors) leaves the container reading the old content forever. The panel
writes in place and is safe. A hand edit on the host is not. If you ever do
edit on the host, redeploy afterwards and verify rather than assume — and
remember the platform rewrites that file from its own store on the next deploy,
so a host edit is temporary in the worst way.

To verify a config change actually landed, ask the running container rather than
reading the file:

```bash
app=$(docker ps --format '{{.ID}} {{.Image}}' | grep -m1 home-rental-automation | cut -d' ' -f1)
docker exec "$app" python -c "
from app.config import load_config
print(load_config('/app/config.yaml').properties[0])"
```

**Keep the host mirror in step.** The nightly backup reads its own copy from
`~/.secrets/`, not from the running container. A `config.yaml` change made only
in the panel is live but **unbacked-up**, and the gap is silent. Update both:

```bash
install -m 600 /etc/dokploy/applications/<app>/files/config.yaml \
  "$HOME/.secrets/<the config mirror>"
```

### Editing the environment

Credentials and settings live in the platform's environment store, edited in the
panel. **A change takes effect on the next deploy**, not on save: the container's
environment is fixed at creation. Redeploy the Application to apply it.

The same "keep the host mirror in step" warning applies. The mirror at
`~/.secrets/` is what the backup script archives and what the migration script
reads `DATABASE_URL` from. A credential rotated only in the panel will work in
production and be missing from every subsequent backup.

---

## Sandbox vs production

Two integrations have a sandbox/production distinction, both selected by the
environment:

- **DocuSign**: `DOCUSIGN_SANDBOX=true` targets the demo hosts; `false` targets
  production. In production, DocuSign is **multi-region**:
  `DOCUSIGN_API_BASE_URI` must be this account's own REST base from OAuth
  userinfo (e.g. `https://na4.docusign.net/restapi`). A global host like
  `www.docusign.net` will fail for a non-www account. The five `DOCUSIGN_*`
  credentials, the Connect **HMAC key**, and `config.yaml`'s
  `docusign_template_id` are all account-scoped: production values differ from
  sandbox and do **not** transfer at go-live (redirect URIs and secrets must be
  re-added in the production Apps & Keys).
- **Seam**: the API key *is* the environment. The production key points at the
  production workspace and the real lock (`seam_device_id` in `config.yaml`).

**Confirm which environment is live.** Every boot logs it:

```bash
app=$(docker ps --format '{{.ID}} {{.Image}}' | grep -m1 home-rental-automation | cut -d' ' -f1)
docker logs "$app" 2>&1 | grep "DocuSign target"
# PRODUCTION → "... PRODUCTION (real envelopes, real money) | oauth=account.docusign.com | api=https://na4.docusign.net/restapi"
# SANDBOX    → "... SANDBOX (demo tier) | oauth=account-d.docusign.com | api=https://demo.docusign.net/restapi"
```

### Reverting to sandbox

1. In the platform's environment store: restore the five sandbox `DOCUSIGN_*`
   values, the sandbox `DOCUSIGN_HMAC_KEY` and the sandbox `SEAM_API_KEY`; set
   `DOCUSIGN_SANDBOX=true`; clear `DOCUSIGN_API_BASE_URI=`.
2. In `config.yaml`: restore the sandbox `docusign_template_id` and
   `seam_device_id`.
3. Redeploy the Application, then confirm the banner reads **SANDBOX**.
4. Update the `~/.secrets/` mirrors to match.

(The swap is symmetric: going live is the same steps in reverse.) Note that a
sandbox credential set is not recorded anywhere off-host, so rolling back
requires re-minting sandbox credentials from scratch.

---

## DocuSign token upkeep

The production **refresh token expires 30 days after its last use**. The weekly
`refresh_docusign_token` keep-alive job resets that clock (a token-only
exchange, no envelope, no cost), and every rotation is persisted to
**`/app/data/docusign_refresh_token`** on the PDF volume, so rotations **survive
container restarts**. The exchange prefers that stored token over the (possibly
stale) environment one and falls back automatically if the stored token is ever
rejected. A failed keep-alive **emails the owner** (weekly at most).

That the rotated token lives on the data volume rather than in the environment
is the single most load-bearing detail in this file for a host migration. A
migration that copies "the PDFs" instead of the whole volume silently breaks
DocuSign authentication, and the failure surfaces weeks later.

Re-minting is therefore only needed if the keep-alive alert fires repeatedly,
the data volume is lost, or the stack is down for more than 30 days. Run it from
the ops clone on the host, forwarding the callback port to your laptop:

```bash
# The redirect URI http://localhost:8765/callback must be registered in the
# PRODUCTION Apps & Keys.
ssh -t -L 8765:localhost:8765 <user>@<host> \
  'cd ~/workspace/home-rental-automation && python3 scripts/manual/get_docusign_refresh_token.py production'
```

Then put the new token in the platform's environment store **and** the
`~/.secrets/` mirror, and redeploy the Application.

---

## Backups

Two jobs, two buckets, two heartbeats, sharing only the encryption passphrase.
That split is deliberate: one alarm names one system, and one leaked storage
token reaches one archive.

| Script | Backs up | Bucket |
|---|---|---|
| `scripts/backup_rental_automation.sh` | **this app**: database, the whole data volume, the environment mirror, `config.yaml` | the app's bucket |
| `scripts/backup_dokploy_platform.sh` | **the platform**: its own database, its config directory, the swarm secrets, and the backup job's own credentials | the platform's bucket |

Both run nightly from the host crontab, each chaining its own heartbeat ping
with `&&` so the ping fires only after the backup *and* the upload both
succeeded. Silence therefore names the job that failed.

The scripts live in this repository and are installed to `~/bin` **by the deploy
pipeline** (see "Deploying a change", step 3). Do not edit the copies in `~/bin`;
they are overwritten on every deploy. Edit the repository copies and push.

### What the app backup captures, and why each piece

- **The database**, via `pg_dump` run from a `postgres` image pinned to the same
  major version as the server, so the dump is never produced by an older client
  than the database it read. Plain SQL, no `-Fc`, so it restores with `psql` and
  stays greppable.
- **The whole data volume**, not just `pdfs/`. The rotated DocuSign refresh
  token sits at its root. See the warning above.
- **The environment mirror**, which is every production credential.
- **`config.yaml`**, which holds the property configuration.

Everything is gpg-encrypted (AES256) **before it leaves the host**, so a leaked
storage token exposes only ciphertext. **Keep the passphrase in a password
manager.** Without that second copy the off-site archives are unreadable, which
is the entire point of storing it there.

`rclone` is deliberately not installed on the host; it runs from its official
image, pinned. Installing it would need root, and the backup path should not
require privilege it does not otherwise have.

### The platform backup is not optional

The platform's own database and config directory hold **both applications'
environment stores**. The swarm auth secret is what derives the platform's
environment-encryption key, so a restore without it recovers only ciphertext.
Backing up the app but not the platform means recovering the data and losing
every credential needed to use it.

---

## Restore

Restoring has been **proven against production data**, not merely written down.
Prefer proving a restore over trusting one: a backup nobody has restored from is
a claim, not a capability.

1. **Fetch and decrypt.** List the bucket with `rclone`, pull the generation you
   want, and decrypt with the passphrase from the password manager:

   ```bash
   gpg -d --batch --passphrase "$BACKUP_ENCRYPTION_PASSPHRASE" \
     db_YYYYMMDD_HHMMSS.sql.gpg > restore.sql
   ```

2. **Restore the database** into the managed PostgreSQL service, over the shared
   overlay network:

   ```bash
   docker run --rm -i --network "$DOCKER_NET" -e PGPASSWORD "$PG_IMAGE" \
     psql -h "$PG_HOST" -U "$PG_USER" -d "$PG_DB" < restore.sql
   ```

   `psql`, not `pg_restore`: plain SQL format and custom format are not
   interchangeable.

3. **Restore the data volume** whole:

   ```bash
   docker run --rm -i -v "$PDF_VOLUME":/data alpine tar xzf - -C /data < pdfs_YYYYMMDD_HHMMSS.tar.gz
   ```

The service identifiers (`PG_HOST`, `PG_USER`, `PG_DB`, `PDF_VOLUME`,
`DOCKER_NET`, `PG_IMAGE`) are the same overridable variables the backup script
declares at the top; read them from there rather than retyping them, since a
platform rebuild can rename a service.

**Restore is destructive on conflicting rows.** Restoring into a non-empty
database may fail on duplicate key violations or produce duplicate data. For a
clean restore, drop and recreate the target database first, or target a fresh
service. The dump sets object ownership via `OWNER TO` but does not `CREATE
ROLE`, so the target database's owning role must already exist.

The volume restore is **additive**, which is right for disaster recovery into an
empty volume. Check for conflicts first if the volume already has content.

---

## Moving to another host

Portability is a design requirement, and the migration has been performed for
real rather than only described. **Establish which topology you are on first**;
the steps differ. The `deploying-to-vps` skill opens with that check.

### Image topology (what production runs)

1. `pg_dump` on the old host.
2. Stand up the platform, a PostgreSQL service, and the app's data volume on the
   new host.
3. Restore the dump, and copy the data volume contents across **whole**. The
   rotated DocuSign refresh token lives beside the signed PDFs and is not in the
   environment. A "copy the PDFs" reading of this step silently breaks DocuSign
   authentication.
4. Load the environment and `config.yaml` into the platform's own stores.
5. Run the image's explicit `migrate` command.
6. Start the app, then point DNS at the new host.

### Compose topology

1. `pg_dump` on the old host.
2. Copy the repository, `.env` and `config.yaml` to the new host.
3. Start PostgreSQL and restore.
4. Run the image's explicit `migrate` command.
5. Start the app and the proxy.

### Either way

**Stop the old app before starting the new one.** The app owns a shared booking
inbox and a physical lock. Two instances double-send envelopes and email.

The app refuses to start if any required credential is missing, and names the
missing field. Resolve credential gaps before proceeding.

### What is not migrated, and does not need to be

- **APScheduler state**: jobs are registered in memory at startup and
  re-register on each start. Nothing to migrate.
- **OAuth refresh tokens**: they are in the environment, which step 4 carries,
  except the DocuSign one noted above.

---

## Monitoring

The app cannot report its own death, because every alert it sends travels
through its own Gmail token. So the outermost layer of monitoring is external.

- **Uptime** (UptimeRobot): checks `/health` from outside the host; catches app,
  host, ingress and TLS failures.
- **Heartbeats** (healthchecks.io): jobs ping a unique URL only on **success**,
  and the monitor alerts when pings stop, through its own channel:

  | Check | Pinged by | Expected cadence |
  |---|---|---|
  | poller | every completed poll cycle | 5 min (alert after 15 to 20 min of silence) |
  | credential-sentinel | daily `verify_credentials`, all checks passing | daily |
  | docusign-keepalive | weekly keep-alive success | weekly (Mon 03:30 ET) |
  | app backup | the app backup crontab, after archive + upload succeed | daily |
  | platform backup | the platform backup crontab, same rule | daily |
  | health audit | the weekly audit, after the report is generated **and** sent | weekly |

  The app's own ping URLs (`HEALTHCHECKS_PING_URL_*`) live in the platform's
  environment store; an empty value means pings are silently skipped, so the app
  runs fine before the monitor is configured. **Editing one takes effect on the
  next deploy**, like any other environment change. The three cron jobs' ping
  URLs are not app settings at all: they live in the per-job credential files
  under `~/.secrets/` on the host and take effect on the next run.

In-app monitoring jobs complement this:

| Job | Schedule | What it proves |
|---|---|---|
| `verify_credentials` | daily 07:00 ET | every integration credential still works (read-only probes) |
| `requeue_stalled_automations` | daily 08:30 ET | FAILED tasks are retried; orphaned PENDING tasks are re-dispatched; a digest goes to the owner |
| `verify_access_codes` | daily 09:00 ET | door codes for upcoming stays actually exist on the lock (Seam programs devices asynchronously) |
| `check_classifier_drift` | Sun 09:00 ET | platform emails falling through to OTHER get a human-review digest, which is the silent failure mode when a platform changes its email format |
| `review_dead_letters` | Sun 10:00 ET | an LLM second opinion on the same dead letters, an hour after the digest above; alert-only, and silent on a clean week |
| `send_monthly_status_email` | 1st, 08:00 ET | the alert send path works end to end; the report's **absence** is itself an alarm |

---

## The weekly health audit

Everything above is deterministic: it fires when a number crosses a threshold or
a ping stops arriving. None of it **interprets**. This job does. Once a week it
runs the same `checking-production-health` skill a human session would, and
emails the interpreted report.

```
Mondays 11:15 UTC → host crontab → scripts/weekly_health_audit.sh
  → claude -p (headless, in the repo clone, read-only)
  → the report body piped into the app container
  → python -m app.send_operator_report → the alerts Gmail account
  → on success only: a healthchecks.io ping
```

Four things about it are load-bearing:

- **It needs a repo clone on the host.** The skill *is* files in the repo, and
  Claude Code reads skills from its working directory. On the image topology the
  running app has no checkout, so this clone exists purely for operational work.
  The script fast-forwards it before each run and records the commit it actually
  ran in the email footer, so a clone that stops updating is visible rather than
  silent. It treats a failed fast-forward as a **warning** and runs anyway,
  because losing a week of monitoring to a transient git problem is the worse
  trade. The deploy pipeline fast-forwards the same clone and treats failure as
  **fatal**, which is the right call there. Different jobs, different answer.
- **The host holds no mail credential.** The report is piped into the running app
  container and sent by `python -m app.send_operator_report`, which reuses the
  alerts Gmail account the app already has. The host never gets a second copy.
- **The healthchecks.io ping is chained behind the whole thing with `&&`.** Any
  failure, whether a timeout, an empty report or an undeliverable email,
  withholds the ping, and the check alerts after 8 days of silence. A dead
  auditor is loud.
- **The report leads with a one-line verdict** (`healthy` / `needs attention` /
  `broken`) so the subject line carries it and the email can be triaged without
  being opened.

Its credentials live in a per-app file under `~/.secrets/` on the host (mode
600): a long-lived Claude Code token from `claude setup-token`, and this job's
healthchecks.io ping URL. **The file is per-app on purpose.** A second app on
the same host gets its own token and its own check, so revoking one leaves the
other running.

To run it by hand, exactly as cron does:

```bash
set -a; . "$HOME/.secrets/<the audit credential file>"; set +a
bash "$HOME/bin/weekly_health_audit.sh"
```

Knobs, all environment variables with sane defaults: `AUDIT_WINDOW_HOURS` (168),
`AUDIT_MODEL` (`sonnet`), `AUDIT_TIMEOUT` (900 seconds, a hard ceiling enforced
by `timeout`), `AUDIT_REPO_DIR`.

**A note on the schedule.** The host runs UTC and the crontab is a fixed UTC
time, so the job lands at 07:15 ET in summer and 06:15 ET in winter. Both are
morning; nothing depends on the exact minute.

---

## Recovering a broken credential

Credential expiry is the most common failure mode in a system built on four
OAuth-shaped integrations. Symptoms (`invalid_grant`, 401s, a sentinel alert, a
DocuSign keep-alive failure email) and the repair procedure for each integration
are documented in the `recovering-credentials` skill under `.claude/skills/`.
