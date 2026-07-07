"""
apps/orchestrator/hass_client.py
Home Assistant REST API client.

Read operations auto-proceed. Write operations (service calls) must only
be called after an approval has been recorded — the caller is responsible
for that gate; this module does not enforce it.

Environment variables:
    HASS_URL    -- e.g. http://homeassistant.local:8123  (required)
    HASS_TOKEN  -- long-lived access token               (required)
"""
import os
from dataclasses import dataclass

import requests

_BASE = os.environ.get("HASS_URL", "").rstrip("/")
_TOKEN = os.environ.get("HASS_TOKEN", "")


class HassError(Exception):
    """Raised when the Home Assistant API returns an error."""


def _headers(token: str = "") -> dict:
    tok = token or _TOKEN
    if not tok:
        raise EnvironmentError("HASS_TOKEN is not set")
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }


def _base(base_url: str = "") -> str:
    url = base_url or _BASE
    if not url:
        raise EnvironmentError("HASS_URL is not set")
    return url.rstrip("/")


# ── Read operations (auto-proceed) ────────────────────────────────────────────

def get_state(entity_id: str, *, base_url: str = "", token: str = "") -> dict:
    """Return the state object for a single entity."""
    url = f"{_base(base_url)}/api/states/{entity_id}"
    resp = requests.get(url, headers=_headers(token), timeout=10)
    if resp.status_code == 404:
        raise HassError(f"Entity not found: {entity_id}")
    resp.raise_for_status()
    return resp.json()


def get_states(*, base_url: str = "", token: str = "") -> list[dict]:
    """Return all entity states."""
    url = f"{_base(base_url)}/api/states"
    resp = requests.get(url, headers=_headers(token), timeout=15)
    resp.raise_for_status()
    return resp.json()


def is_on(entity_id: str, *, base_url: str = "", token: str = "") -> bool:
    """Return True if entity state is 'on'."""
    state = get_state(entity_id, base_url=base_url, token=token)
    return state.get("state") == "on"


# ── Write operations (require prior approval) ─────────────────────────────────

def call_service(
    domain: str,
    service: str,
    service_data: dict | None = None,
    *,
    base_url: str = "",
    token: str = "",
) -> list[dict]:
    """
    Call a Home Assistant service. Returns the list of affected states.
    Caller must have obtained approval before invoking this function.
    """
    url = f"{_base(base_url)}/api/services/{domain}/{service}"
    resp = requests.post(
        url,
        headers=_headers(token),
        json=service_data or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def turn_on(entity_id: str, *, base_url: str = "", token: str = "") -> list[dict]:
    """Turn on a light, switch, or other entity. Requires prior approval."""
    domain = entity_id.split(".")[0]
    return call_service(domain, "turn_on",
                        {"entity_id": entity_id},
                        base_url=base_url, token=token)


def turn_off(entity_id: str, *, base_url: str = "", token: str = "") -> list[dict]:
    """Turn off a light, switch, or other entity. Requires prior approval."""
    domain = entity_id.split(".")[0]
    return call_service(domain, "turn_off",
                        {"entity_id": entity_id},
                        base_url=base_url, token=token)


def process_conversation(
    text: str,
    *,
    conversation_id: str | None = None,
    base_url: str = "",
    token: str = "",
) -> dict:
    """Send text to the Home Assistant conversation agent."""
    url = f"{_base(base_url)}/api/conversation/process"
    body: dict = {"text": text, "language": "en"}
    if conversation_id:
        body["conversation_id"] = conversation_id
    resp = requests.post(url, headers=_headers(token), json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()
