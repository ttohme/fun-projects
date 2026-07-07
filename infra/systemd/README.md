# systemd units — reboot-survivable native services

The Pi 5 runs three native (non-Docker) processes plus a backup job. These units
keep them alive across crashes and reboots. Docker services already restart via
`restart: unless-stopped` in the compose files; these cover the rest.

| Unit | Runs | Where |
|---|---|---|
| `assistant-watcher.service` | `apps/file-watcher/watcher.py` | Pi 5 |
| `assistant-executor.service` | `job_executor.py --watch` | Pi 5 |
| `assistant-openclaw.service` | OpenClaw gateway (external binary) | Pi 5 (optional) |
| `assistant-backup.service` + `.timer` | `scripts/backup-db.sh` daily 03:30 | Pi 5 |

The units ship as templates with `__REPO__`, `__USER__`, and `__OPENCLAW_BIN__`
placeholders. `scripts/install-services.sh` substitutes them and installs to
`/etc/systemd/system/`.

## Install

```bash
# Assumes scripts/bootstrap.sh has been run (creates .venv and the DB).
sudo scripts/install-services.sh                  # watcher + executor + backup
sudo INSTALL_OPENCLAW=1 scripts/install-services.sh   # also the OpenClaw gateway
```

Override the run-as user or OpenClaw path if needed:

```bash
sudo SERVICE_USER=pi OPENCLAW_BIN=/usr/local/bin/openclaw scripts/install-services.sh
```

## Manage

```bash
systemctl status assistant-watcher assistant-executor
journalctl -u assistant-executor -f          # follow logs
systemctl list-timers assistant-backup.timer # next backup run
sudo systemctl restart assistant-watcher
```

## Uninstall

```bash
sudo scripts/uninstall-services.sh
```

## Notes

- All units read secrets from `__REPO__/infra/env/.env` via `EnvironmentFile`.
- `Restart=always` with `RestartSec=5` rides out transient errors (e.g. n8n
  briefly unreachable) without hammering.
- Crash-loop escalation: `StartLimitIntervalSec=600` + `StartLimitBurst=5`
  means more than 5 restarts in 10 minutes stops the unit, and
  `OnFailure=assistant-alert@%n.service` fires a phone push via
  `scripts/notify.sh` (ntfy) so a dead service never fails silently.
  Recover with `systemctl reset-failed <unit> && systemctl start <unit>`.
- Hardening flags (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`) limit
  blast radius; OpenClaw omits `ProtectSystem=full` in case its binary lives
  outside the protected paths.
- OpenClaw is an external gateway binary — install it separately and point
  `OPENCLAW_BIN` at it. The other units run from the repo's `.venv`.
