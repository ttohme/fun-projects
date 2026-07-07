"""
apps/orchestrator/reviews.py
Task intelligence over the local data: weekly review, capture digest,
recurring-pattern detection, project brief. Pure SQL + difflib — no LLM, no
GPU, works with everything else offline. Pushed via ntfy.

CLI:
    python reviews.py weekly   [--dry-run]    # Sunday review
    python reviews.py digest   [--dry-run]    # what entered the system this week
    python reviews.py patterns [--dry-run]    # recurring-task proposals
    python reviews.py brief                   # per-project health snapshot
"""
import argparse
import difflib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from db import open_db
from notifier import notify

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))

PATTERN_MIN_OCCURRENCES = 3
PATTERN_SIMILARITY = 0.8


def weekly_review(conn) -> str:
    """Completed / added / still-open over the last 7 days, plus pipeline health."""
    lines = ["Week in review:"]

    ledger_ok = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='todoist_tasks'").fetchone()
    if ledger_ok:
        closed = conn.execute(
            "SELECT COUNT(*) c FROM todoist_tasks"
            " WHERE closed_at >= datetime('now', '-7 days')").fetchone()["c"]
        added = conn.execute(
            "SELECT COUNT(*) c FROM todoist_tasks"
            " WHERE first_seen >= datetime('now', '-7 days')").fetchone()["c"]
        open_n = conn.execute(
            "SELECT COUNT(*) c FROM todoist_tasks WHERE closed_at IS NULL"
        ).fetchone()["c"]
        lines.append(f"☑ {closed} task(s) completed, {added} added, {open_n} open")

    jobs = conn.execute(
        """
        SELECT status, COUNT(*) c FROM jobs
        WHERE created_at >= datetime('now', '-7 days')
        GROUP BY status
        """
    ).fetchall()
    if jobs:
        parts = ", ".join(f"{r['c']} {r['status']}" for r in jobs)
        lines.append(f"⚙ Pipeline: {parts}")
    dead = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE status='dead_letter'").fetchone()["c"]
    if dead:
        lines.append(f"⚠ {dead} dead-lettered job(s) need attention (make dead-letters)")
    pending = conn.execute(
        "SELECT COUNT(*) c FROM jobs j JOIN approvals a ON a.job_id=j.id"
        " WHERE j.status='pending' AND a.decision IS NULL").fetchone()["c"]
    if pending:
        lines.append(f"✋ {pending} approval(s) waiting (make approve)")
    return "\n".join(lines)


def capture_digest(conn) -> str:
    """What entered the system this week, by source and intent."""
    rows = conn.execute(
        """
        SELECT source_type, intent, payload_json FROM jobs
        WHERE created_at >= datetime('now', '-7 days')
        ORDER BY created_at
        """
    ).fetchall()
    if not rows:
        return "Nothing captured this week."
    by_source = Counter(r["source_type"] for r in rows)
    by_intent = Counter(r["intent"] or "unknown" for r in rows)
    lines = [f"{len(rows)} item(s) captured this week:"]
    lines.append("By source: " + ", ".join(f"{k} {v}" for k, v in by_source.most_common()))
    lines.append("By intent: " + ", ".join(f"{k} {v}" for k, v in by_intent.most_common()))
    titles = [json.loads(r["payload_json"]).get("title", "") for r in rows[-6:]]
    lines.append("Latest: " + "; ".join(t for t in titles if t))
    return "\n".join(lines)


def recurring_patterns(conn) -> list[dict]:
    """
    Closed-task titles that keep coming back — candidates for a real Todoist
    recurrence. Greedy difflib clustering over the ledger (no LLM).
    """
    ledger_ok = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='todoist_tasks'").fetchone()
    if not ledger_ok:
        return []
    titles = [r["content"] for r in conn.execute(
        "SELECT content FROM todoist_tasks WHERE closed_at IS NOT NULL")]
    clusters: list[list[str]] = []
    for title in titles:
        for cluster in clusters:
            if difflib.SequenceMatcher(None, title.lower(),
                                       cluster[0].lower()).ratio() >= PATTERN_SIMILARITY:
                cluster.append(title)
                break
        else:
            clusters.append([title])
    return [
        {"example": c[0], "occurrences": len(c),
         "proposal": f'"{c[0]}" completed {len(c)}× — set a real recurrence in Todoist?'}
        for c in clusters if len(c) >= PATTERN_MIN_OCCURRENCES
    ]


def project_brief(conn) -> str:
    """Open/closed counts per Todoist project (from the shadow ledger)."""
    ledger_ok = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='todoist_tasks'").fetchone()
    if not ledger_ok:
        return "No ledger yet — run `make ledger-sync` first."
    stats: dict[str, dict] = defaultdict(lambda: {"open": 0, "closed": 0})
    for r in conn.execute(
            "SELECT project_id, closed_at FROM todoist_tasks"):
        key = r["project_id"] or "(no project)"
        stats[key]["closed" if r["closed_at"] else "open"] += 1
    lines = ["Project brief:"]
    for project, s in sorted(stats.items(), key=lambda kv: -kv[1]["open"]):
        lines.append(f"  {project}: {s['open']} open, {s['closed']} done")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Task intelligence reviews")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("weekly", "digest", "patterns", "brief"):
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        if args.command == "weekly":
            text, title = weekly_review(conn), "Weekly review"
        elif args.command == "digest":
            text, title = capture_digest(conn), "Capture digest"
        elif args.command == "patterns":
            props = recurring_patterns(conn)
            text = ("\n".join(p["proposal"] for p in props)
                    if props else "No recurring patterns detected.")
            title = "Recurring-task proposals"
        else:
            text, title = project_brief(conn), ""

        if args.command == "brief" or getattr(args, "dry_run", False):
            print(text)
        else:
            notify(text, title=title, tags="bar_chart") or print(text)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
