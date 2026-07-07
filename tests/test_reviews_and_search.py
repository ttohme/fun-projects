import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from db import insert_job, open_db
from doc_search import index_file, index_text, reindex, search
from reviews import capture_digest, project_brief, recurring_patterns, weekly_review
from todoist_ledger import sync as ledger_sync


def fresh_db():
    return open_db(":memory:")


# ── reviews ───────────────────────────────────────────────────────────────────

def _task(i, content, closed=False, project="p1"):
    return {"id": str(i), "content": content, "project_id": project,
            "priority": 1, "due": None, "created_at": "2026-01-01T00:00:00Z"}


def test_weekly_review_counts_ledger_and_pipeline():
    conn = fresh_db()
    ledger_sync(conn, tasks=[_task(1, "Buy milk"), _task(2, "Fix bike")])
    ledger_sync(conn, tasks=[_task(1, "Buy milk")])  # 2 closed this week
    insert_job(conn, source_type="email", source_ref="r1",
               payload={"title": "T"}, intent="create_task")
    text = weekly_review(conn)
    assert "1 task(s) completed" in text
    assert "Pipeline" in text


def test_capture_digest_groups_by_source_and_intent():
    conn = fresh_db()
    insert_job(conn, source_type="email", source_ref="a",
               payload={"title": "Task A"}, intent="create_task")
    insert_job(conn, source_type="voice", source_ref="b",
               payload={"title": "Task B"}, intent="create_task")
    text = capture_digest(conn)
    assert "2 item(s)" in text
    assert "email 1" in text and "voice 1" in text
    assert "Task B" in text


def test_capture_digest_empty():
    assert "Nothing captured" in capture_digest(fresh_db())


def test_recurring_patterns_cluster_similar_closed_tasks():
    conn = fresh_db()
    tasks = [_task(i, "Water the plants") for i in range(3)]
    tasks += [_task(10, "One-off thing")]
    ledger_sync(conn, tasks=tasks)
    ledger_sync(conn, tasks=[])  # close everything
    props = recurring_patterns(conn)
    assert len(props) == 1
    assert props[0]["occurrences"] == 3
    assert "Water the plants" in props[0]["proposal"]


def test_project_brief_counts_per_project():
    conn = fresh_db()
    ledger_sync(conn, tasks=[_task(1, "A", project="work"),
                             _task(2, "B", project="home")])
    ledger_sync(conn, tasks=[_task(1, "A", project="work")])  # B closed
    text = project_brief(conn)
    assert "work: 1 open" in text
    assert "home: 0 open, 1 done" in text


# ── doc_search ────────────────────────────────────────────────────────────────

def test_index_and_search_roundtrip():
    conn = fresh_db()
    index_text(conn, path="/x/water-bill.txt",
               content="The water bill for March is 42 euros, due Friday.")
    index_text(conn, path="/x/insurance.txt",
               content="Car insurance renewal quote attached.")
    hits = search(conn, "water bill")
    assert len(hits) == 1
    assert hits[0]["path"] == "/x/water-bill.txt"
    assert "»" in hits[0]["snippet"]  # highlighted


def test_reindex_directory(tmp_path):
    conn = fresh_db()
    (tmp_path / "a.txt").write_text("groceries list: milk, eggs")
    (tmp_path / "b.md").write_text("meeting notes about the roadmap")
    (tmp_path / ".hidden").write_text("skip me")
    (tmp_path / "img.png").write_bytes(b"\x89PNG")  # unsupported → skipped
    assert reindex(conn, tmp_path) == 2
    assert search(conn, "groceries")[0]["title"] == "a.txt"


def test_reindexing_same_path_replaces():
    conn = fresh_db()
    index_text(conn, path="/x/a.txt", content="old content here")
    index_text(conn, path="/x/a.txt", content="new content entirely")
    assert search(conn, "old") == []
    assert len(search(conn, "new")) == 1


def test_processed_documents_are_auto_indexed(tmp_path):
    from unittest.mock import patch
    from document_processor import process_file
    f = tmp_path / "note.txt"
    f.write_text("Renew the car insurance before June.")
    db = tmp_path / "t.db"
    good = {"title": "Renew insurance", "intent": "create_task",
            "source_type": "file", "approval_required": False, "confidence": 0.9}
    with patch("document_processor.call_document_agent", return_value=[good]), \
         patch("document_processor.PROCESSED_DIR", tmp_path / "processed"), \
         patch("document_processor.REJECTED_DIR", tmp_path / "rejected"):
        process_file(f, db_path=db)
    conn = open_db(db)
    hits = search(conn, "insurance")
    assert len(hits) == 1
