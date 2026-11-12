#!/usr/bin/env bash
# applai/scripts/backup-db.sh — Online backup of the ApplAI SQLite DB on the remote host.
# Run daily from cron as the owner of /srv/applai (UID 1000) or root, e.g.:
#   15 3 * * * /srv/applai/applai/scripts/backup-db.sh >> /srv/applai/backup.log 2>&1
# Backups land in APPLAI_BACKUP_DIR (a send-only Syncthing folder to the Mac); the newest KEEP are kept.
set -euo pipefail

cd "$(dirname "$0")/.."
BACKUP_DIR="${APPLAI_BACKUP_DIR:-/srv/applai/backups}"
KEEP="${KEEP:-14}"
NAME="applai-$(date -u +%Y%m%dT%H%M%SZ).db"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.remote.yml)

trap 'rm -f "data/db/$NAME"' EXIT

# sqlite3's backup API copies a consistent snapshot while the API keeps running
"${COMPOSE[@]}" exec -T applai-api python - "$NAME" <<'PY'
import sqlite3
import sys

src = sqlite3.connect("/data/db/applai.db")
dst = sqlite3.connect(f"/data/db/{sys.argv[1]}")
src.backup(dst)
result = dst.execute("PRAGMA integrity_check").fetchone()[0]
dst.close()
src.close()
if result != "ok":
    sys.exit(f"integrity_check failed: {result}")
PY

mkdir -p "$BACKUP_DIR"
mv "data/db/$NAME" "$BACKUP_DIR/$NAME"
echo "$(date -u +%FT%TZ) backed up to $BACKUP_DIR/$NAME"

ls -1t "$BACKUP_DIR"/applai-*.db | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -f -- "$old"
    echo "removed $old"
done
