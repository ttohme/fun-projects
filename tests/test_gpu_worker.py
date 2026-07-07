import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

import gpu_worker
from db import open_db
from gpu_worker import build_digest, enqueue_summary, get_awaiting_jobs, run_once

DERIVED_TASK = {
    "title": "Buy paint",
    "intent": "create_task",
    "source_type": "file",
    "approval_required": False,
    "confidence": 0.9,
    "dedupe_key": "derived-key-1",
}


def fresh_db():
    return open_db(":memory:")


def _media_job(conn, tmp_path, kind="audio", name="memo.m4a"):
    from db import insert_job
    media = tmp_path / name
    media.write_bytes(b"fake media bytes")
    job_id = insert_job(
        conn, source_type="file", source_ref=f"sync/inbox/{name}",
        payload={"title": f"[{kind}] {name}", "media_kind": kind,
                 "media_path": str(media)},
        intent="document_triage", approval_required=False,
    )
    conn.execute("UPDATE jobs SET status='awaiting_gpu' WHERE id=?", (job_id,))
    conn.commit()
    return job_id, media


def test_enqueue_media_via_document_processor(tmp_path):
    from document_processor import process_file
    audio = tmp_path / "note.m4a"
    audio.write_bytes(b"audio")
    db = tmp_path / "t.db"
    with patch("document_processor.MEDIA_DIR", tmp_path / "media"):
        results = process_file(audio, db_path=db)
    assert results[0]["_status"] == "awaiting_gpu"
    assert not audio.exists()
    assert (tmp_path / "media" / "note.m4a").exists()
    conn = open_db(db)
    row = conn.execute("SELECT status FROM jobs").fetchone()
    assert row["status"] == "awaiting_gpu"


def test_awaiting_gpu_invisible_to_executor(tmp_path):
    from job_executor import run_once as executor_run_once
    conn = fresh_db()
    _media_job(conn, tmp_path)
    with patch("job_executor._dispatch") as mock:
        assert executor_run_once(conn) == []
    mock.assert_not_called()


def test_audio_job_transcribed_and_derived_task_created(tmp_path):
    conn = fresh_db()
    job_id, media = _media_job(conn, tmp_path)
    with patch("gpu_worker.transcribe_audio", return_value="buy paint this weekend"), \
         patch("triage.triage", return_value=dict(DERIVED_TASK)) as mock_triage, \
         patch("gpu_worker.PROCESSED_DIR", tmp_path / "processed"):
        results = run_once(conn)
    assert results[0]["status"] == "done"
    mock_triage.assert_called_once()
    assert mock_triage.call_args.kwargs["dry_run"] is True

    row = conn.execute("SELECT status, result_json FROM jobs WHERE id=?",
                       (job_id,)).fetchone()
    assert row["status"] == "done"
    result = json.loads(row["result_json"])
    assert result["derived_title"] == "Buy paint"
    derived = conn.execute(
        "SELECT * FROM jobs WHERE dedupe_key='derived-key-1'").fetchone()
    assert derived is not None
    assert derived["status"] == "pending"
    assert (tmp_path / "processed" / media.name).exists()


def test_unreachable_gpu_defers_job(tmp_path):
    conn = fresh_db()
    job_id, _ = _media_job(conn, tmp_path)
    with patch("gpu_worker.transcribe_audio",
               side_effect=ConnectionError("box is off")):
        results = run_once(conn)
    assert results[0]["status"] == "deferred"
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "awaiting_gpu"  # untouched, retried next run


def test_derived_gated_task_gets_approval_row(tmp_path):
    conn = fresh_db()
    _media_job(conn, tmp_path)
    gated = {**DERIVED_TASK, "intent": "send_email", "approval_required": True,
             "dedupe_key": "derived-key-2"}
    with patch("gpu_worker.transcribe_audio", return_value="email Bob about the quote"), \
         patch("triage.triage", return_value=gated), \
         patch("notifier.notify") as mock_notify, \
         patch("gpu_worker.PROCESSED_DIR", tmp_path / "processed"):
        run_once(conn)
    derived = conn.execute(
        "SELECT * FROM jobs WHERE dedupe_key='derived-key-2'").fetchone()
    approval = conn.execute(
        "SELECT * FROM approvals WHERE job_id=?", (derived["id"],)).fetchone()
    assert approval is not None


def test_image_job_falls_back_to_tesseract(tmp_path):
    conn = fresh_db()
    _media_job(conn, tmp_path, kind="image", name="receipt.jpg")
    fake_proc = type("P", (), {"returncode": 0, "stdout": "TOTAL 42.00"})()
    with patch.object(gpu_worker, "OLLAMA_BASE_URL", ""), \
         patch("gpu_worker.subprocess.run", return_value=fake_proc), \
         patch("triage.triage", return_value=dict(DERIVED_TASK)), \
         patch("gpu_worker.PROCESSED_DIR", tmp_path / "processed"):
        results = run_once(conn)
    assert results[0]["status"] == "done"


def test_summarize_job_and_weekly_digest():
    conn = fresh_db()
    job_id = enqueue_summary(conn, text="long newsletter body...", subject="AI Weekly")
    assert job_id is not None
    assert get_awaiting_jobs(conn)[0]["id"] == job_id

    with patch("gpu_worker.summarize_text", return_value="• model news\n• tooling news"):
        results = run_once(conn)
    assert results[0]["status"] == "done"

    digest = build_digest(conn, days=7)
    assert "AI Weekly" in digest and "model news" in digest


def test_enqueue_summary_dedupes():
    conn = fresh_db()
    assert enqueue_summary(conn, text="body", subject="Same Subject") is not None
    assert enqueue_summary(conn, text="body", subject="Same Subject") is None
