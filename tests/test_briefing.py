import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

import briefing
from briefing import build_briefing, compose, parse_ics_events

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260707
SUMMARY:Trash pickup
END:VEVENT
BEGIN:VEVENT
DTSTART:20260707T143000Z
SUMMARY:Dentist appointment
END:VEVENT
BEGIN:VEVENT
DTSTART:20260708T090000Z
SUMMARY:Tomorrow's meeting
END:VEVENT
BEGIN:VEVENT
DTSTART:garbage
SUMMARY:Broken event
END:VEVENT
END:VCALENDAR
"""


def test_parse_ics_filters_to_date_and_sorts():
    events = parse_ics_events(ICS, date(2026, 7, 7))
    assert [e["summary"] for e in events] == ["Dentist appointment", "Trash pickup"]
    assert events[0]["time"] == "14:30"
    assert events[1]["time"] == "all day"


def test_parse_ics_other_day_empty():
    assert parse_ics_events(ICS, date(2026, 7, 9)) == []


def test_compose_all_sections():
    text = compose(
        tasks=[{"content": "Buy milk"}, {"content": "Call plumber"}],
        events=[{"time": "14:30", "summary": "Dentist"}],
        weather={"condition": "partly cloudy", "high": 24, "low": 15},
    )
    assert "partly cloudy" in text and "15–24°" in text
    assert "Dentist" in text and "Buy milk" in text
    assert "2 task(s)" in text


def test_compose_empty_day():
    text = compose([], [], None)
    assert "quiet day" in text


def test_compose_truncates_long_task_list():
    tasks = [{"content": f"task {i}"} for i in range(12)]
    text = compose(tasks, [], None)
    assert "…and 4 more" in text


def test_build_briefing_sections_degrade_independently():
    # Todoist raises, calendar works, weather unset → briefing still ships.
    with patch("briefing.fetch_todoist_today", side_effect=Exception("no token")), \
         patch("briefing.fetch_calendar",
               return_value=[{"time": "10:00", "summary": "Standup"}]), \
         patch("briefing.fetch_weather", return_value=None):
        text = build_briefing(today=date(2026, 7, 7))
    assert "Standup" in text
    assert "task" not in text.lower() or "no tasks" in text.lower()


def test_llm_compression_falls_back_on_error():
    with patch.object(briefing, "USE_LLM", True), \
         patch("briefing.fetch_todoist_today", return_value=[{"content": "Buy milk"}]), \
         patch("briefing.fetch_calendar", return_value=[]), \
         patch("briefing.fetch_weather", return_value=None), \
         patch("triage.call_litellm", side_effect=Exception("LiteLLM down")):
        text = build_briefing(today=date(2026, 7, 7))
    assert "Buy milk" in text  # deterministic fallback survived
