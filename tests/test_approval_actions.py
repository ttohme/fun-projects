import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

import approval_actions
from approval_actions import (
    ActionVerificationError,
    _sign,
    make_action_url,
    verify_and_decide,
)
from db import insert_approval, insert_job, open_db

SECRET = "test-secret"
BASE = "http://n8n.local:5678/webhook/approval-action"


def _gated_job(conn):
    payload = {"title": "Send reply", "intent": "send_email",
               "source_type": "email", "approval_required": True, "confidence": 0.9}
    job_id = insert_job(conn, source_type="email", source_ref="e1",
                        payload=payload, intent="send_email", approval_required=True)
    approval_id = insert_approval(conn, job_id=job_id, requested_action="Send reply")
    return job_id, approval_id


def _params(url):
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(url).query)
    return int(q["id"][0]), q["action"][0], int(q["exp"][0]), q["sig"][0]


def test_make_action_url_is_signed_and_expiring():
    url = make_action_url(5, "approved", base_url=BASE, secret=SECRET)
    approval_id, action, exp, sig = _params(url)
    assert approval_id == 5 and action == "approved"
    assert exp > datetime.now(timezone.utc).timestamp()
    assert sig == _sign(5, "approved", exp, SECRET)


def test_verify_and_decide_happy_path():
    conn = open_db(":memory:")
    job_id, approval_id = _gated_job(conn)
    url = make_action_url(approval_id, "approved", base_url=BASE, secret=SECRET)
    aid, action, exp, sig = _params(url)
    result = verify_and_decide(conn, approval_id=aid, action=action,
                               expires_ts=exp, signature=sig, secret=SECRET)
    assert result["decision"] == "approved"
    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "approved"


def test_bad_signature_rejected():
    conn = open_db(":memory:")
    _, approval_id = _gated_job(conn)
    url = make_action_url(approval_id, "approved", base_url=BASE, secret=SECRET)
    aid, action, exp, _ = _params(url)
    with pytest.raises(ActionVerificationError, match="Bad signature"):
        verify_and_decide(conn, approval_id=aid, action=action,
                          expires_ts=exp, signature="f" * 64, secret=SECRET)


def test_action_flip_breaks_signature():
    # A reject link cannot be replayed as an approve.
    conn = open_db(":memory:")
    _, approval_id = _gated_job(conn)
    url = make_action_url(approval_id, "rejected", base_url=BASE, secret=SECRET)
    aid, _, exp, sig = _params(url)
    with pytest.raises(ActionVerificationError, match="Bad signature"):
        verify_and_decide(conn, approval_id=aid, action="approved",
                          expires_ts=exp, signature=sig, secret=SECRET)


def test_expired_link_rejected():
    conn = open_db(":memory:")
    _, approval_id = _gated_job(conn)
    past = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
    sig = _sign(approval_id, "approved", past, SECRET)
    with pytest.raises(ActionVerificationError, match="expired"):
        verify_and_decide(conn, approval_id=approval_id, action="approved",
                          expires_ts=past, signature=sig, secret=SECRET)


def test_replay_is_single_use():
    conn = open_db(":memory:")
    _, approval_id = _gated_job(conn)
    url = make_action_url(approval_id, "approved", base_url=BASE, secret=SECRET)
    aid, action, exp, sig = _params(url)
    verify_and_decide(conn, approval_id=aid, action=action,
                      expires_ts=exp, signature=sig, secret=SECRET)
    with pytest.raises(ValueError, match="already decided"):
        verify_and_decide(conn, approval_id=aid, action=action,
                          expires_ts=exp, signature=sig, secret=SECRET)


def test_missing_secret_fails_closed():
    conn = open_db(":memory:")
    _, approval_id = _gated_job(conn)
    with pytest.raises(ActionVerificationError, match="not configured"):
        verify_and_decide(conn, approval_id=approval_id, action="approved",
                          expires_ts=9999999999, signature="x", secret="")


def test_notification_carries_buttons_when_configured():
    import notifier
    with patch.object(approval_actions, "ACTION_URL", BASE), \
         patch.object(approval_actions, "ACTION_SECRET", SECRET), \
         patch.object(notifier, "NTFY_URL", "http://ntfy.local:8090"), \
         patch("notifier.requests.post") as mock_post:
        mock_post.return_value.ok = True
        notifier.notify_approval_needed({"title": "X", "intent": "send_email"}, 3)
    headers = mock_post.call_args.kwargs["headers"]
    assert "Actions" in headers
    assert "Approve" in headers["Actions"] and "Reject" in headers["Actions"]
    assert "sig=" in headers["Actions"]


def test_notification_plain_when_actions_unconfigured():
    import notifier
    with patch.object(approval_actions, "ACTION_URL", ""), \
         patch.object(notifier, "NTFY_URL", "http://ntfy.local:8090"), \
         patch("notifier.requests.post") as mock_post:
        mock_post.return_value.ok = True
        notifier.notify_approval_needed({"title": "X"}, 3)
    assert "Actions" not in mock_post.call_args.kwargs["headers"]
