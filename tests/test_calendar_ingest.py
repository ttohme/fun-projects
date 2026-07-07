import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from calendar_ingest import (
    ingest,
    load_rules,
    parse_ics_upcoming,
    prep_tasks_for_event,
)
from db import open_db

TODAY = date(2026, 7, 7)

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:flight-123
DTSTART;VALUE=DATE:20260712
SUMMARY:Flight to Lisbon
END:VEVENT
BEGIN:VEVENT
UID:dentist-9
DTSTART:20260710T090000Z
SUMMARY:Dentist checkup
END:VEVENT
BEGIN:VEVENT
UID:far-away
DTSTART;VALUE=DATE:20261225
SUMMARY:Flight home for Christmas
END:VEVENT
END:VCALENDAR
"""


def test_parse_upcoming_respects_horizon():
    events = parse_ics_upcoming(ICS, today=TODAY, horizon_days=14)
    assert {e["uid"] for e in events} == {"flight-123", "dentist-9"}


def test_rules_file_loads_and_matches_flight():
    rules = load_rules()
    event = {"uid": "flight-123", "date": date(2026, 7, 12), "summary": "Flight to Lisbon"}
    tasks = prep_tasks_for_event(event, rules, today=TODAY)
    titles = [t["title"] for t in tasks]
    assert any("Pack for" in t for t in titles)
    assert any("check-in" in t for t in titles)
    # Due one day before the flight
    assert all(t["due_string"] == "2026-07-11" for t in tasks)
    # Deterministic, low-risk → auto-proceed
    assert all(t["approval_required"] is False for t in tasks)


def test_due_date_never_in_the_past():
    rules = [{"match": "(?i)dentist", "tasks": [{"title": "Confirm: {summary}", "days_before": 30}]}]
    event = {"uid": "d1", "date": date(2026, 7, 10), "summary": "Dentist checkup"}
    tasks = prep_tasks_for_event(event, rules, today=TODAY)
    assert tasks[0]["due_string"] == TODAY.isoformat()  # clamped to today


def test_non_matching_event_generates_nothing():
    rules = load_rules()
    event = {"uid": "x", "date": date(2026, 7, 8), "summary": "Lunch with Sam"}
    assert prep_tasks_for_event(event, rules, today=TODAY) == []


def test_ingest_writes_jobs_and_dedupes_on_rerun():
    conn = open_db(":memory:")
    counts1 = ingest(conn, ics_text=ICS, today=TODAY)
    assert counts1["events"] == 2
    assert counts1["created"] >= 3  # 2 flight tasks + 1 dentist task
    assert counts1["duplicates"] == 0

    counts2 = ingest(conn, ics_text=ICS, today=TODAY)  # daily re-run
    assert counts2["created"] == 0
    assert counts2["duplicates"] == counts1["created"]

    row = conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE intent='create_task'"
    ).fetchone()
    assert row["c"] == counts1["created"]


def test_ingest_dry_run_writes_nothing():
    conn = open_db(":memory:")
    counts = ingest(conn, ics_text=ICS, today=TODAY, dry_run=True)
    assert counts["created"] >= 3
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0
