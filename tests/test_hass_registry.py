import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from db import open_db
from hass_registry import (
    UnknownEntityError,
    cache_size,
    entity_exists,
    refresh_cache,
    validate_home_control_payload,
)

STATES = [
    {"entity_id": "light.kitchen", "attributes": {"friendly_name": "Kitchen Light"}},
    {"entity_id": "switch.heater", "attributes": {"friendly_name": "Heater"}},
    {"entity_id": "sensor.temp", "attributes": {}},
    {"entity_id": "garbage-no-domain", "attributes": {}},  # skipped (no dot)
]


def fresh_db():
    return open_db(":memory:")


def test_refresh_cache_populates_entities():
    conn = fresh_db()
    n = refresh_cache(conn, states=STATES)
    assert n == 3  # malformed id skipped
    assert entity_exists(conn, "light.kitchen")
    assert not entity_exists(conn, "light.bedroom")


def test_refresh_cache_replaces_stale_entries():
    conn = fresh_db()
    refresh_cache(conn, states=STATES)
    refresh_cache(conn, states=[{"entity_id": "light.new", "attributes": {}}])
    assert cache_size(conn) == 1
    assert not entity_exists(conn, "light.kitchen")


def test_validate_passes_known_entity():
    conn = fresh_db()
    refresh_cache(conn, states=STATES)
    validate_home_control_payload(conn, {
        "hass_service_data": {"entity_id": "light.kitchen"},
    })  # no raise


def test_validate_rejects_unknown_entity():
    conn = fresh_db()
    refresh_cache(conn, states=STATES)
    with pytest.raises(UnknownEntityError, match="light.hallucinated"):
        validate_home_control_payload(conn, {
            "hass_service_data": {"entity_id": "light.hallucinated"},
        })


def test_validate_rejects_unknown_in_entity_list():
    conn = fresh_db()
    refresh_cache(conn, states=STATES)
    with pytest.raises(UnknownEntityError):
        validate_home_control_payload(conn, {
            "hass_service_data": {"entity_id": ["light.kitchen", "light.fake"]},
        })


def test_validate_checks_hass_entity_id_field():
    conn = fresh_db()
    refresh_cache(conn, states=STATES)
    with pytest.raises(UnknownEntityError):
        validate_home_control_payload(conn, {"hass_entity_id": "sensor.nope"})
    validate_home_control_payload(conn, {"hass_entity_id": "sensor.temp"})


def test_validate_fails_open_on_empty_cache():
    # Fresh install: cache empty → validation must not block anything.
    conn = fresh_db()
    validate_home_control_payload(conn, {
        "hass_service_data": {"entity_id": "light.anything"},
    })  # no raise


def test_executor_rejects_unknown_entity_without_retry():
    from db import insert_job
    from job_executor import execute_job
    conn = fresh_db()
    refresh_cache(conn, states=STATES)
    payload = {"title": "Turn on fake light", "intent": "home_control_write",
               "source_type": "chat", "approval_required": True, "confidence": 0.9,
               "hass_domain": "light", "hass_service": "turn_on",
               "hass_service_data": {"entity_id": "light.hallucinated"}}
    job_id = insert_job(conn, source_type="chat", source_ref="c1", payload=payload,
                        intent="home_control_write", approval_required=True)
    conn.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
    conn.commit()
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    result = execute_job(conn, job)
    assert result["status"] == "rejected"
    row = conn.execute("SELECT status, attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "error"   # immediate, no retry ladder
    assert row["attempts"] == 0
