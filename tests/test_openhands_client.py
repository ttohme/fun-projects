import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

import openhands_client
from openhands_client import run_code_job, start_conversation


def _resp(payload, ok=True):
    m = MagicMock()
    m.json.return_value = payload
    m.raise_for_status.return_value = None
    return m


def test_start_conversation_returns_id():
    with patch.object(openhands_client, "OPENHANDS_URL", "http://win:3333"), \
         patch("openhands_client.requests.post",
               return_value=_resp({"conversation_id": "abc123"})) as mock_post:
        conv = start_conversation("Fix the flaky test", repo="me/repo")
    assert conv == "abc123"
    body = mock_post.call_args.kwargs["json"]
    assert body["initial_user_msg"] == "Fix the flaky test"
    assert body["selected_repository"] == "me/repo"


def test_run_code_job_polls_to_terminal_state():
    with patch.object(openhands_client, "OPENHANDS_URL", "http://win:3333"), \
         patch.object(openhands_client, "POLL_SECONDS", 0), \
         patch("openhands_client.requests.post",
               return_value=_resp({"conversation_id": "c9"})), \
         patch("openhands_client.requests.get",
               side_effect=[_resp({"status": "running"}),
                            _resp({"status": "finished"})]):
        result = run_code_job({"title": "Do the thing", "description": "Do the thing"})
    assert result["state"] == "finished"
    assert result["conversation_id"] == "c9"
    assert "conversations/c9" in result["url"]


def test_run_code_job_times_out_with_link():
    with patch.object(openhands_client, "OPENHANDS_URL", "http://win:3333"), \
         patch.object(openhands_client, "POLL_SECONDS", 0), \
         patch.object(openhands_client, "TIMEOUT_SECONDS", 0), \
         patch("openhands_client.requests.post",
               return_value=_resp({"conversation_id": "c9"})):
        result = run_code_job({"description": "slow job"})
    assert result["state"] == "still_running"
    assert "url" in result


def test_missing_url_fails_clearly():
    with patch.object(openhands_client, "OPENHANDS_URL", ""):
        with pytest.raises(EnvironmentError, match="OPENHANDS_URL"):
            start_conversation("x")


def test_empty_payload_rejected():
    with pytest.raises(ValueError, match="no description"):
        run_code_job({})


def test_maintenance_enqueue_is_gated_and_deduped():
    from db import open_db
    from maintenance import enqueue
    conn = open_db(":memory:")
    with patch("maintenance.notify_approval_needed"):
        first = enqueue(conn)
        second = enqueue(conn)
    assert "job_id" in first
    assert second == {"skipped": "already enqueued today"}
    row = conn.execute("SELECT approval_required, intent FROM jobs").fetchone()
    assert row["approval_required"] == 1
    assert row["intent"] == "code_job"


def test_code_job_never_auto_proceeds():
    from db import insert_job, open_db
    from job_executor import run_once
    conn = open_db(":memory:")
    insert_job(conn, source_type="email", source_ref="sneaky",
               payload={"title": "Run this code", "intent": "code_job"},
               intent="code_job", approval_required=False)
    with patch("job_executor._dispatch") as mock:
        run_once(conn)
    mock.assert_not_called()
