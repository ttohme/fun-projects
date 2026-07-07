import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

import notifier


def test_notify_noop_when_url_unset():
    with patch.object(notifier, "NTFY_URL", ""):
        with patch("notifier.requests.post") as mock_post:
            assert notifier.notify("hello") is False
            mock_post.assert_not_called()


def test_notify_posts_to_topic():
    resp = MagicMock(ok=True)
    with patch.object(notifier, "NTFY_URL", "http://ntfy.local:8090"), \
         patch.object(notifier, "NTFY_TOPIC", "mytopic"), \
         patch("notifier.requests.post", return_value=resp) as mock_post:
        assert notifier.notify("hello", title="Hi", priority="high") is True
    args, kwargs = mock_post.call_args
    assert args[0] == "http://ntfy.local:8090/mytopic"
    assert kwargs["data"] == b"hello"
    assert kwargs["headers"]["Title"] == "Hi"
    assert kwargs["headers"]["Priority"] == "high"


def test_notify_swallows_network_errors():
    import requests
    with patch.object(notifier, "NTFY_URL", "http://ntfy.local:8090"), \
         patch("notifier.requests.post",
               side_effect=requests.ConnectionError("down")):
        assert notifier.notify("hello") is False  # never raises


def test_notify_approval_needed_includes_title_and_intent():
    resp = MagicMock(ok=True)
    with patch.object(notifier, "NTFY_URL", "http://ntfy.local:8090"), \
         patch("notifier.requests.post", return_value=resp) as mock_post:
        ok = notifier.notify_approval_needed(
            {"title": "Buy milk", "intent": "create_task"}, approval_id=7)
    assert ok is True
    body = mock_post.call_args.kwargs["data"].decode()
    assert "Buy milk" in body and "create_task" in body and "#7" in body


def test_intake_failure_does_not_break_pipeline():
    # notify() returning False must be fine for callers — simulate triage's
    # call pattern: the return value is ignored.
    with patch.object(notifier, "NTFY_URL", ""):
        assert notifier.notify_approval_needed({"title": "x"}) is False
