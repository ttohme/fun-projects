"""
apps/orchestrator/job_executor.py
Dispatches approved jobs to their target systems.

Polls the DB for runnable jobs and routes each one to the correct handler
based on intent, updating job status to 'done' or 'error' after each attempt.
Runnable means status='approved' (human-approved via approval_service) or
status='pending' with approval_required=0 (auto-proceed). High-risk intents
never auto-proceed: they are re-flagged for approval instead.

Intent routing:
    create_task / update_task / document_triage  → todoist_client
    home_control_write                            → hass_client
    home_control_read                             → hass_client (read-only)
    send_email                                    → not yet implemented (logs warning)
    code_job                                      → not yet implemented (logs warning)
    unknown                                       → logged and skipped

CLI:
    python job_executor.py           # run once
    python job_executor.py --watch   # poll every N seconds (default 30)

Environment variables:
    DB_PATH           (default: db/assistant.db)
    TODOIST_API_TOKEN (required for Todoist intents)
    HASS_URL / HASS_TOKEN (required for home_control intents)
    EXECUTOR_POLL_INTERVAL (seconds, default 30, used with --watch)
"""
import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from db import (
    _now,
    get_pending_jobs,
    insert_approval,
    open_db,
    schedule_job_retry,
    update_job_status,
)
from hass_registry import UnknownEntityError
from notifier import notify

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))
POLL_INTERVAL = int(os.environ.get("EXECUTOR_POLL_INTERVAL", "30"))

# Transient-failure retry ladder: wait this long after attempt N (then dead-letter).
RETRY_BACKOFF_SECONDS = [60, 300, 1800]
MAX_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("job-executor")

TODOIST_INTENTS = {"create_task", "update_task", "document_triage"}
HASS_WRITE_INTENTS = {"home_control_write"}
HASS_READ_INTENTS = {"home_control_read"}
PROMPT_INTENTS = {"prompt_revision"}
UNIMPLEMENTED_INTENTS = {"send_email", "code_job", "delete_task"}

# AGENTS.md: these must never run without an approval decision, so they are
# excluded from auto-proceed even if a row was inserted with approval_required=0.
# prompt_revision changes system behavior — a human always signs off.
HIGH_RISK_INTENTS = {"delete_task", "home_control_write", "send_email",
                     "prompt_revision"}


def _get_approved_jobs(conn) -> list:
    # next_retry_at is ISO-8601 UTC, so string comparison is chronological.
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'approved'"
        " AND (next_retry_at IS NULL OR next_retry_at <= ?)"
        " ORDER BY updated_at",
        (_now(),),
    ).fetchall()


def _get_auto_proceed_jobs(conn) -> list:
    """Pending jobs flagged auto-proceed (approval_required=0, see db/schema.sql)."""
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'pending' AND approval_required = 0"
        " AND (next_retry_at IS NULL OR next_retry_at <= ?)"
        " ORDER BY created_at",
        (_now(),),
    ).fetchall()


def _next_retry_time(attempts: int) -> str:
    """ISO timestamp when a job that just failed its Nth attempt may run again."""
    from datetime import datetime, timedelta, timezone
    delay = RETRY_BACKOFF_SECONDS[min(attempts, MAX_ATTEMPTS) - 1]
    when = datetime.now(timezone.utc) + timedelta(seconds=delay)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def execute_job(conn, job, *, dry_run: bool = False) -> dict:
    """
    Execute a single runnable job. Returns a result dict with status and detail.
    Success → 'done'. Unimplemented intent → 'error' (no retry). Transient
    failure → attempts+1 with backoff (job stays runnable, gated by
    next_retry_at) until MAX_ATTEMPTS, then 'dead_letter' + ntfy alert.
    """
    job_id = job["id"]
    intent = job["intent"] or "unknown"
    payload = json.loads(job["payload_json"])

    logger.info(f'"Executing job {job_id}: intent={intent}"')

    if dry_run:
        return {"status": "dry_run", "job_id": job_id, "intent": intent}

    try:
        if intent in HASS_WRITE_INTENTS or intent in HASS_READ_INTENTS:
            # Last gate before the HA API: the entity must actually exist.
            from hass_registry import validate_home_control_payload
            validate_home_control_payload(conn, payload)
        result = _dispatch(intent, payload)
        update_job_status(conn, job_id, "done", result=result)
        logger.info(f'"Job {job_id} done"')
        return {"status": "done", "job_id": job_id, "result": result}
    except NotImplementedError as exc:
        # Retrying an unimplemented intent can never succeed — no backoff.
        logger.warning(f'"Job {job_id} skipped: {exc}"')
        update_job_status(conn, job_id, "error",
                          result={"error": str(exc), "skipped": True})
        return {"status": "skipped", "job_id": job_id, "error": str(exc)}
    except UnknownEntityError as exc:
        # Hallucinated entity — retrying can never fix it; fail immediately.
        logger.error(f'"Job {job_id} rejected: {exc}"')
        update_job_status(conn, job_id, "error",
                          result={"error": str(exc), "unknown_entity": True})
        return {"status": "rejected", "job_id": job_id, "error": str(exc)}
    except Exception as exc:
        attempts = (job["attempts"] if "attempts" in job.keys() else 0) + 1
        if attempts >= MAX_ATTEMPTS:
            logger.error(f'"Job {job_id} dead-lettered after {attempts} attempts: {exc}"')
            update_job_status(conn, job_id, "dead_letter",
                              result={"error": str(exc), "attempts": attempts})
            notify(
                f"Job {job_id} ({intent}) failed {attempts} times and was dead-lettered: {exc}\n"
                f"Review with: make dead-letters",
                title="Job dead-lettered", priority="high", tags="warning",
            )
            return {"status": "dead_letter", "job_id": job_id, "error": str(exc)}
        retry_at = _next_retry_time(attempts)
        logger.warning(
            f'"Job {job_id} attempt {attempts}/{MAX_ATTEMPTS} failed, retry at {retry_at}: {exc}"'
        )
        # Status is left runnable (approved / pending); next_retry_at gates it.
        schedule_job_retry(conn, job_id, attempts, retry_at)
        return {"status": "retry_scheduled", "job_id": job_id,
                "attempts": attempts, "next_retry_at": retry_at, "error": str(exc)}


