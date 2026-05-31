# Personal AI Assistant System

A deployable, self-hosted personal AI assistant that automates email intake, voice/text task capture, file triage, home control, and coding workflows — using a hybrid of deterministic automation and agentic interfaces.

## Architecture Overview

| Layer | Component | Role |
|---|---|---|
| Gateway | OpenClaw | Always-on local gateway, loopback-bound |
| Tasks | Todoist | Single source of truth for all tasks |
| Orchestration | n8n | Deterministic workflow engine with human approval |
| Model routing | LiteLLM + Ollama | Local-first routing with cloud fallback |
| Coding | Codex CLI + Claude Code | Interactive coding workers |
| Sandboxing | OpenHands | Docker-isolated execution for risky code |
| Observability | Langfuse + Uptime Kuma | Traces and endpoint health |

## Prerequisites

- Docker and Docker Compose
- Python 3.11+
- Node.js 20+ (for Codex CLI, Claude Code, Todoist CLI)
- [Tailscale](https://tailscale.com) or SSH tunnel for remote access
- Accounts: Todoist, OpenAI or Anthropic (at least one frontier model)

## Quick Start

```bash
# 1. Clone and enter the repo
git clone <repo-url> && cd fun-projects

# 2. Copy and fill in secrets
cp infra/env/.env.example infra/env/.env
$EDITOR infra/env/.env

# 3. Start all services
docker compose -f infra/docker-compose.yml up -d

# 4. Verify services are healthy
docker compose -f infra/docker-compose.yml ps
```

Service URLs after startup:
- n8n UI: http://localhost:5678
- LiteLLM proxy: http://localhost:4000
- Langfuse traces: http://localhost:3000
- Uptime Kuma health: http://localhost:3001
- OpenHands sandbox: http://localhost:3333

## Makefile Targets

```bash
make up            # Start all Docker services
make down          # Stop all Docker services
make logs          # Tail service logs
make db-init       # Create db/assistant.db and apply schema
make test          # Run unit test suite (135 tests)
make validate      # Check JSON schemas, YAML, SQL, CLAUDE.md, hook permissions
make approve       # Interactive CLI to review and approve pending jobs
make execute       # Run job executor once (pass ARGS="--dry-run" to preview)
make capture       # Capture a voice/text task into the DB
                   #   make capture DESC="buy milk" PROJECT="Inbox" DUE="tomorrow"
make watcher       # Start the file watcher (requires N8N_WEBHOOK_URL)
make evals         # Run promptfoo evaluation suite
```

## Typical Daily Workflow

1. **Files land in `sync/inbox/`** → watcher POSTs to n8n → email/voice/file triage → jobs inserted into SQLite
2. **Review pending approvals**: `make approve`
3. **Execute approved jobs**: `make execute` (dispatches to Todoist or Home Assistant)
4. **Ad-hoc task capture**: `make capture DESC="Call dentist" PROJECT="Health"`

## Directory Map

```
.gitignore                           Secret and runtime file exclusions
README.md                            This file
AGENTS.md                            Shared agent governance rules (all agents)
CLAUDE.md                            Claude Code config (imports @AGENTS.md)
.codex/                              Codex CLI config and security hooks
.claude/                             Claude Code agents and skills
apps/
  file-watcher/watcher.py            Watches sync/inbox/ and notifies n8n
  orchestrator/
    db.py                            SQLite helpers (insert/update jobs & approvals)
    schema_validator.py              JSON Schema validation for task objects
    triage.py                        Email intake: LLM → task object → DB
    document_processor.py            File intake: document → task objects → DB
    voice_intake.py                  Voice/text capture → task object → DB
    approval_service.py              Review and approve/reject pending jobs
    todoist_client.py                Todoist REST API client
    hass_client.py                   Home Assistant REST API client
    job_executor.py                  Dispatches approved jobs by intent
    workflows/                       n8n workflow JSON stubs (import into n8n UI)
    prompts/                         LLM prompt templates used by workflows
    schemas/                         JSON Schema contracts for task objects
db/schema.sql                        SQLite schema (jobs + approvals)
evals/                               Promptfoo eval config and test cases
infra/
  docker-compose.yml                 All 6 services
  litellm.yaml                       Model routing config
  env/.env.example                   Required secrets template
mcp/registry.yaml                    MCP server allowlist and trust policies
openclaw/openclaw.json               OpenClaw gateway config
sync/
  inbox/                             Drop files here for automatic triage
  processed/                         Files successfully triaged
  rejected/                          Files that failed parsing
```

## Agent Instructions

- Agents (Codex, Claude Code) read `AGENTS.md` for shared rules.
- Claude Code additionally reads `CLAUDE.md` and loads skills from `.claude/skills/`.
- Codex reads `.codex/AGENTS.md` for Codex-specific rules.
- Security-critical operations are gated by `.codex/hooks/pre_tool_use_policy.py`.

## Initialising the Database

```bash
sqlite3 db/assistant.db < db/schema.sql
```

## Running Evals

```bash
# Requires OPENAI_API_KEY, ANTHROPIC_API_KEY, and a running Ollama instance
npx promptfoo eval --config evals/promptfooconfig.yaml
npx promptfoo view
```

## Security Notes

- OpenClaw is loopback-bound by default. Expose remotely only via Tailscale Serve or SSH tunnelling.
- All write operations to Todoist, Home Assistant, and external services require human approval.
- Never store secrets in code. All credentials belong in `infra/env/.env` (gitignored).
- For untrusted repos or dependency installs, use the OpenHands Docker sandbox.
