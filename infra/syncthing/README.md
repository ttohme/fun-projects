# Syncthing — shared `sync/inbox/`

Syncthing keeps the `sync/inbox/` folder identical across your devices so you
can drop a file from your phone or Windows PC and have it appear on the Pi 5,
where the file-watcher picks it up and kicks off triage.

## Topology

```
  📱 phone / 💻 laptop / 🪟 Windows        🍓 Pi 5 (brain)
  ┌──────────────────────────────┐        ┌──────────────────────────────┐
  │  drop a file into the synced  │        │  ~/fun-projects/sync/inbox/   │
  │  "assistant-inbox" folder     │ ─────► │  file-watcher (running here)  │
  └──────────────────────────────┘  sync  │  → n8n webhook → triage       │
                                           └──────────────────────────────┘
```

The **Pi 5 is the hub**: it is the only device that runs the file-watcher, so
its copy of `sync/inbox/` is the one that matters. Other devices are
"send-receive" peers you capture from.

> Sync **only `sync/inbox/`**. Do **not** sync `sync/processed/` or
> `sync/rejected/` — the Pi 5 moves files there after handling them, and you
> don't want that churn propagating back to capture devices.

## One-time setup

### 1. Install Syncthing on each device
- Pi 5 / Pi 4 (Linux): `sudo apt install syncthing` then `systemctl --user enable --now syncthing`
- Windows: https://syncthing.net/downloads/ (or SyncTrayzor)
- Phone: Syncthing app (Android) / Möbius Sync (iOS)

Each install exposes a Web UI at `http://localhost:8384` and has a unique
**Device ID** (Actions → Show ID).

### 2. Pair the devices
On the Pi 5's Web UI: **Add Remote Device** → paste each other device's ID →
accept the reciprocal prompt on that device. Pair every capture device with the
Pi 5 (you don't need to mesh them all to each other — hub-and-spoke is enough).

### 3. Share the inbox folder
On the Pi 5: **Add Folder**
- **Folder Label:** `assistant-inbox`
- **Folder ID:** `assistant-inbox`   (must match on every device)
- **Folder Path:** `/home/<you>/fun-projects/sync/inbox`
- **Sharing tab:** tick every capture device
- **Ignore Patterns:** paste the contents of `.stignore` (below)

Accept the share prompt on each capture device and point its folder path at
wherever you want to drop files there.

### 4. Restrict Syncthing to the tailnet
In each device's **Settings → GUI / Connections**, bind the sync and GUI
listeners to the Tailscale interface (or firewall ports 22000/tcp+udp and 8384
to the tailnet). Syncthing traffic then rides the same private mesh as the rest
of the system — nothing on the public internet.

## `.stignore`

Put this in the **Ignore Patterns** box for the `assistant-inbox` folder so the
git-tracking files and editor temp files never sync:

```
.gitignore
.gitkeep
.stignore
.stfolder
.DS_Store
*.tmp
*.swp
*.part
.*
```

The file-watcher already skips hidden / `.tmp` / `.swp` / `.part` files, so this
keeps the two layers consistent.

## Verify

1. Drop `hello.txt` into the synced folder on your phone.
2. Within a few seconds it appears in `sync/inbox/` on the Pi 5.
3. The file-watcher logs a structured JSON line and POSTs to the n8n webhook.
4. A job row shows up in SQLite (`make approve` to see it pending).

## Off-site backup copies (second copy of the DB snapshots)

The nightly backup timer writes gzipped SQLite snapshots to `backups/` on the
Pi 5. Syncthing gives you free off-site copies — snapshots are KB–MB scale, so
this costs effectively nothing:

1. On the Pi 5, add `backups/` as a second Syncthing folder, type
   **Send Only** (the Pi 5 is the sole producer; nothing may write back).
2. Share it with the Pi 4 and your laptop, both **Receive Only**.
3. On the receivers, set **File Versioning → Simple** (keep 5) so a corrupted
   snapshot that syncs over never destroys the previous good copy.
4. The laptop copy covers house-level disasters (fire/theft take both Pis).

`scripts/healthcheck.sh` alarms if the newest snapshot on the Pi 5 is older
than 26 hours, so a silently broken timer gets a phone push instead of being
discovered during a restore.
