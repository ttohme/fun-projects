.PHONY: up down logs db-init watcher evals validate test approve execute capture \
        bootstrap backup restore-db healthcheck install-services uninstall-services \
        logrotate-install help

ENV_FILE := infra/env/.env

help:
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

up: $(ENV_FILE) ## Start all services (docker compose up -d)
	docker compose -f infra/docker-compose.yml up -d

down: ## Stop all services
	docker compose -f infra/docker-compose.yml down

logs: ## Tail logs for all services
	docker compose -f infra/docker-compose.yml logs -f

db-init: ## Create SQLite DB and apply schema
	@mkdir -p db
	python3 -c "import sqlite3,pathlib; conn=sqlite3.connect('db/assistant.db'); conn.executescript(pathlib.Path('db/schema.sql').read_text()); conn.close(); print('db/assistant.db ready')"

watcher: ## Run the file watcher (requires N8N_WEBHOOK_URL env var)
	@if [ -z "$$N8N_WEBHOOK_URL" ]; then echo "ERROR: N8N_WEBHOOK_URL is not set"; exit 1; fi
	pip install -q -r apps/file-watcher/requirements.txt
	python3 apps/file-watcher/watcher.py

approve: db-init ## Interactively review pending approvals
	python3 apps/orchestrator/approval_service.py review

execute: db-init ## Run job executor once (--dry-run to preview, --watch to poll)
	python3 apps/orchestrator/job_executor.py $(ARGS)

capture: db-init ## Capture a voice/text task: make capture DESC="buy milk" [PROJECT=Inbox] [DUE=tomorrow]
	@if [ -z "$(DESC)" ]; then echo "ERROR: DESC is required"; exit 1; fi
	PYTHONPATH=apps/orchestrator python3 -c "from voice_intake import capture; import json; print(json.dumps(capture(description='$(DESC)', project='$(PROJECT)', due_string='$(DUE)', source_ref='cli-capture', dry_run=False, db_path=None), indent=2))"

bootstrap: ## One-shot setup for a fresh machine (venv, deps, DB, dirs, .env)
	scripts/bootstrap.sh

backup: ## Snapshot the SQLite DB (WAL-safe, gzipped, rotated)
	scripts/backup-db.sh

restore-db: ## Restore DB from a backup: make restore-db SNAP=backups/assistant-TIMESTAMP.db.gz
	@if [ -z "$(SNAP)" ]; then echo "ERROR: SNAP is required. Usage: make restore-db SNAP=backups/assistant-....db.gz"; exit 1; fi
	scripts/restore-db.sh "$(SNAP)"

logrotate-install: ## Install logrotate config for logs/ (run with sudo, edit USER first)
	@echo "Edit infra/logrotate/assistant — replace REPLACE_USER with your username — then re-run."
	@grep -q REPLACE_USER infra/logrotate/assistant && (echo "ERROR: REPLACE_USER not substituted"; exit 1) || true
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
	pip install -q -r requirements-dev.txt
	python3 -m pytest tests/ -v

validate: ## Validate all config files (JSON, YAML, SQL)
	@python3 -c "import json,pathlib; [json.load(open(p)) for p in pathlib.Path('apps/orchestrator/schemas').glob('*.json')]; print('  JSON schemas OK')"
	@python3 -c "import yaml,pathlib; [yaml.safe_load(open(p)) for p in ['infra/litellm.yaml','mcp/registry.yaml','evals/promptfooconfig.yaml']]; print('  YAML files OK')"
	@python3 -c "import sqlite3,pathlib; conn=sqlite3.connect(':memory:'); conn.executescript(pathlib.Path('db/schema.sql').read_text()); print('  SQLite schema OK')"
	@head -1 CLAUDE.md | grep -q '^@AGENTS.md$$' && echo '  CLAUDE.md @-import OK' || echo '  ERROR: CLAUDE.md line 1 must be @AGENTS.md'
	@ls -la .codex/hooks/*.py | grep -q 'rwx' && echo '  Hook permissions OK' || echo '  ERROR: hooks not executable'
	@for s in scripts/*.sh; do bash -n "$$s" || exit 1; done && echo '  Shell scripts OK'

$(ENV_FILE):
	@echo "ERROR: $(ENV_FILE) not found. Copy infra/env/.env.example and fill in secrets."
	@exit 1
