---
name: deploying-to-vps
description: Pushes local commits to GitHub and gracefully redeploys the rental-automation app on the production VPS, with pre-flight checks, migration backups, an explicit migration step, an app-only restart, post-deploy verification, and database-untouched rollback. Use when the user asks to deploy, redeploy, ship changes, update production, roll back, or get the VPS to pick up pushed changes.
---

# Deploying to the VPS

This is a **low-freedom procedure**: production controls a real door lock and
sends real email, and this project's `caddy` service is the shared front proxy
for a **second tenant** (the wedding-website stack) on the same host. Follow
the steps in order; do not improvise around a failed check.

All host-specific values come from the local `.env` (never hardcode them):

```bash
VPS=$(grep '^DEPLOY_VPS_SSH=' .env | cut -d= -f2-)
DIR=$(grep '^DEPLOY_VPS_REPO_DIR=' .env | cut -d= -f2-)
```

If either is empty, ask the user for it once and offer to append it to `.env`
(and mirror the key, valueless, in `.env.template`).

## Checklist

Copy this into your response and check items off as you go:

```
Deploy progress:
- [ ] 1. Local pre-flight (suite green, tree clean, pushed)
- [ ] 2. VPS pre-flight (tree clean, second-tenant network up)
- [ ] 3. Backup if the pull includes a migration
- [ ] 4. Pull + build + explicit migration + app-only restart
- [ ] 5. Verify (schema guard, boot banner, health, tenants, log watch)
- [ ] 6. Session Log closeout
```

## Step 1 — Local pre-flight

```bash
make test                      # offline suite must be green
git status --porcelain         # must be empty
git push                       # main must be on origin
```

Stop on any failure. Never deploy a red suite or unpushed work.

## Step 2 — VPS pre-flight

```bash
ssh "$VPS" "cd $DIR && git status --porcelain && git log --oneline -1"
ssh "$VPS" "docker network inspect wedding_default --format up 2>/dev/null || echo MISSING"
ssh "$VPS" "docker ps --format '{{.Names}}' | grep caddy"
```

- **Dirty working tree on the VPS → STOP.** Someone changed files in place
  (it has happened). Reconcile first: commit on the VPS and pull locally, or
  get explicit approval to discard. Never `git checkout .` blindly.
- **`wedding_default` MISSING → STOP.** `docker compose up` here fails
  without it. Fix by starting the second tenant's stack, **never** by
  removing the network reference from this compose file.
- Caddy container must be named `home-rental-automation-caddy-1` — the second
  tenant's deploy pipeline reloads it by that exact name. If it differs,
  stop and investigate before touching anything.

## Step 3 — Backup when a migration ships

Check whether the incoming range touches migrations:

```bash
ssh "$VPS" "cd $DIR && git fetch -q && git diff --name-only HEAD..origin/main -- alembic/versions/"
```

Any output → take a DB backup first (backups also contain guest PII — leave
them on the VPS, mode 600, never commit). This is disaster recovery, not the
rollback path:

```bash
ssh "$VPS" "cd $DIR && set -a && . ./.env && set +a && bash scripts/backup.sh"
```

## Step 4 — Pull + build + migrate + restart

```bash
ssh "$VPS" "cd $DIR && git pull --ff-only"
ssh "$VPS" "cd $DIR && docker compose build app"
ssh "$VPS" "cd $DIR && docker compose run --rm --no-deps app migrate"
ssh "$VPS" "cd $DIR && docker compose up -d --no-deps --no-build app"
```

- The migration command is idempotent and runs on every deploy. It runs from
  the newly built image **before** that image starts as the app. The old app
  remains live against the expanded schema during this step; N-1 compatibility
  makes that safe.
- **Always app-only.** A bare `up -d --build` recreates
  `caddy` and drops the second tenant's site during the restart.
- Only rebuild beyond `app` when `docker-compose.yml` or `Caddyfile` changed
  in the pulled range — and then tell the user the other tenant will blip
  and get an explicit go-ahead first.

## Step 5 — Verify (do not skip; do not stop at "it started")

```bash
ssh "$VPS" "cd $DIR && docker compose logs app --since 3m"
```

Confirm, in the logs:
1. `SCHEMA GUARD: database revision == build head (...); starting application`
   printed. During an intentional rollback, the prominent
   `UNKNOWN TO THIS BUILD` warning is the expected start path instead.
2. The credential guard passed and `Application startup complete` printed.
3. The DocuSign banner line says `PRODUCTION` with the expected api host —
   this is the definitive sandbox/production check.

Then from the **outside** (rendered reality, not container introspection):

```bash
DOMAIN=$(ssh "$VPS" "cd $DIR && sed -n 's/^DOMAIN=//p' .env")
test -n "$DOMAIN"  # use the deployed value; local .env may say localhost
curl -s -o /dev/null -w '%{http_code}\n' "https://$DOMAIN/health"   # expect 200
ssh "$VPS" "docker ps --format '{{.Names}} {{.Status}}'"            # all Up, both tenants
```

Finally watch `docker compose logs app --since 2m -f` for ~2 minutes for an
error burst (a bad deploy usually screams immediately: repeated tracebacks,
poller auth failures). One-off bot-probe 404s in caddy logs are normal.

Run the explicit command once more after verification when proving a migration
change; it must be a clean no-op:

```bash
ssh "$VPS" "cd $DIR && docker compose run --rm --no-deps app migrate"
```

## Rollback

```bash
ssh "$VPS" "cd $DIR && git reset --hard <last-good-sha> && docker compose build app && docker compose up -d --no-deps --no-build app"
```

Do **not** run the migration command during rollback and do **not** restore a
dump. The database stays untouched. If the newer release applied a revision,
the old image's schema guard sees that unknown revision, warns, and starts
under the N-1 guarantee; otherwise it sees its own head and starts normally.
Restoring a dump would discard every write made since that dump; backups are
disaster recovery only. Rows whose meaning exists only in the newer release
render as `unknown` until roll-forward. Re-run all of Step 5 after rollback.

## Step 6 — Closeout

Append a dated entry to the Session Log in `GETTING-TO-PRODUCTION.md`: what
deployed (sha range), backup taken or not, verification results, anything
unusual. Commit and push that entry. **Never put guest names, addresses, or
other PII in the entry** — the Session Log has leaked PII before.
