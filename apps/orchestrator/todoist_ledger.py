"""
apps/orchestrator/todoist_ledger.py
Local read-model of Todoist state (the "shadow ledger").

Todoist remains the source of truth (AGENTS.md); this table exists so
analytics — stale-task nudges, weekly reviews, pattern detection — run as
local SQL instead of hammering the API. Synced by a systemd timer
(assistant-ledger.timer, every 30 min) or `make ledger-sync`.

Rows disappear from Todoist's active-task API when completed or deleted;
the sync marks those rows closed_at=now rather than deleting them, so the
ledger keeps history.

CLI:
    python todoist_ledger.py sync
    python todoist_ledger.py nudge [--days 14] [--dry-run]

Environment variables:
    TODOIST_API_TOKEN  (required for sync)
    DB_PATH            (default: db/assistant.db)
    NUDGE_STALE_DAYS   (default: 14)
"""
import argparse
import json
import os
import sys
from pathlib import Path

from db import _now, open_db
from notifier import notify

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))
STALE_DAYS = int(os.environ.get("NUDGE_STALE_DAYS", "14"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS todoist_tasks (
    task_id     TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    project_id  TEXT,
    priority    INTEGER,
    due_date    TEXT,               -- Todoist due.date (YYYY-MM-DD) or NULL
    added_at    TEXT,               -- Todoist created_at
    first_seen  TEXT NOT NULL,      -- when this ledger first saw the task
    last_seen   TEXT NOT NULL,      -- last sync where the task was still active
    closed_at   TEXT                -- set when the task stops appearing (done/deleted)
);
"""


def ensure_table(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def sync(conn, *, tasks: list[dict] | None = None) -> dict:
    """
    Upsert the current active task set; mark vanished tasks closed.
    `tasks` may be injected for tests; by default calls the Todoist REST API.
    Returns counts: {"active": n, "new": n, "closed": n}.
    """
    if tasks is None:
        import requests
        from todoist_client import TODOIST_API_BASE, _headers
        resp = requests.get(f"{TODOIST_API_BASE}/tasks", headers=_headers(), timeout=15)
        resp.raise_for_status()
        tasks = resp.json()

    ensure_table(conn)
    now = _now()
    seen_ids, new_count = set(), 0

    for task in tasks:
        task_id = str(task.get("id", ""))
        if not task_id:
            continue
        seen_ids.add(task_id)
        due = (task.get("due") or {}).get("date")
        existing = conn.execute(
            "SELECT 1 FROM todoist_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if existing is None:
            new_count += 1
        conn.execute(
            """
            INSERT INTO todoist_tasks
                (task_id, content, project_id, priority, due_date,
                 added_at, first_seen, last_seen, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE(
                        (SELECT first_seen FROM todoist_tasks WHERE task_id = ?), ?),
                    ?, NULL)
            ON CONFLICT(task_id) DO UPDATE SET
                content = excluded.content,
                project_id = excluded.project_id,
                priority = excluded.priority,
                due_date = excluded.due_date,
                last_seen = excluded.last_seen,
                closed_at = NULL
            """,
            (task_id, task.get("content", ""), task.get("project_id"),
             task.get("priority"), due, task.get("created_at"),
             task_id, now, now),
        )

    # Anything previously active that didn't appear this sync is closed.
    closed = 0
    for row in conn.execute(
        "SELECT task_id FROM todoist_tasks WHERE closed_at IS NULL"
    ).fetchall():
        if row["task_id"] not in seen_ids:
            conn.execute(
                "UPDATE todoist_tasks SET closed_at = ? WHERE task_id = ?",
                (now, row["task_id"]),
            )
            closed += 1

    conn.commit()
    return {"active": len(seen_ids), "new": new_count, "closed": closed}


def find_stale_tasks(conn, *, days: int = STALE_DAYS) -> list[dict]:
    """
    Open tasks first seen more than `days` ago with no future due date —
    the ones silently rotting at the bottom of the list.
    """
    ensure_table(conn)
    rows = conn.execute(
        """
        SELECT task_id, content, due_date, first_seen
        FROM   todoist_tasks
        WHERE  closed_at IS NULL
          AND  first_seen <= datetime('now', ?)
          AND  (due_date IS NULL OR due_date < date('now'))
        ORDER  BY first_seen
        """,
        (f"-{days} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def nudge(conn, *, days: int = STALE_DAYS, dry_run: bool = False) -> list[dict]:
    """Push a weekly stale-task nudge to the phone. Returns the stale list."""
    stale = find_stale_tasks(conn, days=days)
    if stale and not dry_run:
        lines = [f"• {t['content']}" for t in stale[:10]]
        more = f"\n…and {len(stale) - 10} more" if len(stale) > 10 else ""
        notify(
            f"{len(stale)} task(s) untouched for {days}+ days:\n"
            + "\n".join(lines) + more
            + "\n\nReschedule, keep, or drop them in Todoist.",
            title="Stale tasks",
            tags="wastebasket",
        )
    return stale


def main() -> None:
    parser = argparse.ArgumentParser(description="Todoist shadow ledger")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sync", help="Pull active tasks into the ledger")
    nudge_p = sub.add_parser("nudge", help="Push a stale-task nudge")
    nudge_p.add_argument("--days", type=int, default=STALE_DAYS)
    nudge_p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        if args.command == "sync":
            print(json.dumps(sync(conn)))
        elif args.command == "nudge":
            stale = nudge(conn, days=args.days, dry_run=args.dry_run)
            print(json.dumps({"stale": len(stale)}))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
