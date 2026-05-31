"""
apps/orchestrator/voice_intake.py
Handles voice/text task capture using the task-drafting-agent prompt.

Accepts a natural-language description, normalises it into a structured task
object, and writes a jobs + approvals row. Designed to be called by the n8n
voice-to-task webhook or directly from a CLI shortcut.

Environment variables:
    LITELLM_BASE_URL  (default: http://localhost:4000)
    LITELLM_API_KEY   (default: empty)
    DB_PATH           (default: db/assistant.db)
"""
import argparse
import json
import os
import sys
from pathlib import Path

import requests as http

from db import insert_approval, insert_job, make_dedupe_key, open_db
from schema_validator import ValidationError, validate_task_object
from triage import (
    LITELLM_API_KEY,
    LITELLM_BASE_URL,
    _strip_frontmatter,
    should_require_approval,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "apps" / "orchestrator" / "prompts" / "task-drafting-agent.md"
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))


def call_drafting_agent(
    *,
    description: str,
    project: str = "Inbox",
    due_string: str = "",
) -> dict:
    """Call LiteLLM with the task-drafting-agent prompt. Returns a task dict."""
    template = _strip_frontmatter(PROMPT_PATH.read_text())
    prompt = (
        template
        .replace("{{description}}", description)
        .replace("{{project}}", project)
        .replace("{{due_string}}", due_string or "not specified")
    )

    url = f"{LITELLM_BASE_URL}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if LITELLM_API_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_API_KEY}"

    body = {
        "model": "assistant-default",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
    }
    resp = http.post(url, json=body, headers=headers, timeout=30)
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    return json.loads(raw)


def capture(
    *,
    description: str,
    project: str = "Inbox",
    due_string: str = "",
    source_ref: str = "",
    dry_run: bool = False,
    db_path: Path | None = None,
) -> dict:
    """
    Convert a natural-language description to a Todoist-ready task object,
    validate it, and write the jobs + approvals rows.

    Returns the task dict with _job_id and _approval_id populated (unless dry_run).
    Raises ValidationError if the model output is malformed.
    """
    # Copy so we never mutate the dict returned by the model call
    task = dict(call_drafting_agent(
        description=description,
        project=project,
        due_string=due_string,
    ))

    # Voice input always sets source_type=voice
    task.setdefault("source_type", "voice")
    task["approval_required"] = should_require_approval(task)

    validate_task_object(task)

    ref = source_ref or f"voice:{description[:40]}"
    if not task.get("dedupe_key"):
        task["dedupe_key"] = make_dedupe_key(ref, task.get("title", ""))

    if not dry_run:
        conn = open_db(db_path or DB_PATH)
        job_id = insert_job(
            conn,
            source_type="voice",
            source_ref=ref,
            payload=task,
            intent=task.get("intent"),
            approval_required=task["approval_required"],
        )
        if job_id is None:
            task["_skipped"] = "duplicate"
        else:
            task["_job_id"] = job_id
            # Voice captures always get an approval row — user confirms before Todoist write
            approval_id = insert_approval(
                conn,
                job_id=job_id,
                requested_action=f"Create task: {task.get('title', '')}",
            )
            task["_approval_id"] = approval_id

    return task


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture a voice/text task")
    parser.add_argument("description", help="Natural language task description")
    parser.add_argument("--project", default="Inbox")
    parser.add_argument("--due", default="", dest="due_string")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = capture(
        description=args.description,
        project=args.project,
        due_string=args.due_string,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
