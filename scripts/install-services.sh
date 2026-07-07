#!/usr/bin/env bash
# Installs the native always-on services as systemd units so they survive
# reboots (Pi 5 brain: watcher + executor + optional OpenClaw; + daily backup).
# Substitutes the repo path / user / OpenClaw binary into the unit templates.
#
#   sudo scripts/install-services.sh                 # watcher, executor, backup
#   sudo INSTALL_OPENCLAW=1 scripts/install-services.sh   # also OpenClaw
#
# Env:
#   SERVICE_USER   user to run as     (default: the invoking sudo user)
#   OPENCLAW_BIN   OpenClaw path      (default: /usr/local/bin/openclaw)
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "install-services: run with sudo" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$(logname)}}"
OPENCLAW_BIN="${OPENCLAW_BIN:-/usr/local/bin/openclaw}"
UNIT_SRC="$REPO_ROOT/infra/systemd"
UNIT_DST="/etc/systemd/system"

units=(assistant-watcher.service assistant-executor.service \
       assistant-backup.service assistant-backup.timer \
       assistant-alert@.service)
if [ "${INSTALL_OPENCLAW:-0}" = "1" ]; then
  units+=(assistant-openclaw.service)
fi

echo "==> Installing units for user '$SERVICE_USER' from repo '$REPO_ROOT'"
for u in "${units[@]}"; do
  sed -e "s#__REPO__#$REPO_ROOT#g" \
      -e "s#__USER__#$SERVICE_USER#g" \
      -e "s#__OPENCLAW_BIN__#$OPENCLAW_BIN#g" \
      "$UNIT_SRC/$u" > "$UNIT_DST/$u"
  echo "    $UNIT_DST/$u"
done

systemctl daemon-reload
systemctl enable --now assistant-watcher.service assistant-executor.service
systemctl enable --now assistant-backup.timer
if [ "${INSTALL_OPENCLAW:-0}" = "1" ]; then
  systemctl enable --now assistant-openclaw.service
fi

echo
echo "Installed. Check status with:"
echo "  systemctl status assistant-watcher assistant-executor"
echo "  systemctl list-timers assistant-backup.timer"
