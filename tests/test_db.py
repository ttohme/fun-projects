import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from db import (
    get_job_by_dedupe_key,
    get_pending_jobs,
    insert_approval,
    insert_job,
    make_dedupe_key,
    open_db,
    record_approval_decision,
    update_job_status,
)


def fresh_db():
    conn = open_db(":memory:")
    return conn


# ── make_dedupe_key ───────────────────────────────────────────────────────────

def test_dedupe_key_is_deterministic():
    assert make_dedupe_key("ref1", "Buy milk") == make_dedupe_key("ref1", "Buy milk")

def test_dedupe_key_differs_for_different_refs():
    assert make_dedupe_key("ref1", "Buy milk") != make_dedupe_key("ref2", "Buy milk")

def test_dedupe_key_is_64_hex_chars():
    key = make_dedupe_key("ref", "title")
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


# ── insert_job ────────────────────────────────────────────────────────────────

def test_insert_job_returns_id():
    conn = fresh_db()
    job_id = insert_job(conn, source_type="email", source_ref="msg-001",
                        payload={"title": "Test task"})
    assert isinstance(job_id, int) and job_id > 0

def test_insert_job_duplicate_returns_none():
    conn = fresh_db()
    payload = {"title": "Test task", "dedupe_key": "fixed-key"}
    insert_job(conn, source_type="email", source_ref="msg-001", payload=payload)
    result = insert_job(conn, source_type="email", source_ref="msg-001", payload=payload)
    assert result is None

def test_insert_job_default_status_is_pending():
    conn = fresh_db()
    job_id = insert_job(conn, source_type="file", source_ref="file-001",
                        payload={"title": "Doc task"})
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "pending"

def test_insert_job_approval_required_stored():
    conn = fresh_db()
    job_id = insert_job(conn, source_type="email", source_ref="msg-002",
                        payload={"title": "Risky task"}, approval_required=True)
    row = conn.execute("SELECT approval_required FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["approval_required"] == 1

def test_insert_job_stores_intent():
    conn = fresh_db()
    job_id = insert_job(conn, source_type="voice", source_ref="voice-001",
                        payload={"title": "Voice task"}, intent="create_task")
    row = conn.execute("SELECT intent FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["intent"] == "create_task"


# ── update_job_status ────────────────────────────────────────────────────────

def test_update_status_changes_row():
    conn = fresh_db()
    job_id = insert_job(conn, source_type="email", source_ref="msg-003",
                        payload={"title": "Update me"})
    update_job_status(conn, job_id, "done", result={"todoist_id": "abc"})
    row = conn.execute("SELECT status, result_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "done"
    assert "todoist_id" in row["result_json"]

def test_update_status_without_result():
    conn = fresh_db()
    job_id = insert_job(conn, source_type="email", source_ref="msg-004",
                        payload={"title": "No result"})
    update_job_status(conn, job_id, "error")
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "error"


# ── get_job_by_dedupe_key ─────────────────────────────────────────────────────

def test_get_job_by_dedupe_key_found():
    conn = fresh_db()
    key = make_dedupe_key("ref-x", "Find me")
    insert_job(conn, source_type="email", source_ref="ref-x",
               payload={"title": "Find me", "dedupe_key": key})
    row = get_job_by_dedupe_key(conn, key)
    assert row is not None
    assert row["dedupe_key"] == key

def test_get_job_by_dedupe_key_missing():
    conn = fresh_db()
    assert get_job_by_dedupe_key(conn, "nonexistent") is None


# ── get_pending_jobs ──────────────────────────────────────────────────────────

def test_get_pending_jobs_returns_pending_only():
    conn = fresh_db()
    id1 = insert_job(conn, source_type="email", source_ref="p1",
                     payload={"title": "Pending one"})
    id2 = insert_job(conn, source_type="email", source_ref="p2",
                     payload={"title": "Pending two"})
    update_job_status(conn, id2, "done")
    pending = get_pending_jobs(conn)
    ids = [r["id"] for r in pending]
    assert id1 in ids
    assert id2 not in ids

def test_get_pending_jobs_empty():
    conn = fresh_db()
    assert get_pending_jobs(conn) == []


# ── insert_approval / record_approval_decision ────────────────────────────────

def test_insert_approval_returns_id():
    conn = fresh_db()
    job_id = insert_job(conn, source_type="email", source_ref="app-001",
                        payload={"title": "Needs approval"})
    approval_id = insert_approval(conn, job_id=job_id,
                                  requested_action="create_todoist_task")
    assert isinstance(approval_id, int) and approval_id > 0

def test_approval_decision_approved():
    conn = fresh_db()
    job_id = insert_job(conn, source_type="email", source_ref="app-002",
                        payload={"title": "Approve me"})
    approval_id = insert_approval(conn, job_id=job_id,
                                  requested_action="send_email")
    record_approval_decision(conn, approval_id, "approved", reason="looks good")
    row = conn.execute("SELECT decision, reason, decided_at FROM approvals WHERE id = ?",
                       (approval_id,)).fetchone()
    assert row["decision"] == "approved"
    assert row["reason"] == "looks good"
    assert row["decided_at"] is not None

def test_approval_decision_rejected():
    conn = fresh_db()
    job_id = insert_job(conn, source_type="email", source_ref="app-003",
                        payload={"title": "Reject me"})
    approval_id = insert_approval(conn, job_id=job_id,
                                  requested_action="delete_task")
    record_approval_decision(conn, approval_id, "rejected")
    row = conn.execute("SELECT decision FROM approvals WHERE id = ?",
                       (approval_id,)).fetchone()
    assert row["decision"] == "rejected"
