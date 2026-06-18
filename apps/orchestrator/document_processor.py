"""
apps/orchestrator/document_processor.py
Processes files from sync/inbox/ using the document-triage-agent prompt.

A single document may yield multiple tasks. Each task is validated and
written as a separate jobs row. Files are moved to sync/processed/ on
success or sync/rejected/ on parse/validation failure.

Environment variables:
    LITELLM_BASE_URL  (default: http://localhost:4000)
    LITELLM_API_KEY   (default: empty)
    DB_PATH           (default: db/assistant.db)
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import requests as http

from db import insert_approval, insert_job, make_dedupe_key, open_db, update_job_status
from schema_validator import ValidationError, validate_task_object
from triage import (
    CONFIDENCE_THRESHOLD,
    HIGH_RISK_INTENTS,
    LITELLM_API_KEY,
    LITELLM_BASE_URL,
    _strip_frontmatter,
    should_require_approval,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "apps" / "orchestrator" / "prompts" / "document-triage-agent.md"
INBOX_DIR = REPO_ROOT / "sync" / "inbox"
PROCESSED_DIR = REPO_ROOT / "sync" / "processed"
REJECTED_DIR = REPO_ROOT / "sync" / "rejected"
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))

SUPPORTED_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".html"}
SUPPORTED_DOC_SUFFIXES = {".pdf"}  # future: use pdfminer or pymupdf


def read_file_text(path: Path) -> str:
    """Extract text from supported file types. Returns empty string on failure."""
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_TEXT_SUFFIXES:
        return path.read_text(errors="replace")
    if suffix in SUPPORTED_DOC_SUFFIXES:
        # Placeholder: real deployment would use pdfminer or pymupdf
        return f"[PDF content extraction not yet configured for: {path.name}]"
    return f"[Unsupported file type: {suffix}]"


def call_document_agent(*, document_text: str, file_name: str, source_type: str) -> list[dict]:
    """Call LiteLLM with the document-triage-agent prompt. Returns list of task dicts."""
    template = _strip_frontmatter(PROMPT_PATH.read_text())
    prompt = (
        template
        .replace("{{document_text}}", document_text)
        .replace("{{file_name}}", file_name)
        .replace("{{source_type}}", source_type)
    )

    url = f"{LITELLM_BASE_URL}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if LITELLM_API_KEY:
        headers["Authorization"] = f"Bearer {LITELLM_API_KEY}"

    body = {
        "model": "assistant-small",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    resp = http.post(url, json=body, headers=headers, timeout=60)
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Document agent returned non-JSON for {file_name!r}: {raw[:300]!r}") from exc

    # Model may return {"tasks": [...]} or a bare array
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and "tasks" in parsed:
        return parsed["tasks"]
    # Single task wrapped in an object
    return [parsed]


def process_file(
    path: Path,
    *,
    dry_run: bool = False,
    db_path: Path | None = None,
) -> list[dict]:
    """
    Triage a single file. Returns list of task result dicts.
    Moves file to processed/ or rejected/ unless dry_run=True.
    """
    source_ref = str(path)
    document_text = read_file_text(path)

    try:
        tasks = call_document_agent(
            document_text=document_text,
            file_name=path.name,
            source_type="file",
        )
    except Exception as exc:
        if not dry_run:
            _move(path, REJECTED_DIR)
        return [{"_error": str(exc), "_file": str(path), "_skipped": "parse_failure"}]

    results = []
    all_valid = True

    # Open one connection for the whole batch; None when dry_run.
    conn = open_db(db_path or DB_PATH) if not dry_run else None
    try:
        for raw_task in tasks:
            # Copy so we never mutate the list returned by the model call
            task = dict(raw_task)
            # Drop low-confidence tasks as per document-triage-agent rules
            if task.get("confidence", 1.0) < CONFIDENCE_THRESHOLD:
                task["_skipped"] = "low_confidence"
                results.append(task)
                continue

            try:
                validate_task_object(task)
            except ValidationError as exc:
                task["_error"] = str(exc)
                task["_skipped"] = "validation_failure"
                all_valid = False
                results.append(task)
                continue

            task["approval_required"] = should_require_approval(task)
            if not task.get("dedupe_key"):
                task["dedupe_key"] = make_dedupe_key(source_ref, task.get("title", ""))

            if conn is not None:
                job_id = insert_job(
                    conn,
                    source_type="file",
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

            results.append(task)
    finally:
        if conn is not None:
            conn.close()

    if not dry_run:
        dest = PROCESSED_DIR if all_valid else REJECTED_DIR
        _move(path, dest)

    return results


def _move(src: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}_{src.stat().st_ino}{src.suffix}"
    shutil.move(str(src), str(dest))


def main() -> None:
    parser = argparse.ArgumentParser(description="Process a file from sync/inbox/")
    parser.add_argument("file", help="Path to the file to process")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    results = process_file(Path(args.file), dry_run=args.dry_run)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
