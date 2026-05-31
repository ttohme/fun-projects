.PHONY: up down logs db-init watcher evals validate help

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

evals: ## Run promptfoo evaluation suite
	npx --yes promptfoo@latest eval --config evals/promptfooconfig.yaml

validate: ## Validate all config files (JSON, YAML, SQL)
	@python3 -c "import json,pathlib; [json.load(open(p)) for p in pathlib.Path('apps/orchestrator/schemas').glob('*.json')]; print('  JSON schemas OK')"
	@python3 -c "import yaml,pathlib; [yaml.safe_load(open(p)) for p in ['infra/litellm.yaml','mcp/registry.yaml','evals/promptfooconfig.yaml']]; print('  YAML files OK')"
	@python3 -c "import sqlite3,pathlib; conn=sqlite3.connect(':memory:'); conn.executescript(pathlib.Path('db/schema.sql').read_text()); print('  SQLite schema OK')"
	@head -1 CLAUDE.md | grep -q '^@AGENTS.md$$' && echo '  CLAUDE.md @-import OK' || echo '  ERROR: CLAUDE.md line 1 must be @AGENTS.md'
	@ls -la .codex/hooks/*.py | grep -q 'rwx' && echo '  Hook permissions OK' || echo '  ERROR: hooks not executable'

$(ENV_FILE):
	@echo "ERROR: $(ENV_FILE) not found. Copy infra/env/.env.example and fill in secrets."
	@exit 1
