#!/usr/bin/env bash
# Restore the SQLite DB from a gzipped backup snapshot.
# Verifies integrity before replacing the live DB.
#
#   scripts/restore-db.sh <snapshot.db.gz>
#
# Env:
#   DB_PATH   path to the live DB (default: <repo>/db/assistant.db)
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <snapshot.db.gz>" >&2
  exit 1
fi

SNAPSHOT="$1"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${DB_PATH:-$REPO_ROOT/db/assistant.db}"
TMP="$(mktemp /tmp/assistant-restore-XXXXXX.db)"
trap 'rm -f "$TMP"' EXIT

if [ ! -f "$SNAPSHOT" ]; then
  echo "restore-db: snapshot not found: $SNAPSHOT" >&2
  exit 1
fi

echo "restore-db: decompressing $SNAPSHOT → $TMP"
gzip -dc "$SNAPSHOT" > "$TMP"

# Verify the snapshot before touching the live DB.
py_exit=0
python3 - "$TMP" <<'PY' || py_exit=$?
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
conn.close()
if ok != "ok":
    sys.stderr.write(f"integrity check returned: {ok}\n")
    sys.exit(1)
PY

if [ "$py_exit" -ne 0 ]; then
  echo "restore-db: snapshot failed integrity check — aborting, live DB untouched" >&2
  exit 1
fi

# Swap: make a safety copy of the current live DB first.
if [ -f "$DB_PATH" ]; then
  safety="${DB_PATH}.pre-restore.$(date -u +%Y%m%dT%H%M%SZ)"
  cp "$DB_PATH" "$safety"
  echo "restore-db: live DB backed up to $safety"
fi

cp "$TMP" "$DB_PATH"
echo "restore-db: restored $DB_PATH from $SNAPSHOT"
