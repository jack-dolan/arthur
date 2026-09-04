---
name: deploying-to-vps
description: Deploys the rental-automation app to production and rolls it back, on either supported topology - a platform host that runs a prebuilt image (push to main is the deploy) or a Docker Compose host that builds on the box. Covers pre-flight checks, the explicit migration step, post-deploy verification, and database-untouched rollback. Use when the user asks to deploy, redeploy, ship changes, update production, roll back, or get the host to pick up pushed changes.
---

# Deploying to the VPS

This is a **low-freedom procedure**: production controls a real door lock and
sends real email. Follow the steps in order; do not improvise around a failed
check.

**The single most important invariant: exactly one application instance runs at
a time.** The app polls a shared booking inbox and drives a physical lock. Two
concurrent instances double-send DocuSign envelopes, double-send owner email and
fight over lock codes. Every step below preserves that, and the most common way
to break it is starting a container on one host while another host still serves.

Host-specific values come from the local `.env` — never hardcode them:

```bash
VPS=$(grep '^DEPLOY_VPS_SSH=' .env | cut -d= -f2-)
DIR=$(grep '^DEPLOY_VPS_REPO_DIR=' .env | cut -d= -f2-)   # Compose topology only
```

If `DEPLOY_VPS_SSH` is empty, ask the user for it once and offer to append it to
`.env` (mirroring the key, valueless, in `.env.template`).

## Step 0 — Establish the topology. Do not skip this.

Two topologies are supported and the procedures share almost nothing. Find out
which one you are on before touching anything:

```bash
ssh "$VPS" "docker ps --format '{{.Names}}\t{{.Image}}'"
```

- **Image/platform topology** — you see containers whose image is
  `ghcr.io/<owner>/home-rental-automation:sha-<short-sha>`, alongside a reverse
  proxy and a platform container that belong to the *host* rather than to this
  project. There is **no repository checkout and no compose file** here. Go to
  **Path A**.
- **Compose topology** — you see this project's own compose containers (`app`,
  `db`, `caddy`) built on the box from a checkout at `$DIR`. Go to **Path B**.

**Never run `docker compose up` on a platform host.** There is no compose
project there, so it would start a *second* application instance from a stale
checkout against the same booking inbox — the exact failure the invariant above
exists to prevent.

---

# Path A — image/platform topology

Here a deploy is a **pipeline**, not a command. The normal path involves no SSH
at all.

```
Deploy progress (Path A):
- [ ] 1. Local pre-flight (suite green, tree clean)
- [ ] 2. Push to main — that IS the deploy
- [ ] 3. Watch the run: build → scan → publish → migrate → deploy → verify
- [ ] 4. Independent verification
- [ ] 5. Session Log closeout
```

## A1 — Local pre-flight

```bash
make test                      # offline suite must be green
git status --porcelain         # must be empty
```

Stop on any failure. Never deploy a red suite.

## A2 — Push to main

```bash
git push
```

That is the deploy. The `build` workflow, on every push to `main`:

1. builds the production image once and loads it locally,
2. checks the image installs exactly what `uv.lock` pins,
3. scans it and **blocks** on HIGH/CRITICAL findings that have a fix available,
4. smoke-tests it, including that application boot does **not** migrate,
5. pushes it to the registry as `:sha-<short-sha>` and `:latest`,
6. joins the private network, runs the **explicit migration** from the new image,
7. points the platform application at the new `sha-` tag and triggers the deploy,
8. verifies the running container is on the new image and the public URL answers.

**The order in step 6/7 is load-bearing, not incidental.** The migration runs
*before* the new image starts. Every migration leaves the schema usable by the
previous release too (N-1 compatibility), so the still-running old container
keeps serving correctly against the new schema while the deploy proceeds — and
that is precisely what makes redeploying the old image a *complete* rollback with
no database action. Deploying first is not an alternative: the new container's
startup guard would see a database behind its build head, refuse to start, and
crash-loop until the migration happened.

The migration always runs. It is idempotent and takes seconds, so there is no
"did this release contain a migration?" branch to get wrong.

**Manual deploy — two ways, both first-class:**

```bash
gh workflow run build.yml --ref main     # re-run the whole pipeline
gh run watch                             # follow it
```

