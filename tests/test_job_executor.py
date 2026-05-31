import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from db import insert_approval, insert_job, open_db, update_job_status
from job_executor import _dispatch, execute_job, run_once


def fresh_db():
    return open_db(":memory:")


def _approved_job(conn, intent="create_task", source_ref="ref-001", payload_extra=None):
    """Insert a job, insert approval, set both to approved."""
    payload = {
        "title": "Test task",
        "intent": intent,
        "source_type": "email",
        "approval_required": True,
        "confidence": 0.9,
        **(payload_extra or {}),
    }
    job_id = insert_job(conn, source_type="email", source_ref=source_ref,
                        payload=payload, intent=intent, approval_required=True)
    approval_id = insert_approval(conn, job_id=job_id,
                                  requested_action="create_todoist_task")
    # Simulate approval recorded
    update_job_status(conn, job_id, "approved")
    conn.execute(
        "UPDATE approvals SET decision='approved', decided_at=datetime('now') WHERE id=?",
        (approval_id,),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row


# ── run_once ──────────────────────────────────────────────────────────────────

def test_run_once_no_jobs_returns_empty():
    conn = fresh_db()
    results = run_once(conn)
    assert results == []


def test_run_once_dry_run_does_not_change_status():
    conn = fresh_db()
    job = _approved_job(conn)
    results = run_once(conn, dry_run=True)
    assert results[0]["status"] == "dry_run"
    row = conn.execute("SELECT status FROM jobs WHERE id=?",
                       (job["id"],)).fetchone()
    assert row["status"] == "approved"  # unchanged


def test_run_once_processes_all_approved():
    conn = fresh_db()
    _approved_job(conn, source_ref="r1")
    _approved_job(conn, source_ref="r2")
    with patch("job_executor._dispatch", return_value={"ok": True}):
        results = run_once(conn)
    assert len(results) == 2
    assert all(r["status"] == "done" for r in results)


# ── execute_job ───────────────────────────────────────────────────────────────

def test_execute_job_marks_done_on_success():
    conn = fresh_db()
    job = _approved_job(conn)
    with patch("job_executor._dispatch", return_value={"todoist_task_id": "t-1"}):
        result = execute_job(conn, job)
    assert result["status"] == "done"
    row = conn.execute("SELECT status, result_json FROM jobs WHERE id=?",
                       (job["id"],)).fetchone()
    assert row["status"] == "done"
    assert "todoist_task_id" in row["result_json"]


def test_execute_job_marks_error_on_exception():
    conn = fresh_db()
    job = _approved_job(conn)
    with patch("job_executor._dispatch", side_effect=Exception("API down")):
        result = execute_job(conn, job)
    assert result["status"] == "error"
    assert "API down" in result["error"]
    row = conn.execute("SELECT status FROM jobs WHERE id=?",
                       (job["id"],)).fetchone()
    assert row["status"] == "error"


def test_execute_job_marks_skipped_on_not_implemented():
    conn = fresh_db()
    job = _approved_job(conn, intent="send_email")
    result = execute_job(conn, job)
    assert result["status"] == "skipped"
    row = conn.execute("SELECT status FROM jobs WHERE id=?",
                       (job["id"],)).fetchone()
    assert row["status"] == "error"


# ── _dispatch routing ─────────────────────────────────────────────────────────

def test_dispatch_todoist_intent_calls_todoist():
    with patch("job_executor._execute_todoist", return_value={"todoist_task_id": "x"}) as mock:
        _dispatch("create_task", {"title": "Buy milk"})
    mock.assert_called_once()


def test_dispatch_hass_write_calls_hass():
    with patch("job_executor._execute_hass_write", return_value={"affected_entities": 1}) as mock:
        _dispatch("home_control_write", {"hass_domain": "light", "hass_service": "turn_on"})
    mock.assert_called_once()


def test_dispatch_hass_read_calls_hass_read():
    with patch("job_executor._execute_hass_read", return_value={"state": "on"}) as mock:
        _dispatch("home_control_read", {"hass_entity_id": "light.x"})
    mock.assert_called_once()


def test_dispatch_send_email_raises_not_implemented():
    with pytest.raises(NotImplementedError, match="send_email"):
        _dispatch("send_email", {})


def test_dispatch_code_job_raises_not_implemented():
    with pytest.raises(NotImplementedError, match="code_job"):
        _dispatch("code_job", {})


def test_dispatch_unknown_intent_raises_not_implemented():
    with pytest.raises(NotImplementedError, match="Unknown intent"):
        _dispatch("mystery_intent", {})


def test_dispatch_document_triage_routes_to_todoist():
    with patch("job_executor._execute_todoist", return_value={}) as mock:
        _dispatch("document_triage", {"title": "File review"})
    mock.assert_called_once()
