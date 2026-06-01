#!/usr/bin/env bash
# WAL-safe online backup of the SQLite source of truth, with integrity check,
# gzip compression, and rotation. Safe to run while the assistant is live.
#
#   scripts/backup-db.sh
#
# Env:
#   DB_PATH      path to the live DB        (default: <repo>/db/assistant.db)
#   BACKUP_DIR   where snapshots are kept   (default: <repo>/backups)
#   BACKUP_KEEP  how many to retain         (default: 14)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${DB_PATH:-$REPO_ROOT/db/assistant.db}"
BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/backups}"
KEEP="${BACKUP_KEEP:-14}"

if [ ! -f "$DB_PATH" ]; then
  echo "backup-db: no database at $DB_PATH" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
dest="$BACKUP_DIR/assistant-$ts.db"

# Python's sqlite3.backup() is WAL-aware and produces a consistent snapshot of a
# live DB; it then runs integrity_check. Uses the stdlib so no sqlite3 CLI needed.
python3 - "$DB_PATH" "$dest" <<'PY'
import sqlite3, sys
src_path, dest_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
dst = sqlite3.connect(dest_path)
with dst:
    src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
src.close(); dst.close()
if ok != "ok":
    sys.stderr.write(f"integrity check returned: {ok}\n")
    sys.exit(1)
PY

if [ $? -ne 0 ]; then
  echo "backup-db: integrity check FAILED for $dest" >&2
  rm -f "$dest"
  exit 1
fi

gzip -f "$dest"

# Rotation: keep the newest $KEEP, delete the rest.
# shellcheck disable=SC2012
ls -1t "$BACKUP_DIR"/assistant-*.db.gz 2>/dev/null \
  | tail -n +"$((KEEP + 1))" \
  | xargs -r rm -f

echo "backup-db: wrote $dest.gz (keeping newest $KEEP)"
