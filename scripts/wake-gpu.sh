#!/usr/bin/env bash
# Wake the Windows GPU box via Wake-on-LAN from the Pi 5.
# Pure-python magic packet — no wakeonlan/etherwake package needed.
#
#   scripts/wake-gpu.sh            # uses GPU_MAC from infra/env/.env
#   GPU_MAC=aa:bb:cc:dd:ee:ff scripts/wake-gpu.sh
#
# Windows prep (one time): enable Wake-on-LAN in BIOS/UEFI and in the NIC's
# driver properties ("Wake on Magic Packet"); note the MAC with `getmac`.
#
# Env:
#   GPU_MAC        target MAC address (required)
#   GPU_BROADCAST  broadcast address  (default 255.255.255.255)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${GPU_MAC:-}" ] && [ -f "$REPO_ROOT/infra/env/.env" ]; then
  GPU_MAC="$(grep -E '^GPU_MAC=' "$REPO_ROOT/infra/env/.env" | tail -1 | cut -d= -f2-)"
  GPU_BROADCAST="${GPU_BROADCAST:-$(grep -E '^GPU_BROADCAST=' "$REPO_ROOT/infra/env/.env" | tail -1 | cut -d= -f2-)}"
fi

if [ -z "${GPU_MAC:-}" ]; then
  echo "wake-gpu: GPU_MAC is not set (infra/env/.env or environment)" >&2
  exit 1
fi

python3 - "$GPU_MAC" "${GPU_BROADCAST:-255.255.255.255}" <<'PY'
import socket, sys
mac, broadcast = sys.argv[1], sys.argv[2]
clean = mac.replace(":", "").replace("-", "").lower()
if len(clean) != 12:
    sys.stderr.write(f"wake-gpu: bad MAC address {mac!r}\n")
    sys.exit(1)
packet = b"\xff" * 6 + bytes.fromhex(clean) * 16
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
s.sendto(packet, (broadcast, 9))
s.close()
print(f"wake-gpu: magic packet sent to {mac} via {broadcast}")
PY
