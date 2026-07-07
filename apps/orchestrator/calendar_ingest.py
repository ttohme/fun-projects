"""
apps/orchestrator/calendar_ingest.py
Calendar → prep tasks, entirely deterministic (no LLM).

Reads the private ICS feed, matches upcoming events against the regex rules
in prep_rules.yaml, and inserts create_task jobs ("flight → pack + check-in").
Dedupe keys are derived from the event UID + task title, so re-running daily
never duplicates a task. create_task with confidence 1.0 auto-proceeds under
the standard approval policy — prep tasks are low-risk by construction.

CLI:
    python calendar_ingest.py run [--dry-run] [--horizon 14]

Environment variables:
    BRIEFING_ICS_URL  -- private ICS feed (shared with briefing.py)
    DB_PATH           (default: db/assistant.db)
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

from db import insert_job, make_dedupe_key, open_db
from schema_validator import validate_task_object

REPO_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPO_ROOT / "apps" / "orchestrator" / "prep_rules.yaml"
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))
ICS_URL = os.environ.get("BRIEFING_ICS_URL", "")

DEFAULT_HORIZON_DAYS = 14


def parse_ics_upcoming(ics_text: str, *, today: date, horizon_days: int) -> list[dict]:
    """Events with DTSTART within [today, today+horizon]. Includes UID."""
    events = []
    horizon = today + timedelta(days=horizon_days)
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ics_text, re.S):
        m_start = re.search(r"^DTSTART[^:]*:(\S+)", block, re.M)
        m_summary = re.search(r"^SUMMARY[^:]*:(.+)$", block, re.M)
        m_uid = re.search(r"^UID[^:]*:(\S+)", block, re.M)
        if not m_start:
            continue
        raw = m_start.group(1).strip()
        try:
            event_date = datetime.strptime(raw[:8], "%Y%m%d").date()
        except ValueError:
            continue
        if today <= event_date <= horizon:
            summary = (m_summary.group(1).strip() if m_summary else "(untitled)")
            uid = m_uid.group(1).strip() if m_uid else f"{event_date}:{summary}"
            events.append({"uid": uid, "date": event_date, "summary": summary})
    return events


def load_rules(path: Path | None = None) -> list[dict]:
    data = yaml.safe_load((path or RULES_PATH).read_text()) or {}
    return data.get("rules", [])


def prep_tasks_for_event(event: dict, rules: list[dict], *, today: date) -> list[dict]:
    """Task objects a single event generates under the rules (may be empty)."""
    tasks = []
    for rule in rules:
        if not re.search(rule.get("match", ""), event["summary"]):
            continue
        for template in rule.get("tasks", []):
            days_before = int(template.get("days_before", 1))
            due = max(event["date"] - timedelta(days=days_before), today)
            title = template["title"].format(
                summary=event["summary"], date=event["date"].isoformat()
            )
            task = {
                "title": title,
                "intent": "create_task",
                "source_type": "webhook",
                "approval_required": False,   # deterministic rule, low-risk
                "confidence": 1.0,
                "due_string": due.isoformat(),
                "labels": ["calendar-prep"],
                "dedupe_key": make_dedupe_key(f"ics:{event['uid']}", title),
            }
            validate_task_object(task)
            tasks.append(task)
    return tasks


def ingest(
    conn,
    *,
    ics_text: str | None = None,
    rules: list[dict] | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    today: date | None = None,
    dry_run: bool = False,
) -> dict:
    """Run one ingestion pass. Returns counts."""
    today = today or datetime.now(timezone.utc).date()
    if ics_text is None:
        if not ICS_URL:
            raise EnvironmentError("BRIEFING_ICS_URL is not set")
        resp = requests.get(ICS_URL, timeout=15)
        resp.raise_for_status()
        ics_text = resp.text
    rules = rules if rules is not None else load_rules()

    events = parse_ics_upcoming(ics_text, today=today, horizon_days=horizon_days)
    created, duplicates = 0, 0
    for event in events:
        for task in prep_tasks_for_event(event, rules, today=today):
            if dry_run:
                created += 1
                continue
            job_id = insert_job(
                conn,
                source_type="webhook",
                source_ref=f"ics:{event['uid']}",
                payload=task,
                intent="create_task",
                approval_required=False,
            )
            if job_id is None:
                duplicates += 1
            else:
                created += 1
    return {"events": len(events), "created": created, "duplicates": duplicates}


def main() -> None:
    parser = argparse.ArgumentParser(description="Calendar → prep tasks")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="Run one ingestion pass")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_DAYS)
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        counts = ingest(conn, horizon_days=args.horizon, dry_run=args.dry_run)
        print(json.dumps(counts))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
