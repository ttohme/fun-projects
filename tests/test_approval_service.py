import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from approval_service import (
    decide,
    format_approval_for_review,
    get_approval_by_id,
    list_pending_approvals,
)
from db import insert_approval, insert_job, open_db, update_job_status


def fresh_db():
    return open_db(":memory:")


def _make_job(conn, *, approval_required=True, source_ref="ref-001", title="Do thing"):
    job_id = insert_job(
        conn,
        source_type="email",
        source_ref=source_ref,
        payload={"title": title, "intent": "create_task",
                 "source_type": "email", "approval_required": approval_required,
                 "confidence": 0.9},
        intent="create_task",
        approval_required=approval_required,
    )
    return job_id


def _make_approval(conn, job_id, action="create_todoist_task"):
    return insert_approval(conn, job_id=job_id, requested_action=action)


# ── list_pending_approvals ────────────────────────────────────────────────────

def test_list_pending_returns_undecided():
    conn = fresh_db()
    job_id = _make_job(conn)
    _make_approval(conn, job_id)
    items = list_pending_approvals(conn)
    assert len(items) == 1
    assert items[0]["job_id"] == job_id
    assert items[0]["decision"] is None


def test_list_pending_excludes_decided():
    conn = fresh_db()
    job_id = _make_job(conn)
    approval_id = _make_approval(conn, job_id)
    decide(conn, approval_id, "approved")
    assert list_pending_approvals(conn) == []


def test_list_pending_excludes_done_jobs():
    conn = fresh_db()
    job_id = _make_job(conn)
    _make_approval(conn, job_id)
    update_job_status(conn, job_id, "done")
    # job is done but approval is undecided — still excluded because job not pending
    assert list_pending_approvals(conn) == []


def test_list_pending_multiple_jobs():
    conn = fresh_db()
    for i in range(3):
        job_id = _make_job(conn, source_ref=f"ref-{i}", title=f"Task {i}")
        _make_approval(conn, job_id)
    assert len(list_pending_approvals(conn)) == 3


# ── get_approval_by_id ────────────────────────────────────────────────────────

def test_get_approval_by_id_found():
    conn = fresh_db()
    job_id = _make_job(conn)
    approval_id = _make_approval(conn, job_id)
    row = get_approval_by_id(conn, approval_id)
    assert row is not None
    assert row["approval_id"] == approval_id
    assert row["job_id"] == job_id


def test_get_approval_by_id_missing():
    conn = fresh_db()
    assert get_approval_by_id(conn, 9999) is None


# ── decide ────────────────────────────────────────────────────────────────────

def test_decide_approved_updates_job_status():
    conn = fresh_db()
    job_id = _make_job(conn)
    approval_id = _make_approval(conn, job_id)
    result = decide(conn, approval_id, "approved")
    assert result["decision"] == "approved"
    assert result["next_status"] == "approved"
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "approved"


def test_decide_rejected_updates_job_status():
    conn = fresh_db()
    job_id = _make_job(conn)
    approval_id = _make_approval(conn, job_id)
    result = decide(conn, approval_id, "rejected", reason="too risky")
    assert result["decision"] == "rejected"
    assert result["next_status"] == "rejected"
    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["status"] == "rejected"


def test_decide_records_reason():
    conn = fresh_db()
    job_id = _make_job(conn)
    approval_id = _make_approval(conn, job_id)
    decide(conn, approval_id, "rejected", reason="not now")
    row = conn.execute("SELECT reason, decided_at FROM approvals WHERE id = ?",
                       (approval_id,)).fetchone()
    assert row["reason"] == "not now"
    assert row["decided_at"] is not None


def test_decide_invalid_decision_raises():
    conn = fresh_db()
    job_id = _make_job(conn)
    approval_id = _make_approval(conn, job_id)
    with pytest.raises(ValueError, match="approved.*rejected"):
        decide(conn, approval_id, "maybe")


def test_decide_missing_approval_raises():
    conn = fresh_db()
    with pytest.raises(ValueError, match="No approval"):
        decide(conn, 9999, "approved")


def test_decide_already_decided_raises():
    conn = fresh_db()
    job_id = _make_job(conn)
    approval_id = _make_approval(conn, job_id)
    decide(conn, approval_id, "approved")
    with pytest.raises(ValueError, match="already decided"):
        decide(conn, approval_id, "rejected")


# ── format_approval_for_review ────────────────────────────────────────────────

def test_format_approval_contains_key_fields():
    conn = fresh_db()
    job_id = _make_job(conn, title="Buy groceries")
    approval_id = _make_approval(conn, job_id, action="create_todoist_task")
    items = list_pending_approvals(conn)
    text = format_approval_for_review(items[0])
    assert "Buy groceries" in text
    assert "create_todoist_task" in text
    assert str(approval_id) in text