or open the platform panel and press **Deploy** on the application. The panel
route redeploys whatever image tag the application is currently pointed at, so it
is a *restart*, not a way to ship new code — use it after an environment change,
and use the workflow to ship a commit.

## A3 — Watch the run

```bash
gh run watch
```

If the pipeline fails, read *which step* failed, because the blast radius differs
sharply:

| Failed step | State of production | What to do |
|---|---|---|
| build / lockfile / scan / smoke | untouched, nothing published | fix and push again |
| migration | schema may be partly advanced; **old release still serving** | this is the N-1 guarantee working. Diagnose the migration; do not deploy on top |
| point-at-image or deploy trigger | old release still serving | re-run the workflow |
| verification | new image may be up but unhealthy | go to **Rollback** below |

## A4 — Independent verification

The pipeline checks three things (running image, public health, startup guard).
Do not stop there — invoke the **`checking-production-health`** skill, which
reads logs, scheduler evidence and task state properly.

The minimum by hand, from outside the host (rendered reality, not container
introspection):

```bash
bash scripts/prod_health.sh     # expect: HEALTHY: https://<host>/health -> 200
```

It resolves the hostname by asking **production** — it reads `DOMAIN` out of the
running app container's own environment, over `DEPLOY_VPS_SSH` when run from the
dev box. Pass a hostname as `$1` to override.

**Never build this URL from the local `.env`.** That file configures local
development and its `DOMAIN` is `localhost`, so the probe hits
`https://localhost/health`, fails to connect, and prints a bare `000` — which
during a deploy reads as "production is down". That happened on 2026-07-30 and
cost time. The same variable *inside the container* is correct; reading it from
the dev box's file is what is wrong.

The three outcomes are deliberately different words, because an unreachable host
and an unhealthy app need different next moves:

| Output | Exit | Means |
|---|---|---|
| `HEALTHY … -> 200` | 0 | the deploy is verified at the edge |
| `UNHEALTHY … -> <code>` | 1 | something answered, but not 200 — this is about the app or the proxy. Go to **Rollback** |
| `UNREACHABLE …` | 2 | nothing answered at all. Say so as a *connectivity* failure — do not report it as an outage until you have checked DNS, the tunnel and your own network |

Then in the application's logs (platform panel, or `docker logs` over SSH),
confirm three lines:

1. `SCHEMA GUARD: database revision == build head (...); starting application.`
   During an intentional rollback the prominent `UNKNOWN TO THIS BUILD` warning
   is the correct start path instead.
2. `Application startup complete.`
3. The DocuSign banner names `PRODUCTION` and the expected API host — this is the
   definitive sandbox/production check.

Then watch for a couple of minutes for an error burst. A bad deploy usually
screams immediately: repeated tracebacks, poller auth failures.

---

# Path B — Docker Compose topology

The app, database and a Caddy front proxy run from a checkout on the host and are
built there. Migrations are still an explicit step, never a boot side effect.

```
Deploy progress (Path B):
- [ ] 1. Local pre-flight (suite green, tree clean, pushed)
- [ ] 2. Host pre-flight (tree clean, proxy present)
- [ ] 3. Backup if the incoming range includes a migration
- [ ] 4. Pull + build + explicit migration + app-only restart
- [ ] 5. Verify
- [ ] 6. Session Log closeout
```

## B1 — Local pre-flight

```bash
make test
git status --porcelain
git push
```

## B2 — Host pre-flight

```bash
ssh "$VPS" "cd $DIR && git status --porcelain && git log --oneline -1"
ssh "$VPS" "docker ps --format '{{.Names}} {{.Status}}'"
```

**A dirty working tree on the host → STOP.** Someone changed files in place; it
has happened. Reconcile first — commit on the host and pull locally, or get
explicit approval to discard. Never `git checkout .` blindly.

If this compose file declares any **external** network or bind-mounts a directory
owned by another service, that dependency must exist before `docker compose up`
will start anything. Check it, and fix it by satisfying the dependency — never by
deleting the reference from the compose file, which is load-bearing for whatever
put it there.

## B3 — Backup when a migration ships

