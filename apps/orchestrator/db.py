"""
apps/orchestrator/db.py
SQLite helpers for the jobs and approvals tables.

All functions accept an explicit `conn` so callers control transactions
and tests can inject an in-memory connection.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def make_dedupe_key(source_ref: str, title: str) -> str:
    raw = f"{source_ref}|{title}"
    return hashlib.sha256(raw.encode()).hexdigest()


def insert_job(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    source_ref: str,
    payload: dict[str, Any],
    intent: str | None = None,
    approval_required: bool = False,
) -> int | None:
    """Insert a job row. Returns the new row id, or None if dedupe_key already exists."""
    title = payload.get("title", "")
    dedupe_key = payload.get("dedupe_key") or make_dedupe_key(source_ref, title)
    now = _now()
    try:
        cur = conn.execute(
            """
            INSERT INTO jobs
                (source_type, source_ref, dedupe_key, status, intent,
                 payload_json, approval_required, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                source_type,
                source_ref,
                dedupe_key,
                intent,
                json.dumps(payload),
                1 if approval_required else 0,
                now,
                now,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None  # duplicate


def update_job_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    result: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, result_json = ? WHERE id = ?",
        (status, json.dumps(result) if result is not None else None, job_id),
    )
    conn.commit()


def get_job_by_dedupe_key(conn: sqlite3.Connection, dedupe_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE dedupe_key = ?", (dedupe_key,)
    ).fetchone()


def get_pending_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at"
    ).fetchall()


def insert_approval(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    requested_action: str,
    reviewer: str | None = None,
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO approvals (job_id, requested_action, reviewer, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (job_id, requested_action, reviewer, now),
    )
    conn.commit()
    return cur.lastrowid


def record_approval_decision(
    conn: sqlite3.Connection,
    approval_id: int,
    decision: str,
    reason: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE approvals
        SET decision = ?, reason = ?, decided_at = ?
        WHERE id = ?
        """,
        (decision, reason, _now(), approval_id),
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
