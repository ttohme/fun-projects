"""
apps/orchestrator/memory.py
Persistent facts + near-duplicate detection for the triage pipeline.

Facts ("gym is Tuesdays and Thursdays", "dentist is Dr. Smith, 555-0100",
"work project prefix is ACME-") are injected into triage prompts so the model
classifies with your standing context. Facts are added only via explicit CLI
(or an approval-gated capture) — the model never writes its own memory.

Near-duplicate advisor: before you approve a task, the approval review shows
"similar to job #N (82%)" using difflib over recent titles — pure CPU, works
with everything else offline. (A GPU-embedding upgrade can swap _similarity
later without touching callers.)

CLI:
    python memory.py add "gym is tuesdays and thursdays" [--topic routine]
    python memory.py list / forget <id>
"""
import argparse
import difflib
import json
import os
from pathlib import Path

from db import _now, open_db

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT NOT NULL DEFAULT 'general',
    fact       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
"""

MAX_PROMPT_FACTS = 20
SIMILARITY_THRESHOLD = 0.75


def ensure_table(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def add_fact(conn, fact: str, *, topic: str = "general") -> int | None:
    ensure_table(conn)
    try:
        cur = conn.execute(
            "INSERT INTO facts (topic, fact, created_at) VALUES (?, ?, ?)",
            (topic, fact.strip(), _now()),
        )
        conn.commit()
        return cur.lastrowid
    except Exception:
        return None  # duplicate fact


def list_facts(conn) -> list[dict]:
    ensure_table(conn)
    return [dict(r) for r in conn.execute(
        "SELECT id, topic, fact, created_at FROM facts ORDER BY topic, id")]


def forget_fact(conn, fact_id: int) -> bool:
    ensure_table(conn)
    cur = conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    conn.commit()
    return cur.rowcount > 0


def facts_block(conn) -> str:
    """
    Prompt-injectable block of standing facts ("" when there are none).
    Appended to the triage prompt by render-time callers.
    """
    facts = list_facts(conn)[:MAX_PROMPT_FACTS]
    if not facts:
        return ""
    lines = [f"- ({f['topic']}) {f['fact']}" for f in facts]
    return (
        "\n## Standing facts about the user (apply when classifying)\n"
        + "\n".join(lines) + "\n"
    )


# ── Near-duplicate advisor ────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_similar_jobs(conn, title: str, *, days: int = 30,
                      threshold: float = SIMILARITY_THRESHOLD,
                      exclude_job_id: int | None = None) -> list[dict]:
    """Recent jobs whose title resembles `title` — likely duplicates."""
    rows = conn.execute(
        """
        SELECT id, payload_json, status, created_at FROM jobs
        WHERE  created_at >= datetime('now', ?)
        ORDER  BY created_at DESC LIMIT 200
        """,
        (f"-{days} days",),
    ).fetchall()
    hits = []
    for row in rows:
        if exclude_job_id is not None and row["id"] == exclude_job_id:
            continue
        other = json.loads(row["payload_json"]).get("title", "")
        if not other:
            continue
        score = _similarity(title, other)
        if score >= threshold:
            hits.append({"job_id": row["id"], "title": other,
                         "status": row["status"],
                         "similarity": round(score, 2)})
    hits.sort(key=lambda h: -h["similarity"])
    return hits


def similar_note(conn, title: str, *, exclude_job_id: int | None = None) -> str:
    """One-line advisory for the approval review ('' when nothing similar)."""
    hits = find_similar_jobs(conn, title, exclude_job_id=exclude_job_id)
    if not hits:
        return ""
    top = hits[0]
    return (f"similar to job #{top['job_id']} \"{top['title']}\" "
            f"({int(top['similarity'] * 100)}%, {top['status']})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Assistant memory (facts)")
    sub = parser.add_subparsers(dest="command", required=True)
    add_p = sub.add_parser("add")
    add_p.add_argument("fact")
    add_p.add_argument("--topic", default="general")
    sub.add_parser("list")
    forget_p = sub.add_parser("forget")
    forget_p.add_argument("fact_id", type=int)
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        if args.command == "add":
            fid = add_fact(conn, args.fact, topic=args.topic)
            print(json.dumps({"id": fid} if fid else {"skipped": "duplicate"}))
        elif args.command == "list":
            for f in list_facts(conn):
                print(f"  #{f['id']:<4} ({f['topic']}) {f['fact']}")
        elif args.command == "forget":
            print(json.dumps({"forgotten": forget_fact(conn, args.fact_id)}))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
