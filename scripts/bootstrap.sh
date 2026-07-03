#!/usr/bin/env bash
# One-shot setup for a fresh machine (Pi 5 / Pi 4 / dev box). Idempotent:
# safe to re-run. Creates a venv, installs Python deps, initialises the DB,
# ensures runtime dirs exist, and seeds infra/env/.env from the example.
#
#   scripts/bootstrap.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Python virtualenv (.venv)"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

echo "==> Installing Python dependencies"
pip install -q --upgrade pip
pip install -q -r requirements-dev.txt
pip install -q -r apps/file-watcher/requirements.txt
pip install -q -r .codex/hooks/requirements.txt

echo "==> Runtime directories"
mkdir -p db logs backups sync/inbox sync/processed sync/rejected

echo "==> Database"
if [ ! -f db/assistant.db ]; then
  python3 -c "import sqlite3,pathlib; c=sqlite3.connect('db/assistant.db'); c.executescript(pathlib.Path('db/schema.sql').read_text()); c.close()"
  echo "    created db/assistant.db"
else
  echo "    db/assistant.db already exists"
fi

echo "==> Secrets"
if [ ! -f infra/env/.env ]; then
  cp infra/env/.env.example infra/env/.env
  echo "    seeded infra/env/.env from example — EDIT IT and fill in secrets"
else
  echo "    infra/env/.env already exists"
fi
chmod 600 infra/env/.env  # holds every credential — never world-readable

echo "==> Sanity check"
python3 -m pytest tests/ -q

echo
echo "Bootstrap complete. Next:"
echo "  1. Edit infra/env/.env with your secrets and tailnet hostnames"
echo "  2. Start Docker services for this machine's role (see DEPLOYMENT.md):"
echo "       docker compose --env-file infra/env/.env -f infra/docker-compose.<role>.yml up -d"
echo "     (single machine: make up)"
echo "  3. sudo scripts/install-services.sh   (reboot-survivable native services)"
