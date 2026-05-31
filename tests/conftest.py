"""Shared pytest fixtures for the orchestrator test suite."""
import sys
from pathlib import Path

import pytest

# Make orchestrator modules importable from tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "orchestrator"))


@pytest.fixture
def mem_db():
    """In-memory SQLite DB with schema applied. Fresh per test."""
    from db import open_db
    conn = open_db(":memory:")
    yield conn
    conn.close()


@pytest.fixture
def inbox_dir(tmp_path):
    """Temporary inbox directory with a sample text file."""
    d = tmp_path / "inbox"
    d.mkdir()
    (d / "sample.txt").write_text("Please book a dentist appointment for next Tuesday.")
    return d


@pytest.fixture
def good_task():
    return {
        "title": "Book dentist appointment",
        "intent": "create_task",
        "source_type": "email",
        "approval_required": False,
        "confidence": 0.92,
    }
