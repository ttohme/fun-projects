"""
apps/orchestrator/triage.py
Runs the triage-agent prompt against LiteLLM and writes the result to the DB.

Usage (CLI):
    python -m apps.orchestrator.triage --source-type file --source-ref /path/to/file

Environment variables required:
    LITELLM_BASE_URL  -- e.g. http://localhost:4000  (default: http://localhost:4000)
    LITELLM_MASTER_KEY -- master key from litellm.yaml (default: empty)
    DB_PATH           -- path to SQLite DB              (default: db/assistant.db)
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

from db import (
    insert_approval,
    insert_job,
    make_dedupe_key,
    open_db,
    update_job_status,
)
from schema_validator import validate_task_object, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "apps" / "orchestrator" / "prompts" / "triage-agent.md"

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))

HIGH_RISK_INTENTS = {"delete_task", "home_control_write", "send_email"}
CONFIDENCE_THRESHOLD = 0.7


def load_prompt_template() -> str:
    return PROMPT_PATH.read_text()


def render_prompt(template: str, *, input_content: str, source_type: str, source_ref: str) -> str:
    prompt = template
    prompt = prompt.replace("{{input_content}}", input_content)
    prompt = prompt.replace("{{source_type}}", source_type)
    prompt = prompt.replace("{{source_ref}}", source_ref)
    return prompt


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter block (--- ... ---) from a prompt template."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def call_litellm(prompt: str, model: str = "planner") -> str:
    """POST to LiteLLM /chat/completions, return raw content string."""
    url = f"{LITELLM_BASE_URL}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if LITELLM_MASTER_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_MASTER_KEY}"

    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }

    resp = requests.post(url, json=body, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def should_require_approval(task: dict) -> bool:
    if task.get("intent") in HIGH_RISK_INTENTS:
        return True
    if task.get("confidence", 1.0) < CONFIDENCE_THRESHOLD:
        return True
    return bool(task.get("approval_required", False))


def triage(
    *,
    input_content: str,
    source_type: str,
    source_ref: str,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> dict:
    """
    Run triage on a single piece of content.
    Returns the classified task object dict.
    Raises ValidationError if the model output doesn't match the schema.
    """
    template = load_prompt_template()
    system_prompt = _strip_frontmatter(template)
    rendered = render_prompt(system_prompt, input_content=input_content,
                             source_type=source_type, source_ref=source_ref)

    raw = call_litellm(rendered)
    try:
        task = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned non-JSON for {source_ref!r}: {raw[:300]!r}") from exc

    validate_task_object(task)

    # Enforce approval policy regardless of model output
    task["approval_required"] = should_require_approval(task)

    # Ensure dedupe_key is present
    if not task.get("dedupe_key"):
        task["dedupe_key"] = make_dedupe_key(source_ref, task.get("title", ""))

    if not dry_run:
        conn = open_db(db_path or DB_PATH)
        try:
            job_id = insert_job(
                conn,
                source_type=source_type,
                source_ref=source_ref,
                payload=task,
                intent=task.get("intent"),
                approval_required=task["approval_required"],
            )
            if job_id is None:
                task["_skipped"] = "duplicate"
            else:
                task["_job_id"] = job_id
                if task["approval_required"]:
                    approval_id = insert_approval(
                        conn,
                        job_id=job_id,
                        requested_action=f"Create task: {task.get('title', '')}",
                    )
                    task["_approval_id"] = approval_id
        finally:
            conn.close()

    return task


def main() -> None:
    parser = argparse.ArgumentParser(description="Triage a piece of content")
    parser.add_argument("--source-type", required=True,
                        choices=["email", "voice", "file", "webhook", "chat"])
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--content", help="Content string (or omit to read stdin)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    content = args.content or sys.stdin.read()
    result = triage(
        input_content=content,
        source_type=args.source_type,
        source_ref=args.source_ref,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
