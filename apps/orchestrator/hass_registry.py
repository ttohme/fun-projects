"""
apps/orchestrator/hass_registry.py
Local cache of Home Assistant's entity registry + payload validation.

Stops hallucinated entity_ids at the last gate before an API call: the LLM can
only reference entities that actually exist in the house. The cache is a small
SQLite table refreshed from GET /api/states (nightly cron or `make hass-cache`).

Fail-open by design when the cache is EMPTY (fresh install, HA not yet
configured) — validation only rejects when we positively know the entity set
and the entity isn't in it.

CLI:
    python hass_registry.py refresh     # pull /api/states into the cache
    python hass_registry.py list        # show cached entities
"""
import argparse
import json
import os
import sys
from pathlib import Path

from db import _now, open_db

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hass_entities (
    entity_id     TEXT PRIMARY KEY,
    friendly_name TEXT,
    domain        TEXT NOT NULL,
    refreshed_at  TEXT NOT NULL
);
"""


class UnknownEntityError(Exception):
    """A home-control payload references an entity not present in HA."""


def ensure_table(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def refresh_cache(conn, *, states: list[dict] | None = None) -> int:
    """
    Replace the cache with the current entity set. `states` may be injected
    for tests; by default it calls hass_client.get_states().
    Returns the number of entities cached.
    """
    if states is None:
        from hass_client import get_states
        states = get_states()

    ensure_table(conn)
    now = _now()
    conn.execute("DELETE FROM hass_entities")
    for state in states:
        entity_id = state.get("entity_id", "")
        if not entity_id or "." not in entity_id:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO hass_entities"
            " (entity_id, friendly_name, domain, refreshed_at) VALUES (?, ?, ?, ?)",
            (
                entity_id,
                (state.get("attributes") or {}).get("friendly_name", ""),
                entity_id.split(".", 1)[0],
                now,
            ),
        )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM hass_entities").fetchone()[0]


def cache_size(conn) -> int:
    ensure_table(conn)
    return conn.execute("SELECT COUNT(*) FROM hass_entities").fetchone()[0]


def entity_exists(conn, entity_id: str) -> bool:
    ensure_table(conn)
    row = conn.execute(
        "SELECT 1 FROM hass_entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    return row is not None


def _referenced_entities(payload: dict) -> list[str]:
    """Every entity_id a home-control payload would touch."""
    refs = []
    if payload.get("hass_entity_id"):
        refs.append(payload["hass_entity_id"])
    service_data = payload.get("hass_service_data") or {}
    target = service_data.get("entity_id")
    if isinstance(target, str):
        refs.append(target)
    elif isinstance(target, list):
        refs.extend(t for t in target if isinstance(t, str))
    return refs


def validate_home_control_payload(conn, payload: dict) -> None:
    """
    Raise UnknownEntityError if the payload references an entity that is not
    in the cache. No-op when the cache is empty (fail-open for fresh installs)
    or when the payload references no entities.
    """
    if cache_size(conn) == 0:
        return
    for entity_id in _referenced_entities(payload):
        if not entity_exists(conn, entity_id):
            raise UnknownEntityError(
                f"Entity {entity_id!r} not found in Home Assistant "
                f"(cache refreshed via `make hass-cache`) — refusing the call"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="HA entity registry cache")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("refresh", help="Pull /api/states into the cache")
    sub.add_parser("list", help="Show cached entities")
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        if args.command == "refresh":
            n = refresh_cache(conn)
            print(f"hass_registry: cached {n} entities")
        elif args.command == "list":
            ensure_table(conn)
            for row in conn.execute(
                "SELECT entity_id, friendly_name FROM hass_entities ORDER BY entity_id"
            ):
                print(f"  {row['entity_id']}  —  {row['friendly_name']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
