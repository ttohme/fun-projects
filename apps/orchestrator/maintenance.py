"""
apps/orchestrator/maintenance.py
Weekly repo-maintenance code job: enqueues an approval-gated code_job that
OpenHands runs in its sandbox whenever the Windows box is next up.

code_job is HIGH_RISK (arbitrary code execution), so the enqueued job always
waits for your one-tap approval — the Saturday push asks, you tap, it runs.
The job's deliverable is a REPORT; any actual change goes through a PR.

CLI:
    python maintenance.py enqueue [--repo owner/name] [--task "custom text"]
"""
import argparse
import json
import os
from pathlib import Path

from db import insert_approval, insert_job, make_dedupe_key, open_db, _now
from notifier import notify_approval_needed

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))

DEFAULT_TASK = """Run a maintenance review of this repository and produce a REPORT (no direct
changes — anything actionable becomes a proposed PR):
1. Dependency audit: outdated or vulnerable packages in requirements*.txt
2. Test health: flaky or slow tests, coverage gaps in recently changed modules
3. TODO/FIXME sweep: list them with file:line and a one-line triage
4. Dead code: unused imports/functions worth removing
Summarize findings ranked by impact."""


def enqueue(conn, *, task: str = "", repo: str = "") -> dict:
    description = task or DEFAULT_TASK
    week = _now()[:10]  # one maintenance job per day max via dedupe
    payload = {
        "title": f"Weekly repo maintenance ({week})",
        "intent": "code_job",
        "source_type": "webhook",
        "approval_required": True,
        "confidence": 1.0,
        "description": description,
        "repo": repo,
        "dedupe_key": make_dedupe_key(f"maintenance:{week}", "repo-maintenance"),
    }
    job_id = insert_job(
        conn, source_type="webhook", source_ref=f"maintenance:{week}",
        payload=payload, intent="code_job", approval_required=True,
    )
    if job_id is None:
        return {"skipped": "already enqueued today"}
    approval_id = insert_approval(
        conn, job_id=job_id,
        requested_action=f"Run weekly repo maintenance in the OpenHands sandbox ({week})",
    )
    notify_approval_needed(payload, approval_id)
    return {"job_id": job_id, "approval_id": approval_id}


def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue the weekly maintenance code job")
    sub = parser.add_subparsers(dest="command", required=True)
    enq = sub.add_parser("enqueue")
    enq.add_argument("--repo", default="")
    enq.add_argument("--task", default="")
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        print(json.dumps(enqueue(conn, task=args.task, repo=args.repo)))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
