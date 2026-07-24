---
name: deploying-to-vps
description: Pushes local commits to GitHub and gracefully redeploys the rental-automation app on the production VPS, with pre-flight checks, migration backups, an app-only rebuild, post-deploy verification, and rollback. Use when the user asks to deploy, redeploy, ship changes, update production, roll back, or get the VPS to pick up pushed changes.
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
DOMAIN=$(grep '^DOMAIN=' .env | cut -d= -f2-)
```

If any is empty, ask the user for it once and offer to append it to `.env`
(and mirror the key, valueless, in `.env.template`).

## Checklist

Copy this into your response and check items off as you go:

```
Deploy progress:
- [ ] 1. Local pre-flight (suite green, tree clean, pushed)
- [ ] 2. VPS pre-flight (tree clean, second-tenant network up)
- [ ] 3. Backup if the pull includes a migration
- [ ] 4. Pull + app-only rebuild
- [ ] 5. Verify (migrations, boot banner, health, tenants, log watch)
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
them on the VPS, mode 600, never commit):

```bash
ssh "$VPS" "cd $DIR && set -a && . ./.env && set +a && bash scripts/backup.sh"
```

## Step 4 — Pull + rebuild

```bash
ssh "$VPS" "cd $DIR && git pull --ff-only"
ssh "$VPS" "cd $DIR && docker compose up -d --build app"
```

- **Always app-only** (`--build app`). A bare `up -d --build` recreates
  `caddy` and drops the second tenant's site during the restart.
- Only rebuild beyond `app` when `docker-compose.yml` or `Caddyfile` changed
  in the pulled range — and then tell the user the other tenant will blip
  and get an explicit go-ahead first.

## Step 5 — Verify (do not skip; do not stop at "it started")

```bash
ssh "$VPS" "cd $DIR && docker compose logs app --since 3m"
```

Confirm, in the logs:
1. `alembic upgrade head` ran clean (entrypoint output; no traceback).
2. The credential guard passed and `Application startup complete` printed.
3. The DocuSign banner line says `PRODUCTION` with the expected api host —
   this is the definitive sandbox/production check.

Then from the **outside** (rendered reality, not container introspection):

```bash
curl -s -o /dev/null -w '%{http_code}\n' "https://$DOMAIN/health"   # expect 200
ssh "$VPS" "docker ps --format '{{.Names}} {{.Status}}'"            # all Up, both tenants
```

Finally watch `docker compose logs app --since 2m -f` for ~2 minutes for an
error burst (a bad deploy usually screams immediately: repeated tracebacks,
poller auth failures). One-off bot-probe 404s in caddy logs are normal.

## Rollback

```bash
ssh "$VPS" "cd $DIR && git reset --hard <last-good-sha> && docker compose up -d --build app"
```

If Step 3 took a backup and the migration is suspect, restore it with
`scripts/restore.sh` (see README "Backup/restore") **before** rolling the
code back past the migration. Re-run all of Step 5 after any rollback.

## Step 6 — Closeout

Append a dated entry to the Session Log in `GETTING-TO-PRODUCTION.md`: what
deployed (sha range), backup taken or not, verification results, anything
unusual. Commit and push that entry. **Never put guest names, addresses, or
other PII in the entry** — the Session Log has leaked PII before.
