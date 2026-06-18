#!/usr/bin/env python3
"""
apps/file-watcher/watcher.py
Watches sync/inbox/ for new files and POSTs metadata to the n8n intake webhook.

Requirements:
    pip install watchdog requests

Environment variables:
    N8N_WEBHOOK_URL    -- full URL of the n8n file-intake webhook (required)
    WATCH_DIR          -- directory to watch (default: sync/inbox relative to repo root)
    WATCHER_LOG_LEVEL  -- DEBUG | INFO | WARNING (default: INFO)
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

REPO_ROOT = Path(__file__).resolve().parents[2]
WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")
WATCH_DIR = Path(os.environ.get("WATCH_DIR", str(REPO_ROOT / "sync" / "inbox")))
LOG_LEVEL = os.environ.get("WATCHER_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("file-watcher")

SKIP_SUFFIXES = {".tmp", ".swp", ".part", ".crdownload"}


class InboxHandler(FileSystemEventHandler):
    def on_created(self, event: FileCreatedEvent):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if self._should_skip(path):
            return
        self._post_to_n8n(path)

    def on_moved(self, event: FileMovedEvent):
        # Syncthing writes .syncthing.<name>.tmp then renames → FileMovedEvent.
        # Only care about the destination landing in the watched dir.
        if event.is_directory:
            return
        dest = Path(event.dest_path)
        if self._should_skip(dest):
            return
        self._post_to_n8n(dest)

    def _should_skip(self, path: Path) -> bool:
        if path.name.startswith(".") or path.suffix in SKIP_SUFFIXES:
            logger.debug(f'"Skipping temp/hidden file: {path.name}"')
            return True
        return False

    def _post_to_n8n(self, path: Path, retries: int = 3):
        if not WEBHOOK_URL:
            logger.error('"N8N_WEBHOOK_URL not set — cannot notify n8n"')
            return

        try:
            stat = path.stat()
        except OSError:
            logger.warning(f'"File disappeared before stat: {path.name}"')
            return

        payload = {
            "event": "file_created",
            "file_name": path.name,
            "file_path": str(path),
            "size_bytes": stat.st_size,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "source": "file-watcher",
        }

        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(
                    WEBHOOK_URL,
                    json=payload,
                    timeout=10,
                    headers={"Content-Type": "application/json"},
                )
                resp.raise_for_status()
                logger.info(f'"File posted to n8n: {path.name} (status={resp.status_code})"')
                return
            except requests.RequestException as e:
                logger.warning(f'"Webhook attempt {attempt}/{retries} failed: {e}"')
                if attempt < retries:
                    time.sleep(2 ** attempt)

        logger.error(f'"Failed to post {path.name} after {retries} attempts"')


def main():
    if not WATCH_DIR.is_dir():
        logger.error(f'"Watch directory does not exist: {WATCH_DIR}"')
        sys.exit(1)

    handler = InboxHandler()
    observer = Observer()
    observer.schedule(handler, str(WATCH_DIR), recursive=False)
    observer.start()
    logger.info(f'"Watching {WATCH_DIR} for new files"')

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
