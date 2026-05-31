import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from hass_client import (
    HassError,
    call_service,
    get_state,
    get_states,
    is_on,
    process_conversation,
    turn_off,
    turn_on,
)

BASE = "http://hass.local:8123"
TOKEN = "test-token"


def _resp(json_data, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    return r


def _resp_404():
    r = MagicMock()
    r.status_code = 404
    r.raise_for_status = MagicMock(side_effect=Exception("404"))
    return r


# ── get_state ─────────────────────────────────────────────────────────────────

def test_get_state_returns_entity():
    entity = {"entity_id": "light.living_room", "state": "on", "attributes": {}}
    with patch("hass_client.requests.get", return_value=_resp(entity)):
        result = get_state("light.living_room", base_url=BASE, token=TOKEN)
    assert result["state"] == "on"


def test_get_state_sends_bearer_token():
    with patch("hass_client.requests.get", return_value=_resp({})) as mock_get:
        get_state("light.x", base_url=BASE, token=TOKEN)
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_get_state_404_raises_hass_error():
    with patch("hass_client.requests.get", return_value=_resp_404()):
        with pytest.raises(HassError, match="Entity not found"):
            get_state("light.nonexistent", base_url=BASE, token=TOKEN)


def test_get_state_raises_without_token():
    with pytest.raises(EnvironmentError, match="HASS_TOKEN"):
        get_state("light.x", base_url=BASE, token="")


def test_get_state_raises_without_base_url():
    with pytest.raises(EnvironmentError, match="HASS_URL"):
        get_state("light.x", base_url="", token=TOKEN)


# ── get_states ────────────────────────────────────────────────────────────────

def test_get_states_returns_list():
    states = [{"entity_id": "light.a"}, {"entity_id": "switch.b"}]
    with patch("hass_client.requests.get", return_value=_resp(states)):
        result = get_states(base_url=BASE, token=TOKEN)
    assert len(result) == 2


# ── is_on ─────────────────────────────────────────────────────────────────────

def test_is_on_true():
    with patch("hass_client.get_state", return_value={"state": "on"}):
        assert is_on("light.x", base_url=BASE, token=TOKEN) is True


def test_is_on_false():
    with patch("hass_client.get_state", return_value={"state": "off"}):
        assert is_on("light.x", base_url=BASE, token=TOKEN) is False


# ── call_service ──────────────────────────────────────────────────────────────

def test_call_service_posts_correct_url():
    with patch("hass_client.requests.post", return_value=_resp([])) as mock_post:
        call_service("light", "turn_on", {"entity_id": "light.x"},
                     base_url=BASE, token=TOKEN)
    url = mock_post.call_args.args[0]
    assert url == f"{BASE}/api/services/light/turn_on"


def test_call_service_sends_service_data():
    with patch("hass_client.requests.post", return_value=_resp([])) as mock_post:
        call_service("light", "turn_on", {"entity_id": "light.x", "brightness": 128},
                     base_url=BASE, token=TOKEN)
    body = mock_post.call_args.kwargs["json"]
    assert body["brightness"] == 128


def test_call_service_empty_data_sends_empty_dict():
    with patch("hass_client.requests.post", return_value=_resp([])) as mock_post:
        call_service("homeassistant", "restart", base_url=BASE, token=TOKEN)
    body = mock_post.call_args.kwargs["json"]
    assert body == {}


# ── turn_on / turn_off ────────────────────────────────────────────────────────

def test_turn_on_uses_entity_domain():
    with patch("hass_client.call_service", return_value=[]) as mock_svc:
        turn_on("light.kitchen", base_url=BASE, token=TOKEN)
    assert mock_svc.call_args.args[:2] == ("light", "turn_on")


def test_turn_off_uses_entity_domain():
    with patch("hass_client.call_service", return_value=[]) as mock_svc:
        turn_off("switch.fan", base_url=BASE, token=TOKEN)
    assert mock_svc.call_args.args[:2] == ("switch", "turn_off")


# ── process_conversation ──────────────────────────────────────────────────────

def test_process_conversation_posts_text():
    response = {"response": {"speech": {"plain": {"speech": "Done"}}}}
    with patch("hass_client.requests.post", return_value=_resp(response)) as mock_post:
        result = process_conversation("turn on the lights", base_url=BASE, token=TOKEN)
    body = mock_post.call_args.kwargs["json"]
    assert body["text"] == "turn on the lights"
    assert body["language"] == "en"


def test_process_conversation_includes_conversation_id():
    with patch("hass_client.requests.post", return_value=_resp({})) as mock_post:
        process_conversation("lights off", conversation_id="conv-1",
                             base_url=BASE, token=TOKEN)
    body = mock_post.call_args.kwargs["json"]
    assert body["conversation_id"] == "conv-1"
