#!/usr/bin/env bash
set -euo pipefail

# Destination directory for backup files. Defaults to the current directory,
# preserving the manual workflow documented in the README. The nightly cron
# timer (F-A, bug hunt 2026-07-22) overrides this to a dedicated directory
# outside the git working tree.
BACKUP_DIR="${BACKUP_DIR:-.}"
mkdir -p "${BACKUP_DIR}"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
DB_BACKUP_FILE="${BACKUP_DIR}/backup_${TIMESTAMP}.sql"
PDF_BACKUP_FILE="${BACKUP_DIR}/pdfs_${TIMESTAMP}.tar.gz"

# Both files contain real guest PII (bookings table / signed HOA forms).
# Create them with 0600 from the start so neither is ever briefly
# world-readable. .gitignore also excludes backup_*.sql / pdfs_*.tar.gz so
# neither can be committed.
umask 077

# -T: no pseudo-TTY. Required for cron (no controlling terminal) and harmless
# interactively; also matches restore.sh's existing `exec -T`.
docker compose exec -T db pg_dump -U "${POSTGRES_USER}" "${POSTGRES_DB}" > "${DB_BACKUP_FILE}"
chmod 600 "${DB_BACKUP_FILE}"
echo "Database backup written to ${DB_BACKUP_FILE}"

# F-A: signed DocuSign PDFs live in the pdf_data Docker volume (mounted at
# /app/data in the app container), not in Postgres — until now they were in
# no backup at all, so a volume loss would lose every signed HOA form with
# no recovery path. tar it through the running app container, the same
# docker-compose-exec approach pg_dump already uses (rather than reaching
# into Docker's internal volume storage path, which needs root and is not
# portable across hosts).
docker compose exec -T app tar czf - -C /app/data pdfs > "${PDF_BACKUP_FILE}"
chmod 600 "${PDF_BACKUP_FILE}"
echo "PDF volume backup written to ${PDF_BACKUP_FILE}"

# Retention: prune our own timestamped backups older than RETENTION_DAYS
# (default 14). Scoped to BACKUP_DIR and our two filename patterns only —
# never a bare rm -rf of the directory.
RETENTION_DAYS="${RETENTION_DAYS:-14}"
find "${BACKUP_DIR}" -maxdepth 1 -type f \
  \( -name 'backup_*.sql' -o -name 'pdfs_*.tar.gz' \) \
  -mtime "+${RETENTION_DAYS}" -print -delete

echo "Backups written to ${BACKUP_DIR} (mode 600; transfer off-host for disaster recovery beyond this VPS)"
