---
name: inbox-triage
description: >
  Classifies items in sync/inbox/, assigns intent, sets approval_required flag,
  and inserts a job record into the database. Invokes the triage-agent prompt
  template. Use this skill when a batch of new inbox files needs processing.
input:
  - name: file_paths
    type: array
    description: List of absolute paths to files in sync/inbox/ to triage
  - name: dry_run
    type: boolean
    default: false
    description: If true, classify but do not write to DB or move files
output:
  - name: classified_jobs
    type: array
    description: Array of job objects with intent, approval_required, and confidence
---

# Inbox Triage Skill

## When to Use
Use this skill after the file-watcher notifies of new items in `sync/inbox/`,
or on-demand when manually dropping files to be processed.

## Steps
1. For each file in `file_paths`, read the file content.
2. Invoke the triage prompt at `apps/orchestrator/prompts/triage-agent.md`
   against the `planner` model via the LiteLLM gateway.
3. Parse the structured JSON response.
4. Apply rules:
   - If `confidence < 0.7`: set `approval_required = true`.
   - If `intent` is in `["delete_task", "home_control_write", "send_email"]`:
     set `approval_required = true` regardless of confidence.
5. If not `dry_run`:
   a. Insert a row into the `jobs` table in the SQLite DB.
   b. Move file to `sync/processed/` on success, `sync/rejected/` on parse failure.
6. Return the array of classified job objects.

## Rules
- Never modify Todoist directly from this skill. Insert a `jobs` row; the
  n8n workflow handles the Todoist write with its own approval gate.
- Always validate the triage response against
  `apps/orchestrator/schemas/task-object.schema.json` before DB insert.
- Log every classification decision to `logs/tool_use.jsonl`.
