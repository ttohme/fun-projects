import sys
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "file-watcher"))

# Stub out watchdog before importing watcher so the import succeeds
# even when watchdog is not installed in the test environment.
watchdog_stub = MagicMock()
sys.modules.setdefault("watchdog", watchdog_stub)
sys.modules.setdefault("watchdog.events", watchdog_stub.events)
sys.modules.setdefault("watchdog.observers", watchdog_stub.observers)

# Provide the real base class names the module uses
watchdog_stub.events.FileSystemEventHandler = object
watchdog_stub.events.FileCreatedEvent = MagicMock
watchdog_stub.events.FileMovedEvent = MagicMock

import watcher as w


# ── Skip logic ────────────────────────────────────────────────────────────────

def _make_event(path: str, is_dir: bool = False):
    event = MagicMock()
    event.src_path = path
    event.is_directory = is_dir
    return event


def test_directory_event_is_skipped(tmp_path):
    handler = w.InboxHandler()
    event = _make_event(str(tmp_path), is_dir=True)
    with patch.object(handler, "_post_to_n8n") as mock_post:
        handler.on_created(event)
        mock_post.assert_not_called()


def test_hidden_file_is_skipped(tmp_path):
    hidden = tmp_path / ".DS_Store"
    hidden.touch()
    handler = w.InboxHandler()
    event = _make_event(str(hidden))
    with patch.object(handler, "_post_to_n8n") as mock_post:
        handler.on_created(event)
        mock_post.assert_not_called()


def test_tmp_file_is_skipped(tmp_path):
    tmp_file = tmp_path / "upload.tmp"
    tmp_file.touch()
    handler = w.InboxHandler()
    event = _make_event(str(tmp_file))
    with patch.object(handler, "_post_to_n8n") as mock_post:
        handler.on_created(event)
        mock_post.assert_not_called()


def test_swp_file_is_skipped(tmp_path):
    swp = tmp_path / ".note.swp"
    swp.touch()
    handler = w.InboxHandler()
    event = _make_event(str(swp))
    with patch.object(handler, "_post_to_n8n") as mock_post:
        handler.on_created(event)
        mock_post.assert_not_called()


def test_normal_file_is_processed(tmp_path):
    normal = tmp_path / "invoice.pdf"
    normal.touch()
    handler = w.InboxHandler()
    event = _make_event(str(normal))
    with patch.object(handler, "_post_to_n8n") as mock_post:
        handler.on_created(event)
        mock_post.assert_called_once_with(normal)


# ── Webhook POST logic ────────────────────────────────────────────────────────

def test_post_succeeds_on_first_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "WEBHOOK_URL", "http://localhost:5678/webhook/test")
    test_file = tmp_path / "doc.txt"
    test_file.write_text("hello")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    handler = w.InboxHandler()
    with patch("watcher.requests.post", return_value=mock_resp) as mock_post:
        handler._post_to_n8n(test_file)
        assert mock_post.call_count == 1
        payload = mock_post.call_args.kwargs["json"]
        assert payload["event"] == "file_created"
        assert payload["file_name"] == "doc.txt"
        assert payload["source"] == "file-watcher"


def test_post_retries_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "WEBHOOK_URL", "http://localhost:5678/webhook/test")
    test_file = tmp_path / "doc.txt"
    test_file.write_text("hello")

    import requests as req_mod

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()

    call_count = {"n": 0}

    def flaky_post(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise req_mod.ConnectionError("connection refused")
        return mock_resp

    handler = w.InboxHandler()
    with patch("watcher.requests.post", side_effect=flaky_post):
        with patch("watcher.time.sleep"):  # avoid real sleeps in tests
            handler._post_to_n8n(test_file, retries=3)
    assert call_count["n"] == 3


def test_no_webhook_url_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "WEBHOOK_URL", "")
    test_file = tmp_path / "doc.txt"
    test_file.write_text("hello")
    handler = w.InboxHandler()
    handler._post_to_n8n(test_file)  # must not raise


def test_missing_file_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "WEBHOOK_URL", "http://localhost:5678/webhook/test")
    gone = tmp_path / "gone.pdf"
    # do not create the file — simulate disappearance between event and stat
    handler = w.InboxHandler()
    handler._post_to_n8n(gone)  # must not raise


# ── Syncthing on_moved support ────────────────────────────────────────────────

def _make_moved_event(src_path: str, dest_path: str, is_dir: bool = False):
    event = MagicMock()
    event.src_path = src_path
    event.dest_path = dest_path
    event.is_directory = is_dir
    return event


def test_on_moved_syncthing_rename_is_processed(tmp_path):
    """Syncthing renames .syncthing.<name>.tmp → <name>, producing a moved event."""
    final = tmp_path / "report.pdf"
    final.touch()
    handler = w.InboxHandler()
    event = _make_moved_event(
        str(tmp_path / ".syncthing.report.pdf.tmp"),
        str(final),
    )
    with patch.object(handler, "_post_to_n8n") as mock_post:
        handler.on_moved(event)
        mock_post.assert_called_once_with(final)


def test_on_moved_directory_is_skipped(tmp_path):
    handler = w.InboxHandler()
    event = _make_moved_event(str(tmp_path / "old"), str(tmp_path / "new"), is_dir=True)
    with patch.object(handler, "_post_to_n8n") as mock_post:
        handler.on_moved(event)
        mock_post.assert_not_called()


def test_on_moved_tmp_dest_is_skipped(tmp_path):
    """If the destination is still a temp file, skip it."""
    handler = w.InboxHandler()
    event = _make_moved_event(
        str(tmp_path / "a.tmp"),
        str(tmp_path / "b.tmp"),
    )
    with patch.object(handler, "_post_to_n8n") as mock_post:
        handler.on_moved(event)
        mock_post.assert_not_called()


# ── Startup catch-up scan ─────────────────────────────────────────────────────

def test_scan_existing_posts_eligible_files_only(tmp_path):
    # Regression: files that arrived while the watcher was down were never
    # triaged — scan_existing() posts them on startup.
    (tmp_path / "invoice.pdf").touch()
    (tmp_path / "notes.txt").touch()
    (tmp_path / ".hidden").touch()
    (tmp_path / "partial.tmp").touch()
    (tmp_path / "subdir").mkdir()
    handler = w.InboxHandler()
    with patch.object(handler, "_post_to_n8n") as mock_post:
        posted = w.scan_existing(handler, tmp_path)
    assert posted == 2
    posted_names = {Path(c.args[0]).name for c in mock_post.call_args_list}
    assert posted_names == {"invoice.pdf", "notes.txt"}


def test_scan_existing_empty_dir_posts_nothing(tmp_path):
    handler = w.InboxHandler()
    with patch.object(handler, "_post_to_n8n") as mock_post:
        posted = w.scan_existing(handler, tmp_path)
    assert posted == 0
    mock_post.assert_not_called()
