"""
apps/orchestrator/approval_service.py
Surfaces pending approvals and records decisions.

Pending jobs with approval_required=1 land here. The service formats each
pending approval for review, accepts a decision (approved/rejected), updates
the approvals row, then either queues the job for execution or marks it done.

CLI usage (interactive review loop):
    python approval_service.py review          # review one at a time
    python approval_service.py list            # list all pending approvals
    python approval_service.py decide <id> approved [--reason "..."]
    python approval_service.py decide <id> rejected [--reason "..."]

Environment variables:
    DB_PATH  (default: db/assistant.db)
"""
import argparse
import json
import os
import sys
from pathlib import Path

from db import (
    get_pending_jobs,
    insert_approval,
    open_db,
    record_approval_decision,
    update_job_status,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))


# ── Core service functions ────────────────────────────────────────────────────

def list_pending_approvals(conn) -> list[dict]:
    """Return all jobs that are pending and require approval, with their approval rows."""
    rows = conn.execute(
        """
        SELECT j.id        AS job_id,
               j.source_type,
               j.source_ref,
               j.intent,
               j.payload_json,
               j.created_at,
               a.id        AS approval_id,
               a.requested_action,
               a.reviewer,
               a.decision,
               a.created_at AS approval_requested_at
        FROM   jobs j
        JOIN   approvals a ON a.job_id = j.id
        WHERE  j.status           = 'pending'
          AND  j.approval_required = 1
          AND  a.decision          IS NULL
        ORDER  BY j.created_at
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_approval_by_id(conn, approval_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT j.id AS job_id, j.payload_json, j.source_type, j.intent,
               a.id AS approval_id, a.requested_action, a.decision
        FROM   approvals a
        JOIN   jobs j ON j.id = a.job_id
        WHERE  a.id = ?
        """,
        (approval_id,),
    ).fetchone()
    return dict(row) if row else None


