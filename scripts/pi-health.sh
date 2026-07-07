#!/usr/bin/env bash
# Pi health sentinel: disk, temperature, undervoltage. Runs hourly from
# assistant-health.timer on each Pi (~2 seconds of work).
#
# Alerts (ntfy via notify.sh) only on threshold breaches; optionally pushes a
# dead-man heartbeat to an Uptime Kuma push monitor on EVERY healthy run —
# Kuma then alerts when the heartbeat stops, catching a wedged Pi that can't
# alert for itself.
#
#   scripts/pi-health.sh
#
# Env (infra/env/.env or environment):
#   DISK_ALERT_PCT     alert when / usage exceeds this   (default 85)
#   TEMP_ALERT_C       alert when SoC temp exceeds this  (default 75)
#   KUMA_PUSH_URL      Uptime Kuma push-monitor URL      (optional)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NOTIFY="$REPO_ROOT/scripts/notify.sh"

DISK_ALERT_PCT="${DISK_ALERT_PCT:-85}"
TEMP_ALERT_C="${TEMP_ALERT_C:-75}"
HOST="$(hostname)"
problems=()

# ── Disk usage on / ───────────────────────────────────────────────────────────
disk_pct="$(df --output=pcent / 2>/dev/null | tail -1 | tr -dc '0-9')"
if [ -n "$disk_pct" ] && [ "$disk_pct" -ge "$DISK_ALERT_PCT" ]; then
  problems+=("disk ${disk_pct}% full (threshold ${DISK_ALERT_PCT}%)")
fi

# ── SoC temperature (sysfs works on all Pis; no vcgencmd dependency) ─────────
if [ -r /sys/class/thermal/thermal_zone0/temp ]; then
  temp_c=$(( $(cat /sys/class/thermal/thermal_zone0/temp) / 1000 ))
  if [ "$temp_c" -ge "$TEMP_ALERT_C" ]; then
    problems+=("SoC at ${temp_c}°C (threshold ${TEMP_ALERT_C}°C)")
  fi
fi

# ── Undervoltage / throttling flags (Raspberry Pi only; skip elsewhere) ──────
if command -v vcgencmd >/dev/null 2>&1; then
  throttled="$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
  # Bit 0 = undervoltage now, bit 16 = undervoltage has occurred since boot.
  if [ -n "$throttled" ] && [ $(( throttled & 0x1 )) -ne 0 ]; then
    problems+=("UNDERVOLTAGE right now (throttled=$throttled) — check the PSU")
  elif [ -n "$throttled" ] && [ $(( throttled & 0x50000 )) -ne 0 ]; then
    problems+=("undervoltage/throttling occurred since boot (throttled=$throttled)")
  fi
fi

if [ "${#problems[@]}" -gt 0 ]; then
  msg="$HOST health:"$'\n'"$(printf ' - %s\n' "${problems[@]}")"
  echo "$msg" >&2
  "$NOTIFY" "$msg" "Pi health: $HOST" high
  exit 1
fi

echo "pi-health: $HOST OK (disk ${disk_pct:-?}%, temp ${temp_c:-?}°C)"

# Dead-man heartbeat: only on a healthy run, so Kuma alerts when either the
# Pi wedges (no heartbeat) or this script starts failing (exit 1 above).
if [ -n "${KUMA_PUSH_URL:-}" ]; then
  curl -fsS --max-time 5 "${KUMA_PUSH_URL}?status=up&msg=ok" >/dev/null 2>&1 || true
fi
exit 0