```bash
ssh "$VPS" "cd $DIR && git fetch -q && git diff --name-only HEAD..origin/main -- alembic/versions/"
```

Any output → take a database backup first. Backups contain guest PII: leave them
on the host, mode 600, never commit them. **This is disaster recovery, not the
rollback path.**

```bash
ssh "$VPS" "cd $DIR && set -a && . ./.env && set +a && bash scripts/backup.sh"
```

## B4 — Pull, build, migrate, restart

```bash
ssh "$VPS" "cd $DIR && git pull --ff-only"
ssh "$VPS" "cd $DIR && docker compose build app"
ssh "$VPS" "cd $DIR && docker compose run --rm --no-deps app migrate"
ssh "$VPS" "cd $DIR && docker compose up -d --no-deps --no-build app"
```

- The migration runs from the newly built image **before** that image starts as
  the app, for the same N-1 reason given in Path A.
- **Always app-only.** A bare `up -d --build` recreates the proxy too, which
  interrupts anything else it fronts.
- Only rebuild beyond `app` when `docker-compose.yml` or `Caddyfile` changed in
  the pulled range, and say so before you do it.

## B5 — Verify

```bash
ssh "$VPS" "cd $DIR && docker compose logs app --since 3m"
```

Confirm the same three log lines listed in **A4**, then the same external probe
(`bash scripts/prod_health.sh`, same three outcomes), then invoke
**`checking-production-health`**.

Re-run the migration command once more when proving a migration change; it must
be a clean no-op:

```bash
ssh "$VPS" "cd $DIR && docker compose run --rm --no-deps app migrate"
```

---

# Rollback

**Rollback never touches the database.** Redeploying the previous release *is* a
complete rollback. That is a guarantee bought by the migration discipline, not a
hope: every migration leaves the schema usable by the immediately previous
release, so the newer schema simply sits there unused.

Do **not** restore a database dump as part of a rollback. A dump is disaster
recovery — restoring it discards every write since it was taken, which on this
system can mean a booking that exists in the inbox but not in the database.

### Path A — roll back an image tag

1. Find the previous good tag. The registry keeps the newest ~10 `sha-` tags,
   pruned by a scheduled workflow, and the successful runs of `build` list what
   they published:
   ```bash
   gh run list --workflow build.yml --status success --limit 10
   ```
2. In the platform panel, open the application, set its **Docker image** to the
   previous `...:sha-<short-sha>`, save, and press **Deploy**.
3. **Run no migration.** Take no database action of any kind.
4. Verify per **A4**. What the startup guard logs depends on whether the release
   you are leaving actually shipped a migration:
   - **It did:** the old image finds a revision it has never heard of and logs
     the prominent `UNKNOWN TO THIS BUILD` warning, then starts anyway. **That
     warning is the rollback working**, not a fault — it is the guard
     distinguishing "the database is ahead of me" (safe, start) from "the
     database is behind me" (the explicit migration was skipped, refuse to
     start).
   - **It did not:** the guard logs its ordinary `revision == build head` line,
     because both builds share a head. Equally correct, just less dramatic.

Roll forward by re-running the `build` workflow on `main`, which points the
application back at the newest tag.

### Path B — roll back a checkout

```bash
ssh "$VPS" "cd $DIR && git reset --hard <last-good-sha> && docker compose build app && docker compose up -d --no-deps --no-build app"
```

Same rules: no migration command, no dump, then verify.

### The one honest residual, on both paths

Rollback is complete at the **schema** level. It is not always complete at the
**meaning** level. If the newer release introduced a task type, status or enum
value that only its code understands, rows carrying those values render as
`unknown` under the older code until you roll forward. That is a deliberate
trade: an unfamiliar label is acceptable, a crash or an unsafe write is not.
Nothing is lost — the rows keep their values and regain their meaning on roll
forward.

---

# Closeout

Append a dated entry to the Session Log in `GETTING-TO-PRODUCTION.md`: what
deployed (sha range), whether a backup was taken, verification results, anything
unusual. Commit and push that entry.

**Never put guest names, addresses, phone numbers or booking confirmation codes
in the entry** — the Session Log has leaked PII before. Name a booking by its
external-id *shape* or just call it "the guest".
