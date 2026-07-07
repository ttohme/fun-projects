#!/usr/bin/env bash
# Best-effort push notification via the self-hosted ntfy server.
# Never fails the caller: exits 0 even when ntfy is unreachable or unset.
#
#   scripts/notify.sh "message" [title] [priority]
#
# Env (from infra/env/.env if present, or the environment):
#   NTFY_URL    e.g. http://localhost:8090   (empty = silently do nothing)
#   NTFY_TOPIC  default: assistant
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Pull NTFY_* from the env file without exporting unrelated secrets.
if [ -z "${NTFY_URL:-}" ] && [ -f "$REPO_ROOT/infra/env/.env" ]; then
  NTFY_URL="$(grep -E '^NTFY_URL=' "$REPO_ROOT/infra/env/.env" | tail -1 | cut -d= -f2-)"
  NTFY_TOPIC="${NTFY_TOPIC:-$(grep -E '^NTFY_TOPIC=' "$REPO_ROOT/infra/env/.env" | tail -1 | cut -d= -f2-)}"
fi

MESSAGE="${1:-}"
TITLE="${2:-}"
PRIORITY="${3:-default}"

if [ -z "${NTFY_URL:-}" ] || [ -z "$MESSAGE" ]; then
  exit 0
fi

curl -fsS --max-time 5 \
  -H "Priority: $PRIORITY" \
  ${TITLE:+-H "Title: $TITLE"} \
  -d "$MESSAGE" \
  "${NTFY_URL%/}/${NTFY_TOPIC:-assistant}" >/dev/null 2>&1 || true

exit 0
