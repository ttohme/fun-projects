import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from db import insert_job, open_db
from memory import (
    add_fact,
    facts_block,
    find_similar_jobs,
    forget_fact,
    list_facts,
    similar_note,
)


def fresh_db():
    return open_db(":memory:")


def test_add_list_forget_facts():
    conn = fresh_db()
    fid = add_fact(conn, "gym is tuesdays and thursdays", topic="routine")
    assert fid is not None
    assert add_fact(conn, "gym is tuesdays and thursdays") is None  # duplicate
    assert len(list_facts(conn)) == 1
    assert forget_fact(conn, fid) is True
    assert list_facts(conn) == []


def test_facts_block_formats_for_prompt():
    conn = fresh_db()
    add_fact(conn, "dentist is Dr. Smith 555-0100", topic="contacts")
    block = facts_block(conn)
    assert "Standing facts" in block
    assert "(contacts) dentist is Dr. Smith" in block
    assert facts_block(fresh_db()) == ""  # empty DB → no block


def test_triage_prompt_includes_facts(tmp_path):
    from unittest.mock import patch
    import triage as t
    db = tmp_path / "m.db"
    conn = open_db(db)
    add_fact(conn, "work project prefix is ACME-", topic="work")
    conn.close()
    captured = {}
    def fake_llm(prompt, model=None):
        captured["prompt"] = prompt
        return ('{"title": "ACME- kickoff", "intent": "create_task", '
                '"source_type": "email", "approval_required": false, '
                '"confidence": 0.9}')
    with patch("triage.call_litellm", side_effect=fake_llm):
        t.triage(input_content="kickoff meeting notes", source_type="email",
                 source_ref="e1", db_path=db)
    assert "work project prefix is ACME-" in captured["prompt"]


def test_find_similar_jobs_flags_near_duplicates():
    conn = fresh_db()
    insert_job(conn, source_type="email", source_ref="a",
               payload={"title": "Book dentist appointment"}, intent="create_task")
    insert_job(conn, source_type="email", source_ref="b",
               payload={"title": "Water the plants"}, intent="create_task")
    hits = find_similar_jobs(conn, "Book dentist appointment for March")
    assert len(hits) == 1
    assert hits[0]["title"] == "Book dentist appointment"

    note = similar_note(conn, "Book dentist appointment for March")
    assert "similar to job #" in note


def test_similar_note_excludes_self_and_unrelated():
    conn = fresh_db()
    jid = insert_job(conn, source_type="email", source_ref="a",
                     payload={"title": "Book dentist appointment"}, intent="create_task")
    assert similar_note(conn, "Book dentist appointment",
                        exclude_job_id=jid) == ""
    assert similar_note(conn, "Completely different thing") == ""
