# Roadmap — Personal AI Assistant

Feature plan built from a constrained ideation pass (48 candidate ideas across six
lenses, consolidated and phased). Every feature obeys four rules:

1. **Free** — open-source/self-hosted only; no new paid APIs, subscriptions, or hardware.
   High-volume LLM work prefers local models to protect the $20/30d cloud budget cap.
2. **Fits the hardware** — Pi 5 (~8GB, always-on, already loaded) takes only lightweight
   always-on work; Pi 4 (~4GB) is nearly full; the Windows GPU box is **on-demand** and
   every feature that uses it must degrade gracefully when it is off.
3. **Genuinely useful** day-to-day for one person.
4. **Respects governance** (AGENTS.md) — approval gates, deterministic nodes over
   improvisation, secrets in `.env` only, tests + eval regression cases.

Phases are ordered by value-per-effort and dependency; each is independently shippable.

Effort: S = hours, M = a day-ish, L = multi-day.

---

## Phase 1 — Foundations & reliability (all Pi 5, zero GPU dependency)

Goal: the system alerts you when something breaks, survives failures, and closes the
biggest existing intake gap (PDFs). Everything here is small and pays rent immediately.

### 1.1 ntfy push-alert hub (S) — foundation for Phases 2–4
Self-hosted [ntfy](https://ntfy.sh) (single Go binary, ~20MB RAM, official ARM64 build)
on the Pi 5, bound to the tailnet; free open-source phone app.
- Add `ntfy` service to `infra/docker-compose.pi5.yml` + combined compose (tailnet bind)
- `scripts/notify.sh` helper (curl POST, topic + priority args)
- Wire into `scripts/healthcheck.sh` failures and `scripts/backup-db.sh` failures
- Notify on new approval-required jobs from `apps/orchestrator/*.py` intake paths
- Add `NTFY_URL` / `NTFY_TOPIC` to `infra/env/.env.example`; port to `infra/tailscale/acl.json`

### 1.2 Executor retry with backoff + dead-letter queue (M)
Today one flaky Todoist call marks a job `error` forever.
- Add `attempts`, `next_retry_at` columns (migration in `db/schema.sql`, keep idempotent)
- `job_executor.py`: exponential backoff (1m/5m/30m, max 3), then status `dead_letter`
- `make dead-letters` review command in `approval_service.py`; ntfy alert on dead-letter
- Unit tests for retry ladder + dead-letter transition

### 1.3 systemd watchdog hardening (S)
- `Restart=always`, `StartLimitIntervalSec`/`StartLimitBurst` crash-loop detection in
  `infra/systemd/*.service`; `OnFailure=` unit that fires `scripts/notify.sh`
- Document in `infra/systemd/README.md`

### 1.4 Off-site second-copy backups over Syncthing (S)
Gzipped snapshots are KB–MB scale; house-level disaster coverage is free.
- Share `backups/` (send-only) from Pi 5 → Pi 4 + laptop via Syncthing
- Update `infra/syncthing/README.md`; `.stignore` for partial files
- Backup-freshness check in `scripts/healthcheck.sh` (alert if newest snapshot > 26h old)

### 1.5 Real PDF ingestion (S) — fills the existing TODO in `document_processor.py`
- PyMuPDF text extraction on Pi 5 (milliseconds/page, pure CPU)
- Scanned/no-text-layer PDFs: queue for OCR (Tesseract CPU batch on Pi 5; GPU vision
  path upgrades this in Phase 3)
- Tests with a real fixture PDF; eval case for a PDF-derived task

### 1.6 Telegram quick-capture bot (S)
n8n's native Telegram Trigger node — capture from anywhere without opening an app.
- New n8n workflow stub `apps/orchestrator/workflows/telegram-capture.json`
- Route into the existing `voice_intake.py` path with `source_type="chat"`
- Bot replies with drafted task + whether it auto-proceeded or awaits approval
- Bot token in `.env.example`; note: inbound commands are capture-only (no actions)

### 1.7 HA entity registry cache + deterministic payload validation (S)
Stops hallucinated `entity_id`s before they ever reach an approval row.
- Nightly n8n cron dumps `GET /api/states` into a `hass_entities` SQLite table
- Validate every `home_control_*` payload against the cache before `insert_job`;
  unknown entity → validation failure, never a queued job
- Tests + eval case (AGENTS.md: task-semantics change)

---

## Phase 2 — Mobile experience & daily briefings

Goal: the assistant reaches you (phone push, morning brief) instead of you polling it.

### 2.1 ntfy one-tap approvals (M)
Approve/reject from a phone notification instead of SSH + `make approve`.
- ntfy action buttons → n8n webhook → `approval_service.decide()`
- Signed single-use token per approval id (HMAC w/ secret from `.env`) so a leaked
  notification can't approve anything else; expiry 24h
- Tests for token verification + replay rejection

### 2.2 Morning briefing (M)
07:00 n8n schedule: Todoist today/overdue + calendar (private ICS URL) + weather
(Open-Meteo, free/keyless) + HA home/energy digest → one `assistant-small` call
compresses to 6–8 lines → ntfy push. Optional audio: Piper TTS on Pi 5 ARM (runs
seconds/day, not resident); Kokoro on the GPU upgrades voice quality when the box is on.
- Workflow stub + `apps/orchestrator/briefing.py` (deterministic fetchers, one LLM call)
- `make briefing` for manual runs; read-only HA calls auto-proceed under existing rules

### 2.3 Todoist shadow ledger (S) — foundation for 2.4 and Phase 5 task-intel
30-min incremental sync of Todoist state into SQLite (`todoist_tasks` table).
Todoist stays the source of truth; the ledger is a read model for analytics.
- `apps/orchestrator/todoist_ledger.py` + systemd timer; tests

### 2.4 Stale-task nudges (S)
Weekly SQL over the ledger: tasks untouched N weeks → ntfy nudge with one-tap
"reschedule / keep / drop" actions; drop = `delete_task` intent → always approval-gated.

### 2.5 Calendar ICS ingestion with deterministic prep-task rules (M)
Poll private ICS feed; rule table (YAML) maps event patterns → prep tasks
("flight → pack + check-in T-24h"). Deterministic rules, no LLM; dedupe via existing keys.

### 2.6 Android share-sheet + browser bookmarklet capture (S)
One more n8n webhook route (`/webhook/capture`) + an HTTP Shortcut config for the phone
share sheet and a JS bookmarklet for the laptop; both over Tailscale.

### 2.7 Pi health sentinel + dead-man heartbeats (M)
- Hourly timer on both Pis: disk %, SD wear (if readable), temperature, undervoltage
  flags → Uptime Kuma **push** monitors (Pi 4) — silence itself becomes an alert
- Convert watcher/executor/backup to heartbeat pushes (one curl line each)

---

## Phase 3 — GPU unlock (graceful-degradation mandatory)

Goal: the Windows box becomes a real accelerator — woken on demand, never a dependency.

### 3.1 Hermes agent: `local-agent` alias (S) — user-requested
[Hermes 3](https://ollama.com/library/hermes3) (`hermes3:8b`, 4.7GB, verified on the
Ollama library) is finetuned for function calling + schema-exact JSON — exactly what
triage and HA-intent extraction need. Additive to `local-private` (llama3.1), not a
replacement.
- `infra/litellm.yaml`: `local-agent` = `ollama_chat/hermes3:8b` (ollama_chat/ carries
  tool definitions through), `drop_params: true`; fallback `local-agent → assistant-small`
  (non-private traffic only — `local-private` keeps NO fallback and fails closed)
- `openclaw/openclaw.json`: `tag:agent → local-agent` rule AFTER the `tag:private` rule
  (order = precedence)
- `DEPLOYMENT.md` Windows step: `ollama pull hermes3:8b` (official tag only)
- `evals/promptfooconfig.yaml`: add `ollama:hermes3:8b` provider for head-to-head vs
  llama3.1 on the existing cases; add one multi-entity `home_control_write` case
- `triage.py`: env-driven `TRIAGE_LOCAL_MODEL=local-agent` for private-labeled payloads; tests
- `scripts/healthcheck.sh`: probe `/api/tags` for the model ("box up but model missing" ≠ "box off")

### 3.2 Wake-on-LAN GPU orchestrator (S)
- `scripts/wake-gpu.sh` (etherwake/wakeonlan from the Pi 5); `GPU_MAC` in `.env.example`
- n8n schedule wakes the box before nightly batch windows; executor can request a wake
  when `awaiting_gpu` depth exceeds a threshold
- Windows: BIOS WoL note in `DEPLOYMENT.md`

### 3.3 Voice memo transcription — the Whisper lane (M)
Record on phone → Syncthing `sync/voice/` → job `status='awaiting_gpu'` →
faster-whisper on the GPU (large-v3/distil) → transcript into the existing
`voice_intake` path. Pi 5 fallback: whisper.cpp `tiny.en` (~75MB) for sub-minute memos
so the lane works with the GPU permanently off.
- Watcher route for audio extensions; `awaiting_gpu` status migration; poller in executor
- Tests; eval case for a transcribed-memo task

### 3.4 Photo & screenshot capture (M)
Receipts/whiteboards/screenshots into `sync/inbox/`: Tesseract OCR on Pi 5 (CPU,
per-file batch) always works; a 7B vision model (llava/qwen2-vl q4, ~6–8GB VRAM) on the
GPU upgrades handwriting/layout understanding when available (`awaiting_gpu` queue).

### 3.5 Local-first triage routing (S)
Make `local-agent` the first choice for the high-volume triage path with automatic
LiteLLM fallback to `assistant-small` when the box is off — cuts cloud spend to near
zero when the GPU is up, changes nothing when it isn't. (Config + eval evidence from 3.1.)

### 3.6 Newsletter & long-email digest queue (M)
Long emails queue in SQLite; weekend GPU batch summarizes into one digest note.
If the box stays off, the digest ships links-only — still useful.

---

## Phase 4 — OpenClaw continuous-improvement loop — user-requested

Goal: the system learns from every approve/reject you make. Three existing signals
(approvals table, Langfuse traces, OpenClaw request logs) become a nightly flywheel.
**Nothing auto-deploys: a prompt change is itself an approval-gated job.**

```
approvals+jobs (SQLite) ─┐
OpenClaw request log ────┼─► miner ─► evals/cases/mined/*.yaml
Langfuse traces ─────────┘      │
                                ▼
        promptfoo: current prompts  vs  prompts/candidates/*
                                │
                                ▼
        win-rate report ─► job (intent=prompt_revision, approval_required=1)
                                │ make approve
                                ▼
        gated promotion: candidate → apps/orchestrator/prompts/
```

- **4.1** `scripts/approval_miner.py` (S): decided approvals → promptfoo cases in
  `evals/cases/mined/` (rejected = corrective assertion from `reason`; approved = sampled
  positives). Dedupe on `dedupe_key`; **redact/skip `private`-labeled payloads** (cases
  are committed to git); `last_run` in a `meta` table. Tests.
- **4.2** `evals/promptfooconfig.yaml` (S): load `file://evals/cases/mined/*.yaml`;
  add `prompts/candidates/*.md` pattern → promptfoo cross-products current vs candidate.
- **4.3** `apps/orchestrator/improvement_report.py` (M): parse promptfoo JSON, compute
  win-rate deltas, write `var/evals/reports/YYYY-MM-DD.md`; when a candidate wins,
  insert job `intent='prompt_revision', approval_required=1` + approvals row — lands in
  the normal `make approve` flow.
- **4.4** `job_executor.py` (M): `prompt_revision` joins `HIGH_RISK_INTENTS` (never
  auto-proceeds); approved → deterministic copy candidate → live, archive old version.
  Tests for gate + promotion.
- **4.5** `scripts/openclaw_log_miner.py` (S): journald metrics (per-tag routing,
  fallback rate, p95 latency) → report section; anomaly flag (e.g. fallback > 25%).
  Request metadata only — `log_responses` stays `false`.
- **4.6** `infra/systemd/assistant-improve.timer` (S): nightly 02:30 (before the 03:30
  backup so mined state lands in the snapshot); register in install/uninstall scripts.
- **4.7** Learned deterministic pre-triage rules (M): mine approval history for
  high-confidence patterns ("sender X + subject Y → always create_task/project Z") into a
  rules table consulted **before** the LLM — cheaper, faster, and deterministic-first per
  AGENTS.md. Rules themselves ship via the same gated `prompt_revision`-style approval.
- **4.8** Eval-gated local-model promotion (M): when evals show `local-agent` matches the
  cloud model on an intent, propose (gated) routing that intent local by default —
  measured cloud-spend reduction, not vibes.

---

## Phase 5 — Knowledge layer & autonomous jobs

Goal: search everything you've ever captured; let the system do bounded work alone.

- **5.1 Personal document RAG (L)**: sqlite-vec index on Pi 5 (personal-scale vectors are
  a few hundred MB; ANN queries are ms on ARM), embeddings batched on the GPU
  (`awaiting_gpu`), **FTS5 keyword fallback** (deterministic SQL) when the GPU is off.
  New `make ask Q="..."` + n8n webhook.
- **5.2 Semantic near-duplicate advisor (S)**: annotate approval items with "similar to
  task #N (87%)" via one embedding call; skipped silently when the GPU is off.
- **5.3 `code_job` executor → OpenHands (M)**: implement the stubbed intent — POST
  approved code jobs to OpenHands (:3333) with repo/branch/acceptance criteria; poll for
  completion; result → PR link in `result_json`. Always approval-gated; box-off = job
  waits, nothing lost.
- **5.4 Nightly repo maintenance (M)**: timer enqueues a `code_job` (deps audit, test
  flake report, TODO sweep) — runs whenever the box is next up; output is a report, and
  any actual change goes through a PR.
- **5.5 Memory layer (M)**: `facts` table (preferences, people, recurring context)
  injected into triage prompts; facts added/edited only via approval-gated capture.
- **5.6 NL scene compiler (M)**: "movie night" → validated HA scene JSON (entity cache
  from 1.7) → approval-gated `home_control_write`.
- **5.7 Task-intel pack (S each)**: Sunday weekly review; Friday "what entered the
  system" digest; recurring-pattern detector (RapidFuzz over titles, zero LLM) proposing
  real Todoist recurrences; `make project-brief` health snapshot.
- **5.8 Small intake extras (S each)**: RSS read-later with local-model filtering; email
  attachment router into the file pipeline; zero-token home Q&A via HA's conversation API;
  presence-aware departure/arrival checklist.

---

## Consolidations applied

Overlapping candidates merged during review: two Whisper voice lanes → 3.3; two PDF
ideas → 1.5; photo OCR + vision capture → 3.4; ntfy hub / one-tap approvals / three
briefing variants → 1.1 + 2.1 + 2.2; GPU-offline ladder + local-first routing +
eval-gated promotion → 3.5 + 4.8. Camera snapshot Q&A deferred (depends on HA camera
setup) — revisit after 3.4.

## Standing rules for every feature above

- Unit tests for touched modules; `make validate` green; eval regression case whenever
  task semantics change (AGENTS.md).
- New services: pinned image tags, scoped env vars, tailnet-only ports added to
  `infra/tailscale/acl.json`, entry in `scripts/healthcheck.sh` + Uptime Kuma.
- New intents default to `approval_required=1` until proven safe; `HIGH_RISK_INTENTS`
  can only grow.