def decide(
    conn,
    approval_id: int,
    decision: str,
    reason: str | None = None,
) -> dict:
    """
    Record a decision on an approval row and update the parent job status.

    Returns a result dict with job_id, decision, and next_status.
    Raises ValueError if the approval is not found or already decided.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f"decision must be 'approved' or 'rejected', got {decision!r}")

    row = get_approval_by_id(conn, approval_id)
    if row is None:
        raise ValueError(f"No approval found with id={approval_id}")
    if row["decision"] is not None:
        raise ValueError(
            f"Approval {approval_id} already decided: {row['decision']}"
        )

    record_approval_decision(conn, approval_id, decision, reason=reason)

    next_status = "approved" if decision == "approved" else "rejected"
    update_job_status(conn, row["job_id"], next_status)

    return {
        "approval_id": approval_id,
        "job_id": row["job_id"],
        "decision": decision,
        "next_status": next_status,
        "requested_action": row["requested_action"],
    }


def list_dead_letters(conn) -> list[dict]:
    """Jobs that exhausted their retries and need human attention."""
    rows = conn.execute(
        """
        SELECT id AS job_id, source_type, source_ref, intent,
               payload_json, result_json, attempts, updated_at
        FROM   jobs
        WHERE  status = 'dead_letter'
        ORDER  BY updated_at
        """
    ).fetchall()
    return [dict(r) for r in rows]


def retry_dead_letter(conn, job_id: int) -> dict:
    """
    Reset a dead-lettered job so the executor picks it up again.
    Raises ValueError if the job is not in dead_letter state.
    """
    row = conn.execute(
        "SELECT id, status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No job found with id={job_id}")
    if row["status"] != "dead_letter":
        raise ValueError(f"Job {job_id} is {row['status']!r}, not dead_letter")

    conn.execute(
        "UPDATE jobs SET status = 'approved', attempts = 0, next_retry_at = NULL"
        " WHERE id = ?",
        (job_id,),
    )
    conn.commit()
    return {"job_id": job_id, "status": "approved", "attempts": 0}


def format_approval_for_review(item: dict) -> str:
    """Render a pending approval as a human-readable string for CLI review."""
    payload = json.loads(item.get("payload_json", "{}"))
    lines = [
        f"  Approval ID : {item['approval_id']}",
        f"  Job ID      : {item['job_id']}",
        f"  Action      : {item['requested_action']}",
        f"  Source      : {item['source_type']} — {item['source_ref']}",
        f"  Intent      : {item['intent']}",
        f"  Title       : {payload.get('title', '(no title)')}",
        f"  Project     : {payload.get('project', 'Inbox')}",
        f"  Due         : {payload.get('due_string') or 'not set'}",
        f"  Priority    : {payload.get('priority', 4)}",
        f"  Labels      : {', '.join(payload.get('labels', [])) or 'none'}",
        f"  Confidence  : {payload.get('confidence', '?')}",
        f"  Requested   : {item['approval_requested_at']}",
    ]
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def cmd_list(args, conn) -> None:
    items = list_pending_approvals(conn)
    if not items:
        print("No pending approvals.")
        return
    print(f"{len(items)} pending approval(s):\n")
    for item in items:
        print(format_approval_for_review(item))
        print()


def cmd_decide(args, conn) -> None:
    result = decide(conn, args.approval_id, args.decision, reason=args.reason)
    print(json.dumps(result, indent=2))


def cmd_review(args, conn) -> None:
    """Interactive one-at-a-time approval review."""
    items = list_pending_approvals(conn)
    if not items:
        print("No pending approvals.")
        return

    for item in items:
        print("\n" + "─" * 60)
        print(format_approval_for_review(item))
        print("─" * 60)
        while True:
            choice = input("  Decision [a=approve / r=reject / s=skip / q=quit]: ").strip().lower()
            if choice == "q":
                return
            if choice == "s":
                break
            if choice in ("a", "r"):
                reason = input("  Reason (optional): ").strip() or None
                decision = "approved" if choice == "a" else "rejected"
                result = decide(conn, item["approval_id"], decision, reason=reason)
                print(f"  → Recorded: {result['decision']} (job {result['job_id']} → {result['next_status']})")
                break
            print("  Please enter a, r, s, or q.")


def cmd_dead_letters(args, conn) -> None:
    items = list_dead_letters(conn)
    if not items:
        print("No dead-lettered jobs.")
        return
    print(f"{len(items)} dead-lettered job(s):\n")
    for item in items:
        payload = json.loads(item.get("payload_json") or "{}")
        result = json.loads(item.get("result_json") or "{}")
        print(f"  Job ID   : {item['job_id']}")
        print(f"  Intent   : {item['intent']}")
        print(f"  Title    : {payload.get('title', '(no title)')}")
        print(f"  Attempts : {item['attempts']}")
        print(f"  Error    : {result.get('error', '?')}")
        print(f"  Source   : {item['source_type']} — {item['source_ref']}")
        print(f"  Retry    : python approval_service.py retry {item['job_id']}")
        print()


def cmd_retry(args, conn) -> None:
    result = retry_dead_letter(conn, args.job_id)
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Approval service")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all pending approvals")

    review_p = sub.add_parser("review", help="Interactive approval review")

    decide_p = sub.add_parser("decide", help="Record a decision on an approval")
    decide_p.add_argument("approval_id", type=int)
    decide_p.add_argument("decision", choices=["approved", "rejected"])
    decide_p.add_argument("--reason", default=None)

    sub.add_parser("dead-letters", help="List jobs that exhausted their retries")

    retry_p = sub.add_parser("retry", help="Reset a dead-lettered job for retry")
    retry_p.add_argument("job_id", type=int)

    args = parser.parse_args()
    conn = open_db(DB_PATH)

    if args.command == "list":
        cmd_list(args, conn)
    elif args.command == "decide":
        cmd_decide(args, conn)
    elif args.command == "review":
        cmd_review(args, conn)
    elif args.command == "dead-letters":
        cmd_dead_letters(args, conn)
    elif args.command == "retry":
        cmd_retry(args, conn)


if __name__ == "__main__":
    main()
