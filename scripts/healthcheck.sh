#!/usr/bin/env bash
# Probes the assistant's local/tailnet service endpoints. Exits non-zero if any
# probe fails — suitable for cron, a systemd timer, or an Uptime Kuma push.
#
#   scripts/healthcheck.sh
#
# Env (override per machine / tailnet):
#   N8N_URL       default http://localhost:5678/healthz
#   LITELLM_URL   default http://localhost:4000/health/liveliness
#   OPENCLAW_URL  default http://localhost:18789/health
#                 (/health is the only unauthenticated endpoint — /metrics needs the bearer token)
#   LANGFUSE_URL  default http://localhost:3000/api/public/health
#                 (3-machine split: set to the Pi 4, e.g. http://pi4.tailnet.ts.net:3000/api/public/health)
#   HEALTH_TIMEOUT seconds per probe (default 5)
set -uo pipefail

TIMEOUT="${HEALTH_TIMEOUT:-5}"
declare -A TARGETS=(
  ["n8n"]="${N8N_URL:-http://localhost:5678/healthz}"
  ["litellm"]="${LITELLM_URL:-http://localhost:4000/health/liveliness}"
  ["openclaw"]="${OPENCLAW_URL:-http://localhost:18789/health}"
  ["langfuse"]="${LANGFUSE_URL:-http://localhost:3000/api/public/health}"
)

fail=0
for name in "${!TARGETS[@]}"; do
  url="${TARGETS[$name]}"
  if curl -fsS --max-time "$TIMEOUT" "$url" >/dev/null 2>&1; then
    echo "  OK   $name  $url"
  else
    echo "  DOWN $name  $url" >&2
    fail=1
  fi
done

# Ollama / GPU box: informational — the box is on-demand, so unreachable is
# "OFF", not a failure. But reachable WITHOUT the expected models is a real
# problem (someone forgot ollama pull) and does fail the check.
if [ -n "${OLLAMA_BASE_URL:-}" ]; then
  tags_json="$(curl -fsS --max-time "$TIMEOUT" "${OLLAMA_BASE_URL%/}/api/tags" 2>/dev/null || true)"
  if [ -z "$tags_json" ]; then
    echo "  OFF  ollama  ${OLLAMA_BASE_URL} (on-demand box is off — not a failure)"
  else
    for model in llama3.1 hermes3; do
      if echo "$tags_json" | grep -q "\"$model"; then
        echo "  OK   ollama  model $model present"
      else
        echo "  DOWN ollama  box is up but model '$model' is missing — run: ollama pull $model" >&2
        fail=1
      fi
    done
  fi
fi

# Backup freshness: the nightly timer should leave a snapshot < 26h old.
# Only checked when BACKUP_DIR exists and has at least one snapshot (so a
# fresh install without backups yet doesn't alarm).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$REPO_ROOT/backups}"
if [ -d "$BACKUP_DIR" ] && ls "$BACKUP_DIR"/assistant-*.db.gz >/dev/null 2>&1; then
  fresh=$(find "$BACKUP_DIR" -name 'assistant-*.db.gz' -mmin -1560 | head -1)
  if [ -n "$fresh" ]; then
    echo "  OK   backups  newest snapshot < 26h old"
  else
    echo "  DOWN backups  newest snapshot older than 26h — check assistant-backup.timer" >&2
    fail=1
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "healthcheck: one or more services are DOWN" >&2
  "$(dirname "${BASH_SOURCE[0]}")/notify.sh" "One or more services are DOWN — run scripts/healthcheck.sh for detail" "Healthcheck failed" high
  exit 1
fi
echo "healthcheck: all services OK"
