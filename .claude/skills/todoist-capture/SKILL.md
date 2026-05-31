---
name: todoist-capture
description: >
  Takes a natural-language task description and creates a Todoist task via the
  approved MCP write flow. Always triggers an approval step before the Todoist
  API call. Use for voice-to-task and quick-capture from chat.
input:
  - name: description
    type: string
    description: Natural language description of the task to capture
  - name: project
    type: string
    default: "Inbox"
    description: Todoist project name or ID to add the task to
  - name: due_string
    type: string
    default: null
    description: Optional due date in natural language (e.g. "tomorrow at 9am")
output:
  - name: task_id
    type: string
    description: The created Todoist task ID
  - name: approval_id
    type: integer
    description: The approvals table row ID for audit trail
---

# Todoist Capture Skill

## When to Use
Use when the user says "add task", "remind me to", "capture", or similar
quick-add phrasing. Also used by the voice-to-task n8n workflow.

## Steps
1. Use the `task-drafting-agent` prompt to normalize `description` into a
   structured task object matching `apps/orchestrator/schemas/task-object.schema.json`.
2. Invoke the `risk-checker` subagent — a Todoist task creation is low-risk
   but must be checked to confirm no destructive intents are embedded.
3. Insert an `approvals` row with `requested_action = "create_todoist_task"`.
4. Present the structured task to the user for approval (show project, due date, labels).
5. On approval: call Todoist MCP `createTask`. On rejection: update the
   `approvals` row with `decision = "rejected"`.
6. Return `task_id` and `approval_id`.

## Rules
- Todoist is the source of truth. Never create duplicate tasks —
  query for existing tasks with similar descriptions first.
- If the task description contains personal health data, route to `local-private`
  model instead of `assistant-default`.
