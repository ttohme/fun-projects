import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from db import open_db
from todoist_ledger import find_stale_tasks, nudge, sync

T1 = {"id": "101", "content": "Buy milk", "project_id": "p1", "priority": 1,
      "due": {"date": "2099-01-01"}, "created_at": "2026-01-01T00:00:00Z"}
T2 = {"id": "102", "content": "Fix bike", "project_id": "p1", "priority": 4,
      "due": None, "created_at": "2026-01-02T00:00:00Z"}


def fresh_db():
    return open_db(":memory:")


def test_sync_inserts_and_counts():
    conn = fresh_db()
    counts = sync(conn, tasks=[T1, T2])
    assert counts == {"active": 2, "new": 2, "closed": 0}
    row = conn.execute("SELECT * FROM todoist_tasks WHERE task_id='101'").fetchone()
    assert row["content"] == "Buy milk"
    assert row["due_date"] == "2099-01-01"
    assert row["closed_at"] is None


def test_sync_marks_vanished_tasks_closed():
    conn = fresh_db()
    sync(conn, tasks=[T1, T2])
    counts = sync(conn, tasks=[T1])  # 102 completed/deleted in Todoist
    assert counts["closed"] == 1
    row = conn.execute("SELECT closed_at FROM todoist_tasks WHERE task_id='102'").fetchone()
    assert row["closed_at"] is not None


def test_sync_reopens_returning_task():
    conn = fresh_db()
    sync(conn, tasks=[T1, T2])
    sync(conn, tasks=[T1])          # 102 closed
    sync(conn, tasks=[T1, T2])      # 102 uncompleted in Todoist
    row = conn.execute("SELECT closed_at FROM todoist_tasks WHERE task_id='102'").fetchone()
    assert row["closed_at"] is None


def test_sync_preserves_first_seen_across_updates():
    conn = fresh_db()
    sync(conn, tasks=[T1])
    first = conn.execute("SELECT first_seen FROM todoist_tasks WHERE task_id='101'").fetchone()[0]
    sync(conn, tasks=[{**T1, "content": "Buy oat milk"}])
    row = conn.execute("SELECT first_seen, content FROM todoist_tasks WHERE task_id='101'").fetchone()
    assert row["first_seen"] == first
    assert row["content"] == "Buy oat milk"


def _age_task(conn, task_id, days=30):
    conn.execute(
        "UPDATE todoist_tasks SET first_seen = datetime('now', ?) WHERE task_id = ?",
        (f"-{days} days", task_id),
    )
    conn.commit()


def test_find_stale_tasks_only_old_and_unscheduled():
    conn = fresh_db()
    sync(conn, tasks=[T1, T2])
    _age_task(conn, "101", 30)  # but has a future due date → not stale
    _age_task(conn, "102", 30)  # no due date → stale
    stale = find_stale_tasks(conn, days=14)
    assert [t["task_id"] for t in stale] == ["102"]


def test_closed_tasks_are_never_stale():
    conn = fresh_db()
    sync(conn, tasks=[T2])
    _age_task(conn, "102", 30)
    sync(conn, tasks=[])  # closed
    assert find_stale_tasks(conn, days=14) == []


def test_nudge_pushes_notification():
    conn = fresh_db()
    sync(conn, tasks=[T2])
    _age_task(conn, "102", 30)
    with patch("todoist_ledger.notify") as mock_notify:
        stale = nudge(conn, days=14)
    assert len(stale) == 1
    assert "Fix bike" in mock_notify.call_args.args[0]


def test_nudge_dry_run_and_empty_send_nothing():
    conn = fresh_db()
    with patch("todoist_ledger.notify") as mock_notify:
        assert nudge(conn) == []           # nothing stale
        sync(conn, tasks=[T2])
        _age_task(conn, "102", 30)
        nudge(conn, days=14, dry_run=True)  # dry run
    mock_notify.assert_not_called()
