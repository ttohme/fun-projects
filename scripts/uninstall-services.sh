#!/usr/bin/env bash
# Removes the systemd units installed by install-services.sh.
#
#   sudo scripts/uninstall-services.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "uninstall-services: run with sudo" >&2
  exit 1
fi

UNIT_DST="/etc/systemd/system"
units=(assistant-watcher.service assistant-executor.service \
       assistant-openclaw.service assistant-backup.service \
       assistant-backup.timer)

for u in "${units[@]}"; do
  systemctl disable --now "$u" 2>/dev/null || true
  rm -f "$UNIT_DST/$u"
done

systemctl daemon-reload
echo "Removed assistant systemd units."
