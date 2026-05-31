import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from db import open_db
from voice_intake import capture

GOOD_TASK = {
    "title": "Call the plumber",
    "intent": "create_task",
    "source_type": "voice",
    "approval_required": False,
    "confidence": 0.95,
}


def _mock_agent(task):
    return patch("voice_intake.call_drafting_agent", return_value=task)


# ── capture (dry_run) ─────────────────────────────────────────────────────────

def test_capture_dry_run_returns_task():
    with _mock_agent(GOOD_TASK):
        result = capture(description="Call the plumber", dry_run=True)
    assert result["title"] == "Call the plumber"
    assert "_job_id" not in result


def test_capture_sets_source_type_voice():
    with _mock_agent(GOOD_TASK):
        result = capture(description="Call the plumber", dry_run=True)
    assert result["source_type"] == "voice"


def test_capture_writes_job_and_approval(tmp_path):
    db = tmp_path / "test.db"
    with _mock_agent(GOOD_TASK):
        result = capture(description="Call the plumber", db_path=db)
    assert "_job_id" in result
    assert "_approval_id" in result
    conn = open_db(db)
    job = conn.execute("SELECT * FROM jobs WHERE id = ?",
                       (result["_job_id"],)).fetchone()
    assert job["source_type"] == "voice"
    assert job["status"] == "pending"
    approval = conn.execute("SELECT * FROM approvals WHERE id = ?",
                            (result["_approval_id"],)).fetchone()
    assert approval["decision"] is None  # pending


def test_capture_always_creates_approval_even_low_risk(tmp_path):
    db = tmp_path / "test.db"
    safe_task = {**GOOD_TASK, "intent": "create_task", "approval_required": False}
    with _mock_agent(safe_task):
        result = capture(description="Buy milk", db_path=db)
    assert "_approval_id" in result


def test_capture_duplicate_returns_skipped_flag(tmp_path):
    db = tmp_path / "test.db"
    task = {**GOOD_TASK, "dedupe_key": "fixed-key-voice"}
    with _mock_agent(task):
        capture(description="Buy milk", source_ref="v-001", db_path=db)
        result = capture(description="Buy milk", source_ref="v-001", db_path=db)
    assert result.get("_skipped") == "duplicate"


def test_capture_send_email_forces_approval(tmp_path):
    db = tmp_path / "test.db"
    task = {**GOOD_TASK, "intent": "send_email", "approval_required": False}
    with _mock_agent(task):
        result = capture(description="Send reply to John", db_path=db)
    assert result["approval_required"] is True


def test_capture_low_confidence_forces_approval(tmp_path):
    db = tmp_path / "test.db"
    task = {**GOOD_TASK, "confidence": 0.5, "approval_required": False}
    with _mock_agent(task):
        result = capture(description="maybe do something tomorrow", db_path=db)
    assert result["approval_required"] is True


def test_capture_invalid_task_raises(tmp_path):
    from schema_validator import ValidationError
    bad = {"title": "Incomplete"}  # missing required fields
    with _mock_agent(bad):
        try:
            capture(description="x", dry_run=True)
            assert False, "Should have raised"
        except (ValidationError, Exception) as e:
            assert "invalid" in str(e).lower() or "missing" in str(e).lower()
