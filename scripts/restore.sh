#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 <backup_file.sql>"
  exit 1
fi

docker compose exec -T db psql -U "${POSTGRES_USER}" "${POSTGRES_DB}" < "$1"
echo "Restore from $1 complete."
