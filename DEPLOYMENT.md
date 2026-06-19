# Deployment — 3-machine topology

This system is self-hosted across three machines you own, tied together by a
[Tailscale](https://tailscale.com) private mesh VPN. Nothing is exposed to the
public internet; everything binds to loopback or the tailnet.

## Role of each machine

| Machine | Role | Runs |
|---|---|---|
| 🍓 **Raspberry Pi 5** | The brain (always on) | n8n, LiteLLM, Redis, OpenClaw, file-watcher, orchestrator scripts, SQLite (source of truth) |
| 🍓 **Raspberry Pi 4** | The monitor (always on) | Langfuse (traces), Uptime Kuma (health dashboard) |
| 🪟 **Windows PC** | The muscle (on demand) | Ollama (local-private model, GPU), OpenHands (code sandbox), Codex CLI / Claude Code |

The Pi 5 carries the core pipeline. The Pi 4 takes observability off the Pi 5 so
the brain stays lean. The Windows PC supplies GPU inference and heavy code
execution only when it's powered on — the always-on path never depends on it.

```
  🍓 Pi 5 (brain)            🍓 Pi 4 (monitor)          🪟 Windows (muscle)
  ┌────────────────┐         ┌────────────────┐         ┌────────────────┐
  │ n8n      :5678 │         │ Langfuse :3000 │         │ Ollama   :11434│
  │ LiteLLM  :4000 │◄───────►│ Uptime   :3001 │         │ OpenHands :3333│
  │ OpenClaw :18789│ traces  │                │         │ Codex/Claude   │
  │ Redis · SQLite │         │ monitors all ──┼────────►│                │
  │ file-watcher   │         │ ports          │         │                │
  │ orchestrator   │─────────┼────────────────┼────────►│ local-private  │
  └───────┬────────┘ private │                │  LLM    └───────┬────────┘
          │          model   │                │ requests        │
          └───────────────────┴── TAILSCALE ──┴─────────────────┘
                       private encrypted mesh + 📱 phone
```

## Network wiring (the three cross-machine links)

All configured through `infra/env/.env` — the compose files and `litellm.yaml`
read these variables, so the same checked-in files work on every machine.

| Link | Variable | Set on | Value |
|---|---|---|---|
| LiteLLM → Ollama | `OLLAMA_BASE_URL` | Pi 5 | `http://windows-pc.tailnet.ts.net:11434` |
| LiteLLM → Langfuse | `LANGFUSE_HOST` | Pi 5 | `http://pi4.tailnet.ts.net:3000` |
| OpenHands → LiteLLM | `OPENHANDS_LLM_BASE_URL` | Windows | `http://pi5.tailnet.ts.net:4000` |
| n8n public webhook | `N8N_WEBHOOK_BASE_URL` | Pi 5 | `http://pi5.tailnet.ts.net:5678` |
| Langfuse login URL | `LANGFUSE_NEXTAUTH_URL` | Pi 4 | `http://pi4.tailnet.ts.net:3000` |

Replace `*.tailnet.ts.net` with your actual Tailscale MagicDNS names
(`tailscale status` lists them).

## Bring-up order

### 1. Tailscale (all three machines)
```bash
# Install per https://tailscale.com/download, then on each box:
tailscale up
tailscale status      # note the MagicDNS name of each machine
```
Tag each machine by role (`tag:brain` = Pi 5, `tag:monitor` = Pi 4,
`tag:muscle` = Windows) and paste `infra/tailscale/acl.json` into the admin
console's Access Controls. It locks cross-machine traffic down to only the
service-to-service ports this system needs.

File capture across devices uses Syncthing on the shared `sync/inbox/` folder —
see [`infra/syncthing/README.md`](infra/syncthing/README.md).

### 2. Pi 4 — the monitor
```bash
cp infra/env/.env.example infra/env/.env   # fill in secrets + tailnet hostnames
docker compose -f infra/docker-compose.pi4.yml up -d
# Langfuse → http://pi4.tailnet.ts.net:3000   (create project, copy the keys)
# Uptime Kuma → http://pi4.tailnet.ts.net:3001 (add monitors for the ports below)
```

### 3. Windows PC — the muscle
```powershell
# Ollama natively (gets GPU): https://ollama.com/download
ollama serve
ollama pull llama3.1
# OpenHands in Docker Desktop:
docker compose -f infra/docker-compose.windows.yml up -d
```

### 4. Pi 5 — the brain
```bash
cp infra/env/.env.example infra/env/.env   # same secrets; tailnet hostnames for Pi4 + Windows
scripts/bootstrap.sh                       # venv, deps, DB, dirs (idempotent)
docker compose -f infra/docker-compose.pi5.yml up -d

# Reboot-survivable native services (watcher, executor, OpenClaw, daily backup):
sudo INSTALL_OPENCLAW=1 scripts/install-services.sh
```

This replaces running `make watcher` / `make execute` by hand — systemd keeps
them alive across crashes and reboots. See [`infra/systemd/README.md`](infra/systemd/README.md).
For a quick manual run instead: `make watcher` and `make execute ARGS=--watch`.

### 5. n8n configuration (one time, in the UI)
Open `http://pi5.tailnet.ts.net:5678`, import the stubs from
`apps/orchestrator/workflows/`, fill in credential UUIDs, then activate.

## Ports on the tailnet

| URL | Service | Use |
|---|---|---|
| `pi5.tailnet:5678` | n8n | Configure / monitor workflows |
| `pi5.tailnet:4000` | LiteLLM | Model proxy (internal) |
| `pi5.tailnet:18789` | OpenClaw | Agent gateway |
| `pi4.tailnet:3000` | Langfuse | Trace explorer |
| `pi4.tailnet:3001` | Uptime Kuma | Health dashboard |
| `win.tailnet:3333` | OpenHands | Coding sandbox |
| `win.tailnet:11434` | Ollama | Local model API (internal) |

## Host firewall (UFW) — Pi 5 and Pi 4

Tailscale handles cross-machine auth, but it's good hygiene to restrict TCP ports
on each Pi's host OS to the Tailscale subnet only (`100.64.0.0/10`). This way, a
misconfigured Docker `ports:` mapping doesn't accidentally expose a service to the
LAN or internet.

```bash
# On Pi 5 and Pi 4 — run once after Tailscale is up.
sudo apt install ufw -y

# Deny everything by default, then punch holes.
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Always allow SSH (from any interface — restrict further if preferred)
sudo ufw allow ssh

# Tailscale itself (UDP 41641 plus the tun0 interface)
sudo ufw allow in on tailscale0
sudo ufw allow 41641/udp

# Docker-managed ports are controlled by iptables directly and bypass ufw's
# INPUT chain, so we need to limit them via Docker's own network rather than
# ufw rules.  Instead, edit each service in the compose file to bind to the
# tailscale0 IP:
#
#   ports:
#     - "100.x.x.x:5678:5678"   # tailscale0 IP only, not 0.0.0.0
#
# The tailnet ACL (infra/tailscale/acl.json) is your primary firewall for
# inter-machine traffic.

sudo ufw enable
sudo ufw status verbose
```

> **Tip**: run `ip addr show tailscale0` to get the machine's tailnet IP, then
> bind Docker ports to that address in the compose `ports:` stanza.

## Single-machine fallback

To run everything on one box (e.g. just the Pi 5), use the combined
`infra/docker-compose.yml` instead of the split files and leave the
`OLLAMA_BASE_URL` / `LANGFUSE_HOST` defaults (`host.docker.internal` /
`langfuse:3000`) as-is.

## Data flow recap

```
file → sync/inbox/ (Syncthing) → file-watcher (Pi5) → n8n webhook (Pi5)
  → triage.py / document_processor.py (Pi5)
      → LiteLLM (Pi5): default/planner → cloud APIs; private → Ollama (Windows)
  → jobs + approvals in SQLite (Pi5); traces → Langfuse (Pi4)
  → make approve (you)  → make execute → Todoist API / Home Assistant
```

## Security invariants (unchanged across machines)

- OpenClaw binds `127.0.0.1` only; remote access is via Tailscale, never open ports.
- No outbound email, file deletion, or home-control write without an approval row.
- Secrets live only in `infra/env/.env` (gitignored) on each machine.
- OpenHands mounts the Docker socket — keep it on the Windows PC behind the tailnet.
