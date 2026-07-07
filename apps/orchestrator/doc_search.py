"""
apps/orchestrator/doc_search.py
Full-text search over everything the pipeline has processed.

SQLite FTS5 (built into the Pi's sqlite3, zero ML, always available). Files
are indexed automatically as document_processor finishes them; `make ask`
searches from the terminal, and an n8n webhook can expose the same query.

Upgrade path (roadmap 5.1): a sqlite-vec + GPU-embedding layer can sit next
to this — FTS5 stays as the deterministic fallback when the box is off.

CLI:
    python doc_search.py index <path>       # index one file
    python doc_search.py reindex            # rebuild from sync/processed/
    python doc_search.py ask "water bill"
"""
import argparse
import json
import os
import sys
from pathlib import Path

from db import _now, open_db

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))
PROCESSED_DIR = REPO_ROOT / "sync" / "processed"

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS doc_index USING fts5(
    path, title, content, indexed_at UNINDEXED
);
"""


def ensure_table(conn) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def index_text(conn, *, path: str, content: str, title: str = "") -> None:
    """Insert or replace one document in the index."""
    if not content.strip():
        return
    ensure_table(conn)
    conn.execute("DELETE FROM doc_index WHERE path = ?", (path,))
    conn.execute(
        "INSERT INTO doc_index (path, title, content, indexed_at) VALUES (?, ?, ?, ?)",
        (path, title or Path(path).name, content[:200_000], _now()),
    )
    conn.commit()


def index_file(conn, path: Path) -> bool:
    """Extract text (reusing the pipeline's readers) and index it."""
    from document_processor import read_file_text
    content = read_file_text(path)
    if content.startswith("[Unsupported") or not content.strip():
        return False
    index_text(conn, path=str(path), content=content)
    return True


def reindex(conn, directory: Path | None = None) -> int:
    count = 0
    for path in sorted((directory or PROCESSED_DIR).iterdir()):
        if path.is_file() and not path.name.startswith("."):
            if index_file(conn, path):
                count += 1
    return count


def search(conn, query: str, *, limit: int = 8) -> list[dict]:
    ensure_table(conn)
    rows = conn.execute(
        """
        SELECT path, title,
               snippet(doc_index, 2, '»', '«', '…', 18) AS snippet,
               rank
        FROM   doc_index
        WHERE  doc_index MATCH ?
        ORDER  BY rank LIMIT ?
        """,
        (query, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Document search (FTS5)")
    sub = parser.add_subparsers(dest="command", required=True)
    idx = sub.add_parser("index")
    idx.add_argument("path")
    sub.add_parser("reindex")
    ask = sub.add_parser("ask")
    ask.add_argument("query")
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        if args.command == "index":
            ok = index_file(conn, Path(args.path))
            print(json.dumps({"indexed": ok}))
        elif args.command == "reindex":
            print(json.dumps({"indexed": reindex(conn)}))
        elif args.command == "ask":
            hits = search(conn, args.query)
            if not hits:
                print("No matches.")
            for h in hits:
                print(f"  {h['title']}\n    {h['snippet']}\n    {h['path']}\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
