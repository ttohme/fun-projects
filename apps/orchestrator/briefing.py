"""
apps/orchestrator/briefing.py
Morning briefing: today's tasks + calendar + weather → one phone push at 07:00.

Deterministic fetchers, deterministic composition. Each section degrades
independently — no Todoist token means no task section, an unreachable ICS
feed means no calendar section — the briefing still ships with whatever it
has. An optional single assistant-small call tightens the wording
(BRIEFING_USE_LLM=1); any LLM failure falls back to the deterministic text.

CLI:
    python briefing.py [--dry-run]      # --dry-run prints instead of pushing

Environment variables:
    TODOIST_API_TOKEN   -- task section (optional)
    BRIEFING_ICS_URL    -- private ICS feed URL for the calendar section (optional)
    BRIEFING_LAT / BRIEFING_LON -- weather section via Open-Meteo, keyless (optional)
    BRIEFING_USE_LLM    -- "1" to compress via assistant-small (default off)
"""
import argparse
import os
import re
import sys
from datetime import date, datetime, timezone

import requests

from notifier import notify

ICS_URL = os.environ.get("BRIEFING_ICS_URL", "")
LAT = os.environ.get("BRIEFING_LAT", "")
LON = os.environ.get("BRIEFING_LON", "")
USE_LLM = os.environ.get("BRIEFING_USE_LLM", "0") == "1"

WEATHER_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent showers", 95: "thunderstorm",
    96: "thunderstorm w/ hail", 99: "thunderstorm w/ hail",
}


# ── Calendar (minimal ICS parsing — DTSTART + SUMMARY only) ───────────────────

def parse_ics_events(ics_text: str, on_date: date) -> list[dict]:
    """
    Extract events occurring on `on_date` from an ICS feed.
    Handles all-day (VALUE=DATE:YYYYMMDD) and timed (YYYYMMDDTHHMMSS[Z])
    DTSTART values. Recurring-event expansion is out of scope: only the
    literal DTSTART is matched (fine for a personal morning brief).
    """
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", ics_text, re.S):
        m_start = re.search(r"^DTSTART[^:]*:(\S+)", block, re.M)
        m_summary = re.search(r"^SUMMARY[^:]*:(.+)$", block, re.M)
        if not m_start:
            continue
        raw = m_start.group(1).strip()
        try:
            if "T" in raw:
                start = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
                event_date, time_str = start.date(), start.strftime("%H:%M")
            else:
                event_date, time_str = datetime.strptime(raw[:8], "%Y%m%d").date(), "all day"
        except ValueError:
            continue
        if event_date == on_date:
            summary = (m_summary.group(1).strip() if m_summary else "(untitled)")
            events.append({"time": time_str, "summary": summary})
    events.sort(key=lambda e: (e["time"] == "all day", e["time"]))
    return events


def fetch_calendar(on_date: date) -> list[dict]:
    if not ICS_URL:
        return []
    resp = requests.get(ICS_URL, timeout=15)
    resp.raise_for_status()
    return parse_ics_events(resp.text, on_date)


# ── Tasks ─────────────────────────────────────────────────────────────────────

def fetch_todoist_today() -> list[dict]:
    """Today + overdue tasks via the Todoist REST filter."""
    from todoist_client import TODOIST_API_BASE, _headers
    resp = requests.get(
        f"{TODOIST_API_BASE}/tasks",
        params={"filter": "today | overdue"},
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── Weather (Open-Meteo, free and keyless) ────────────────────────────────────

def fetch_weather() -> dict | None:
    if not LAT or not LON:
        return None
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": LAT, "longitude": LON,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": "auto", "forecast_days": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    daily = resp.json().get("daily", {})
    try:
        return {
            "condition": WEATHER_CODES.get(daily["weather_code"][0], "unknown"),
            "high": round(daily["temperature_2m_max"][0]),
            "low": round(daily["temperature_2m_min"][0]),
        }
    except (KeyError, IndexError):
        return None


# ── Composition ───────────────────────────────────────────────────────────────

def compose(tasks: list[dict], events: list[dict], weather: dict | None) -> str:
    lines = []
    if weather:
        lines.append(f"☁ {weather['condition']}, {weather['low']}–{weather['high']}°")
    if events:
        lines.append(f"\n📅 {len(events)} event(s):")
        lines += [f"  {e['time']}  {e['summary']}" for e in events[:6]]
    if tasks:
        lines.append(f"\n☑ {len(tasks)} task(s) due today/overdue:")
        lines += [f"  • {t.get('content', '?')}" for t in tasks[:8]]
        if len(tasks) > 8:
            lines.append(f"  …and {len(tasks) - 8} more")
    if not lines:
        lines.append("Nothing on the calendar, no tasks due. Enjoy the quiet day.")
    return "\n".join(lines)


def _compress_with_llm(text: str) -> str:
    """One assistant-small call to tighten the brief; falls back on any error."""
    try:
        from triage import call_litellm
        compressed = call_litellm(
            "Rewrite this morning briefing as 6-8 short lines, keeping every "
            "task and event, no preamble, plain text only:\n\n" + text,
            model="assistant-small",
        )
        return compressed.strip() or text
    except Exception as exc:
        print(f"briefing: LLM compression skipped: {exc}", file=sys.stderr)
        return text


def build_briefing(*, today: date | None = None) -> str:
    """Assemble the briefing text; every section is optional."""
    today = today or datetime.now(timezone.utc).date()
    sections: dict[str, object] = {"tasks": [], "events": [], "weather": None}

    for name, fetcher in (
        ("tasks", fetch_todoist_today),
        ("events", lambda: fetch_calendar(today)),
        ("weather", fetch_weather),
    ):
        try:
            sections[name] = fetcher()
        except Exception as exc:
            print(f"briefing: {name} section skipped: {exc}", file=sys.stderr)

    text = compose(sections["tasks"] or [], sections["events"] or [],
                   sections["weather"])
    if USE_LLM:
        text = _compress_with_llm(text)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Morning briefing")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print instead of pushing to ntfy")
    args = parser.parse_args()

    text = build_briefing()
    if args.dry_run:
        print(text)
    else:
        sent = notify(text, title="Good morning", tags="sunrise")
        print("briefing: pushed" if sent else "briefing: ntfy not configured, printing:")
        if not sent:
            print(text)


if __name__ == "__main__":
    main()
