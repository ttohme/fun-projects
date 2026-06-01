#!/usr/bin/env bash
# Probes the assistant's local/tailnet service endpoints. Exits non-zero if any
# probe fails — suitable for cron, a systemd timer, or an Uptime Kuma push.
#
#   scripts/healthcheck.sh
#
# Env (override per machine / tailnet):
#   N8N_URL       default http://localhost:5678/healthz
#   LITELLM_URL   default http://localhost:4000/health/liveliness
#   OPENCLAW_URL  default http://localhost:18789/metrics
#   LANGFUSE_URL  default http://localhost:3000/api/public/health
#   HEALTH_TIMEOUT seconds per probe (default 5)
set -uo pipefail

TIMEOUT="${HEALTH_TIMEOUT:-5}"
declare -A TARGETS=(
  ["n8n"]="${N8N_URL:-http://localhost:5678/healthz}"
  ["litellm"]="${LITELLM_URL:-http://localhost:4000/health/liveliness}"
  ["openclaw"]="${OPENCLAW_URL:-http://localhost:18789/metrics}"
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

if [ "$fail" -ne 0 ]; then
  echo "healthcheck: one or more services are DOWN" >&2
  exit 1
fi
echo "healthcheck: all services OK"