def _dispatch(intent: str, payload: dict) -> dict:
    if intent in TODOIST_INTENTS:
        return _execute_todoist(payload)
    if intent in HASS_WRITE_INTENTS:
        return _execute_hass_write(payload)
    if intent in HASS_READ_INTENTS:
        return _execute_hass_read(payload)
    if intent in PROMPT_INTENTS:
        return _execute_prompt_revision(payload)
    if intent in UNIMPLEMENTED_INTENTS:
        raise NotImplementedError(
            f"Intent '{intent}' is not yet automated — handle manually or via n8n"
        )
    raise NotImplementedError(f"Unknown intent: {intent!r}")


def _execute_todoist(payload: dict) -> dict:
    from todoist_client import execute_approved_job
    created = execute_approved_job(payload)
    return {"todoist_task_id": created.get("id"), "title": created.get("content")}


def _execute_prompt_revision(payload: dict) -> dict:
    """
    Promote an approved candidate prompt over the live one. Deterministic file
    copy, no LLM. Both paths must resolve inside apps/orchestrator/prompts/ —
    a payload pointing anywhere else is rejected.
    """
    import shutil
    from datetime import datetime, timezone

    prompts_root = (REPO_ROOT / "apps" / "orchestrator" / "prompts").resolve()
    candidate = (REPO_ROOT / payload.get("candidate_path", "")).resolve()
    target = (REPO_ROOT / payload.get("target_path", "")).resolve()

    for p, name in ((candidate, "candidate_path"), (target, "target_path")):
        if not str(p).startswith(str(prompts_root)):
            raise ValueError(f"{name} escapes the prompts directory: {p}")
    if not candidate.is_file():
        raise ValueError(f"candidate prompt missing: {candidate}")

    archive_dir = prompts_root / "archive"
    archive_dir.mkdir(exist_ok=True)
    archived = ""
    if target.is_file():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archived = str(archive_dir / f"{target.stem}.{stamp}{target.suffix}")
        shutil.copy2(target, archived)
    shutil.copy2(candidate, target)
    return {"promoted": str(candidate), "target": str(target),
            "archived_previous": archived}


def _execute_hass_write(payload: dict) -> dict:
    from hass_client import call_service
    domain = payload.get("hass_domain", "homeassistant")
    service = payload.get("hass_service", "toggle")
    service_data = payload.get("hass_service_data", {})
    affected = call_service(domain, service, service_data)
    return {"affected_entities": len(affected), "domain": domain, "service": service}


def _execute_hass_read(payload: dict) -> dict:
    from hass_client import get_state
    entity_id = payload.get("hass_entity_id", "")
    if not entity_id:
        raise ValueError("home_control_read job missing hass_entity_id in payload")
    state = get_state(entity_id)
    return {"entity_id": entity_id, "state": state.get("state")}


def run_once(conn, *, dry_run: bool = False) -> list[dict]:
    """
    Execute all runnable jobs: human-approved ones plus pending auto-proceed
    ones (approval_required=0). High-risk intents never auto-proceed — if one
    slipped through with approval_required=0, it is re-flagged for approval
    instead of executed.
    """
    jobs = list(_get_approved_jobs(conn))

    for job in _get_auto_proceed_jobs(conn):
        if (job["intent"] or "unknown") in HIGH_RISK_INTENTS:
            logger.warning(
                f'"Job {job["id"]} intent={job["intent"]} is high-risk but was '
                f'flagged auto-proceed — re-flagging for approval"'
            )
            if not dry_run:
                conn.execute(
                    "UPDATE jobs SET approval_required = 1 WHERE id = ?",
                    (job["id"],),
                )
                conn.commit()
                payload = json.loads(job["payload_json"])
                insert_approval(
                    conn,
                    job_id=job["id"],
                    requested_action=(
                        f"High-risk intent {job['intent']!r}: "
                        f"{payload.get('title', '(no title)')}"
                    ),
                )
            continue
        jobs.append(job)

    if not jobs:
        logger.info('"No runnable jobs"')
        return []
    results = [execute_job(conn, job, dry_run=dry_run) for job in jobs]
    logger.info(f'"Processed {len(results)} job(s)"')
    return results


def run_watch(conn, interval: int = POLL_INTERVAL) -> None:
    """Poll for approved jobs on a fixed interval until interrupted."""
    logger.info(f'"Job executor watching (interval={interval}s)"')
    try:
        while True:
            run_once(conn)
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info('"Job executor stopped"')


def main() -> None:
    parser = argparse.ArgumentParser(description="Job executor")
    parser.add_argument("--watch", action="store_true",
                        help="Poll continuously instead of running once")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL,
                        help="Poll interval in seconds (with --watch)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Classify jobs without executing them")
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    if args.watch:
        run_watch(conn, interval=args.interval)
    else:
        results = run_once(conn, dry_run=args.dry_run)
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
