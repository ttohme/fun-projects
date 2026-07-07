"""
apps/orchestrator/approval_miner.py
Turns your approve/reject decisions into promptfoo regression cases.

Every decided approval is a labeled example of what the triage prompt should
have done:
    rejected → the model produced a job a human refused; the mined case
               asserts that this input must come out approval-gated
               (approval_required=true) so it can never auto-proceed again
    approved → sampled positive case asserting the recorded intent sticks

Privacy: payloads labeled "private" are skipped entirely — mined cases are
committed to git. The reconstruction uses only the classified payload (title +
description), never raw source content, which the DB doesn't store anyway.

Incremental: last_run lives in a `meta` table; each run mines only decisions
made since. Files land in evals/cases/mined/ (machine-generated — add new
cases by hand as new files, don't edit mined ones in place).

CLI:
    python approval_miner.py mine [--out evals/cases/mined]
    python approval_miner.py propose-rules   # deterministic pre-triage rule candidates
"""
import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from db import _now, open_db

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))
MINED_DIR = REPO_ROOT / "evals" / "cases" / "mined"

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

LAST_RUN_KEY = "approval_miner_last_run"
POSITIVE_SAMPLE_EVERY = 3  # keep 1 in N approved cases; rejections are all kept


def ensure_meta(conn) -> None:
    conn.executescript(_META_SCHEMA)
    conn.commit()


def get_last_run(conn) -> str:
    ensure_meta(conn)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (LAST_RUN_KEY,)).fetchone()
    return row["value"] if row else "1970-01-01T00:00:00.000Z"


def set_last_run(conn, value: str) -> None:
    ensure_meta(conn)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (LAST_RUN_KEY, value),
    )
    conn.commit()


def _decided_since(conn, since: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id AS approval_id, a.decision, a.reason, a.decided_at,
               j.id AS job_id, j.intent, j.source_type, j.payload_json, j.dedupe_key
        FROM   approvals a
        JOIN   jobs j ON j.id = a.job_id
        WHERE  a.decision IS NOT NULL AND a.decided_at > ?
        ORDER  BY a.decided_at
        """,
        (since,),
    ).fetchall()
    return [dict(r) for r in rows]


def _reconstruct_input(payload: dict) -> str:
    """Pseudo-input from the classified payload (raw source isn't stored)."""
    parts = [payload.get("title", "")]
    if payload.get("description"):
        parts.append(payload["description"])
    return "\n".join(p for p in parts if p)


def build_case(decision_row: dict) -> dict | None:
    """One promptfoo test case from a decided approval; None when skipped."""
    payload = json.loads(decision_row["payload_json"])
    if "private" in (payload.get("labels") or []):
        return None
    input_content = _reconstruct_input(payload)
    if not input_content.strip():
        return None

    base = {
        "vars": {
            "input_content": input_content,
            "source_type": decision_row["source_type"],
            "source_ref": f"mined-{decision_row['approval_id']}",
        },
        "assert": [{"type": "is-json"}],
    }
    if decision_row["decision"] == "rejected":
        base["description"] = (
            f"mined-rejected #{decision_row['approval_id']}: "
            f"{payload.get('title', '')[:60]}"
            + (f" (reason: {decision_row['reason'][:80]})" if decision_row["reason"] else "")
        )
        base["assert"].append({
            "type": "javascript",
            "value": "const o = JSON.parse(output); return o.approval_required === true;",
        })
    else:
        base["description"] = (
            f"mined-approved #{decision_row['approval_id']}: "
            f"{payload.get('title', '')[:60]}"
        )
        base["assert"].append({
            "type": "javascript",
            "value": f"const o = JSON.parse(output); return o.intent === '{decision_row['intent']}';",
        })
    return base


def mine(conn, *, out_dir: Path | None = None) -> dict:
    """Mine decisions since last run into YAML case files. Returns counts."""
    out = out_dir or MINED_DIR
    out.mkdir(parents=True, exist_ok=True)
    since = get_last_run(conn)
    rows = _decided_since(conn, since)

    written, skipped, sampled_out = 0, 0, 0
    approved_seen = 0
    for row in rows:
        if row["decision"] == "approved":
            approved_seen += 1
            if approved_seen % POSITIVE_SAMPLE_EVERY != 0:
                sampled_out += 1
                continue
        case = build_case(row)
        if case is None:
            skipped += 1
            continue
        path = out / f"approval-{row['approval_id']}.yaml"
        path.write_text(yaml.safe_dump([case], sort_keys=False, allow_unicode=True))
        written += 1

    set_last_run(conn, _now())
    return {"decided": len(rows), "written": written,
            "skipped_private_or_empty": skipped, "sampled_out": sampled_out}


def propose_rules(conn, *, min_count: int = 5) -> list[dict]:
    """
    Deterministic pre-triage rule candidates: (source_type, intent) pairs that
    humans approved every single time, at least `min_count` times. Proposals
    only — nothing is applied without going through an approval.
    """
    rows = conn.execute(
        """
        SELECT j.source_type, j.intent,
               COUNT(*) AS n,
               SUM(CASE WHEN a.decision = 'approved' THEN 1 ELSE 0 END) AS approved_n
        FROM   approvals a
        JOIN   jobs j ON j.id = a.job_id
        WHERE  a.decision IS NOT NULL
        GROUP  BY j.source_type, j.intent
        HAVING n >= ? AND approved_n = n
        """,
        (min_count,),
    ).fetchall()
    return [
        {"source_type": r["source_type"], "intent": r["intent"],
         "observations": r["n"],
         "proposal": (f"{r['source_type']}/{r['intent']} was approved {r['n']}/{r['n']} "
                      f"times — consider auto-proceeding this combination")}
        for r in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Mine approval decisions into eval cases")
    sub = parser.add_subparsers(dest="command", required=True)
    mine_p = sub.add_parser("mine")
    mine_p.add_argument("--out", default=str(MINED_DIR))
    sub.add_parser("propose-rules")
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        if args.command == "mine":
            print(json.dumps(mine(conn, out_dir=Path(args.out))))
        elif args.command == "propose-rules":
            print(json.dumps(propose_rules(conn), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
