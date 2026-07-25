#!/usr/bin/env bash
set -euo pipefail

# Off-site backup to Cloudflare R2 (sustainability audit 2026-07-23, item 5).
#
# Runs on the VPS, chained after scripts/backup.sh in the nightly crontab.
# Encrypts (gpg symmetric, AES256) and uploads four things:
#   - the newest backup_*.sql        (database dump — guest PII)
#   - the newest pdfs_*.tar.gz       (signed DocuSign forms — guest PII)
#   - .env                           (every production credential)
#   - config.yaml                    (property config incl. HOA email)
# then prunes the bucket to the newest OFFSITE_RETENTION_COUNT versions per
# file type. Client-side encryption means a leaked R2 token or misconfigured
# bucket exposes only ciphertext; the passphrase lives in .env AND in the
# owner's password manager (without it the off-site copies are unreadable).
#
# Required env (from .env, sourced by the crontab line):
#   R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET,
#   BACKUP_ENCRYPTION_PASSPHRASE
# Optional: OFFSITE_RETENTION_COUNT (default 25), BACKUP_DIR (default .)
#
# Requires: rclone, gpg. Restore procedure: docs/operations.md "Off-site backups".

BACKUP_DIR="${BACKUP_DIR:-.}"
RETENTION_COUNT="${OFFSITE_RETENTION_COUNT:-25}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for var in R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET \
           BACKUP_ENCRYPTION_PASSPHRASE; do
  if [ -z "${!var:-}" ]; then
    echo "offsite_backup: $var is not set — off-site backup not configured" >&2
    exit 1
  fi
done

command -v rclone >/dev/null || { echo "offsite_backup: rclone not installed" >&2; exit 1; }
command -v gpg    >/dev/null || { echo "offsite_backup: gpg not installed" >&2; exit 1; }

# rclone remote defined entirely via environment — no rclone.conf needed.
export RCLONE_CONFIG_R2OFF_TYPE=s3
export RCLONE_CONFIG_R2OFF_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2OFF_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2OFF_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2OFF_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
# R2 answers 501 Not Implemented to the post-upload HEAD request that older
# rclone builds (Ubuntu ships 1.60) issue after each PUT. The upload itself
# succeeds; skip that HEAD — this script does its own verification below by
# listing the bucket and checking every object landed.
export RCLONE_CONFIG_R2OFF_NO_HEAD=true
REMOTE="r2off:${R2_BUCKET}"

latest() { ls -1t "$BACKUP_DIR"/$1 2>/dev/null | head -1; }
DB_FILE="$(latest 'backup_*.sql')"
PDF_FILE="$(latest 'pdfs_*.tar.gz')"
if [ -z "$DB_FILE" ] || [ -z "$PDF_FILE" ]; then
  echo "offsite_backup: no local backup files found in $BACKUP_DIR" >&2
  exit 1
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
umask 077

encrypt() { # encrypt <src> <dest-name>
  gpg --batch --yes --symmetric --cipher-algo AES256 \
      --passphrase "$BACKUP_ENCRYPTION_PASSPHRASE" \
      -o "$STAGE/$2" "$1"
}

encrypt "$DB_FILE"              "db_${TIMESTAMP}.sql.gpg"
encrypt "$PDF_FILE"             "pdfs_${TIMESTAMP}.tar.gz.gpg"
encrypt "$REPO_DIR/.env"        "env_${TIMESTAMP}.gpg"
encrypt "$REPO_DIR/config.yaml" "config_${TIMESTAMP}.yaml.gpg"

rclone copy "$STAGE" "$REMOTE" --s3-no-check-bucket

# Verify all four objects landed before pruning anything.
UPLOADED="$(rclone lsf "$REMOTE")"
for f in "$STAGE"/*; do
  name="$(basename "$f")"
  if ! grep -qx "$name" <<< "$UPLOADED"; then
    echo "offsite_backup: verification FAILED — $name missing from bucket" >&2
    exit 1
  fi
done
echo "offsite_backup: uploaded 4 encrypted files (stamp $TIMESTAMP)"

# Prune each file type to the newest RETENTION_COUNT versions. Timestamped
# names sort lexicographically == chronologically.
for prefix in db_ pdfs_ env_ config_; do
  rclone lsf "$REMOTE" --include "${prefix}*" | sort | head -n -"$RETENTION_COUNT" | \
  while read -r old; do
    [ -n "$old" ] && rclone deletefile "$REMOTE/$old" && echo "offsite_backup: pruned $old"
  done
done

echo "offsite_backup: done (retention ${RETENTION_COUNT} per file type)"
