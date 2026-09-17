#!/usr/bin/env bash
# Nightly pg_dump of the fleet database, gzip'd, keeping the last N days.
# Run from cron on the host, from the dashboard directory:
#   0 3 * * * cd /opt/fleet/dashboard && ./scripts/backup.sh >> backups/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/fleet-$(date -u +%Y%m%d-%H%M%S).sql.gz"
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-fleet}" -d fleet | gzip > "$out"
find "$BACKUP_DIR" -name 'fleet-*.sql.gz' -mtime "+$KEEP_DAYS" -delete
echo "$(date -Is) $out $(du -h "$out" | cut -f1)"
