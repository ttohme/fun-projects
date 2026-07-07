"""Smoke tests for scripts/backup-db.sh — the DB backup capability."""
import gzip
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "backup-db.sh"
SCHEMA = REPO_ROOT / "db" / "schema.sql"


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text())
    conn.execute(
        "INSERT INTO jobs (source_type, source_ref, dedupe_key, intent, payload_json) "
        "VALUES ('email', 'ref-1', 'key-1', 'create_task', '{}')"
    )
    conn.commit()
    conn.close()


def _run(db_path: Path, backup_dir: Path, keep: str = "14"):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "DB_PATH": str(db_path),
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_KEEP": keep,
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
        capture_output=True,
        text=True,
    )


def test_backup_creates_compressed_snapshot(tmp_path):
    db = tmp_path / "assistant.db"
    backups = tmp_path / "backups"
    _make_db(db)

    result = _run(db, backups)
    assert result.returncode == 0, result.stderr

    snaps = list(backups.glob("assistant-*.db.gz"))
    assert len(snaps) == 1


def test_backup_snapshot_is_a_valid_db(tmp_path):
    db = tmp_path / "assistant.db"
    backups = tmp_path / "backups"
    _make_db(db)
    _run(db, backups)

    snap = next(backups.glob("assistant-*.db.gz"))
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(snap.read_bytes()))

    conn = sqlite3.connect(restored)
    count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    assert count == 1
    assert integrity == "ok"


def test_backup_missing_db_fails(tmp_path):
    result = _run(tmp_path / "nonexistent.db", tmp_path / "backups")
    assert result.returncode != 0


def test_backup_rotation_keeps_only_n(tmp_path):
    db = tmp_path / "assistant.db"
    backups = tmp_path / "backups"
    _make_db(db)
    backups.mkdir()
    # Pre-seed 5 old snapshots; with KEEP=2 only the newest 2 should remain after.
    for i in range(5):
        (backups / f"assistant-2020010{i}T000000Z.db.gz").write_bytes(b"old")

    result = _run(db, backups, keep="2")
    assert result.returncode == 0, result.stderr
    remaining = sorted(backups.glob("assistant-*.db.gz"))
    assert len(remaining) == 2
