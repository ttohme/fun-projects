.PHONY: up down logs db-init watcher evals validate test approve execute capture \
        bootstrap backup restore-db healthcheck install-services uninstall-services \
        logrotate-install dead-letters hass-cache ledger-sync nudges briefing \
        calendar-ingest gpu-work wake-gpu improve help

ENV_FILE := infra/env/.env

# Prefer the project venv when available so installed deps are always found.
PYTHON := $(if $(wildcard .venv/bin/python3),.venv/bin/python3,python3)
PIP    := $(if $(wildcard .venv/bin/pip),.venv/bin/pip,pip)

help:
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

# --env-file is required: compose interpolates ${VARS} from the shell or
# --env-file only, never from a service-level env_file.
COMPOSE := docker compose --env-file $(ENV_FILE)

up: $(ENV_FILE) ## Start all services (docker compose up -d)
	$(COMPOSE) -f infra/docker-compose.yml up -d

down: $(ENV_FILE) ## Stop all services
	$(COMPOSE) -f infra/docker-compose.yml down

logs: $(ENV_FILE) ## Tail logs for all services
	$(COMPOSE) -f infra/docker-compose.yml logs -f

db-init: ## Create SQLite DB and apply schema
	@mkdir -p db
	$(PYTHON) -c "import sqlite3,pathlib; conn=sqlite3.connect('db/assistant.db'); conn.executescript(pathlib.Path('db/schema.sql').read_text()); conn.close(); print('db/assistant.db ready')"

watcher: ## Run the file watcher (requires N8N_WEBHOOK_URL env var)
	@if [ -z "$$N8N_WEBHOOK_URL" ]; then echo "ERROR: N8N_WEBHOOK_URL is not set"; exit 1; fi
	$(PIP) install -q -r apps/file-watcher/requirements.txt
	$(PYTHON) apps/file-watcher/watcher.py

approve: db-init ## Interactively review pending approvals
	$(PYTHON) apps/orchestrator/approval_service.py review

dead-letters: db-init ## List jobs that exhausted their retries
	$(PYTHON) apps/orchestrator/approval_service.py dead-letters

hass-cache: db-init ## Refresh the Home Assistant entity cache (requires HASS_URL/HASS_TOKEN)
	PYTHONPATH=apps/orchestrator $(PYTHON) apps/orchestrator/hass_registry.py refresh

ledger-sync: db-init ## Sync Todoist tasks into the local shadow ledger
	PYTHONPATH=apps/orchestrator $(PYTHON) apps/orchestrator/todoist_ledger.py sync

nudges: db-init ## Push a stale-task nudge (--dry-run via ARGS)
	PYTHONPATH=apps/orchestrator $(PYTHON) apps/orchestrator/todoist_ledger.py nudge $(ARGS)

briefing: ## Build and push the morning briefing (--dry-run via ARGS)
	PYTHONPATH=apps/orchestrator $(PYTHON) apps/orchestrator/briefing.py $(ARGS)

calendar-ingest: db-init ## Generate prep tasks from the calendar (--dry-run via ARGS)
	PYTHONPATH=apps/orchestrator $(PYTHON) apps/orchestrator/calendar_ingest.py run $(ARGS)

gpu-work: db-init ## Process awaiting_gpu media jobs once (transcribe/OCR/summarize)
	PYTHONPATH=apps/orchestrator $(PYTHON) apps/orchestrator/gpu_worker.py run

wake-gpu: ## Send a Wake-on-LAN packet to the Windows GPU box (GPU_MAC in .env)
	scripts/wake-gpu.sh

improve: db-init ## Run the continuous-improvement pipeline once (mine, eval, report)
	scripts/improvement_run.sh

execute: db-init ## Run job executor once (--dry-run to preview, --watch to poll)
	$(PYTHON) apps/orchestrator/job_executor.py $(ARGS)

capture: db-init ## Capture a voice/text task: make capture DESC="buy milk" [PROJECT=Inbox] [DUE=tomorrow]
	@if [ -z "$(DESC)" ]; then echo "ERROR: DESC is required"; exit 1; fi
	PYTHONPATH=apps/orchestrator $(PYTHON) -c "from voice_intake import capture; import json; print(json.dumps(capture(description='$(DESC)', project='$(PROJECT)', due_string='$(DUE)', source_ref='cli-capture', dry_run=False, db_path=None), indent=2))"

bootstrap: ## One-shot setup for a fresh machine (venv, deps, DB, dirs, .env)
	scripts/bootstrap.sh

backup: ## Snapshot the SQLite DB (WAL-safe, gzipped, rotated)
	scripts/backup-db.sh

restore-db: ## Restore DB from a backup: make restore-db SNAP=backups/assistant-TIMESTAMP.db.gz
	@if [ -z "$(SNAP)" ]; then echo "ERROR: SNAP is required. Usage: make restore-db SNAP=backups/assistant-....db.gz"; exit 1; fi
	scripts/restore-db.sh "$(SNAP)"

logrotate-install: ## Install logrotate config for logs/ (run with sudo, edit USER first)
	@if grep -q REPLACE_USER infra/logrotate/assistant; then \
		echo "ERROR: edit infra/logrotate/assistant first — replace REPLACE_USER with your username"; \
		exit 1; \
	fi
	sudo cp infra/logrotate/assistant /etc/logrotate.d/assistant
	sudo logrotate --debug /etc/logrotate.d/assistant

healthcheck: ## Probe local/tailnet service endpoints (exits non-zero if any down)
	scripts/healthcheck.sh

install-services: ## Install reboot-survivable systemd units (run with sudo)
	sudo scripts/install-services.sh

uninstall-services: ## Remove the assistant systemd units (run with sudo)
	sudo scripts/uninstall-services.sh

evals: ## Run promptfoo evaluation suite
	npx --yes promptfoo@latest eval --config evals/promptfooconfig.yaml

test: ## Run unit tests
	$(PIP) install -q -r requirements-dev.txt
	$(PYTHON) -m pytest tests/ -v

validate: ## Validate all config files (JSON, YAML, SQL)
	@$(PYTHON) -c "import json,pathlib; [json.load(open(p)) for p in pathlib.Path('apps/orchestrator/schemas').glob('*.json')]; print('  JSON schemas OK')"
	@$(PYTHON) -c "import yaml,pathlib; [yaml.safe_load(open(p)) for p in ['infra/litellm.yaml','mcp/registry.yaml','evals/promptfooconfig.yaml']]; print('  YAML files OK')"
	@$(PYTHON) -c "import sqlite3,pathlib; conn=sqlite3.connect(':memory:'); conn.executescript(pathlib.Path('db/schema.sql').read_text()); print('  SQLite schema OK')"
	@head -1 CLAUDE.md | grep -q '^@AGENTS.md$$' && echo '  CLAUDE.md @-import OK' || echo '  ERROR: CLAUDE.md line 1 must be @AGENTS.md'
	@ls -la .codex/hooks/*.py | grep -q 'rwx' && echo '  Hook permissions OK' || echo '  ERROR: hooks not executable'
	@for s in scripts/*.sh; do bash -n "$$s" || exit 1; done && echo '  Shell scripts OK'

$(ENV_FILE):
	@echo "ERROR: $(ENV_FILE) not found. Copy infra/env/.env.example and fill in secrets."
	@exit 1
