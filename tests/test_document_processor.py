import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from db import open_db
from document_processor import (
    CONFIDENCE_THRESHOLD,
    call_document_agent,
    process_file,
    read_file_text,
)

GOOD_TASK = {
    "title": "Review insurance renewal",
    "intent": "create_task",
    "source_type": "file",
    "approval_required": False,
    "confidence": 0.88,
}


def _mock_agent(tasks):
    return patch("document_processor.call_document_agent", return_value=tasks)


# ── read_file_text ────────────────────────────────────────────────────────────

def test_read_text_file(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("Hello world")
    assert read_file_text(f) == "Hello world"

def test_read_unsupported_file(tmp_path):
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG")
    result = read_file_text(f)
    assert "Unsupported" in result

def test_read_pdf_placeholder(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")
    result = read_file_text(f)
    assert "PDF" in result


# ── process_file (dry_run) ────────────────────────────────────────────────────

def test_process_file_dry_run_returns_tasks(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("Renew insurance.")
    with _mock_agent([GOOD_TASK]):
        results = process_file(f, dry_run=True)
    assert len(results) == 1
    assert results[0]["title"] == "Review insurance renewal"
    assert "_job_id" not in results[0]
    assert f.exists()  # file not moved in dry_run


def test_process_file_writes_jobs(tmp_path):
    db = tmp_path / "test.db"
    f = tmp_path / "note.txt"
    f.write_text("Renew insurance.")
    with _mock_agent([GOOD_TASK]):
        results = process_file(f, db_path=db)
    assert "_job_id" in results[0]
    conn = open_db(db)
    row = conn.execute("SELECT intent FROM jobs WHERE id = ?",
                       (results[0]["_job_id"],)).fetchone()
    assert row["intent"] == "create_task"


def test_process_file_moves_to_processed(tmp_path):
    processed = tmp_path / "processed"
    processed.mkdir()
    f = tmp_path / "note.txt"
    f.write_text("Pay the water bill by Friday.")
    db = tmp_path / "test.db"
    with _mock_agent([GOOD_TASK]), \
         patch("document_processor.PROCESSED_DIR", processed), \
         patch("document_processor.REJECTED_DIR", tmp_path / "rejected"):
        process_file(f, db_path=db)
    assert not f.exists()
    assert (processed / "note.txt").exists()


def test_process_file_low_confidence_skipped(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("something vague")
    low_conf_task = {**GOOD_TASK, "confidence": CONFIDENCE_THRESHOLD - 0.01}
    with _mock_agent([low_conf_task]):
        results = process_file(f, dry_run=True)
    assert results[0].get("_skipped") == "low_confidence"
    assert "_job_id" not in results[0]


def test_process_file_multiple_tasks(tmp_path):
    f = tmp_path / "report.txt"
    f.write_text("Pay electricity bill. Also schedule annual review.")
    db = tmp_path / "test.db"
    task2 = {**GOOD_TASK, "title": "Schedule annual review",
             "dedupe_key": "different-key"}
    with _mock_agent([GOOD_TASK, task2]):
        results = process_file(f, db_path=db)
    assert len(results) == 2
    assert all("_job_id" in r for r in results)


def test_process_file_agent_error_goes_to_rejected(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("content")
    rejected = tmp_path / "rejected"
    rejected.mkdir()
    db = tmp_path / "test.db"
    with patch("document_processor.call_document_agent",
               side_effect=Exception("LiteLLM timeout")), \
         patch("document_processor.REJECTED_DIR", rejected):
        results = process_file(f, db_path=db)
    assert results[0].get("_skipped") == "parse_failure"
    assert (rejected / "note.txt").exists()


def test_process_file_high_risk_adds_approval(tmp_path):
    db = tmp_path / "test.db"
    f = tmp_path / "note.txt"
    f.write_text("Please send a follow-up email.")
    send_task = {**GOOD_TASK, "intent": "send_email", "approval_required": False}
    with _mock_agent([send_task]):
        results = process_file(f, db_path=db)
    assert results[0]["approval_required"] is True
    assert "_approval_id" in results[0]
