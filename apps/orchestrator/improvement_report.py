"""
apps/orchestrator/improvement_report.py
Turns a promptfoo eval run into a win-rate report and — when a candidate
prompt beats the live one — an approval-gated prompt_revision job.

Nothing auto-deploys: the job carries approval_required=1 and the intent is in
the executor's HIGH_RISK_INTENTS, so promotion happens only after a human
decision in `make approve` (or a one-tap phone approval).

CLI:
    python improvement_report.py report --input var/evals/latest.json
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from db import insert_approval, insert_job, open_db
from notifier import notify_approval_needed

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))
REPORTS_DIR = REPO_ROOT / "var" / "evals" / "reports"
PROMPTS_DIR = REPO_ROOT / "apps" / "orchestrator" / "prompts"
CANDIDATES_DIR = PROMPTS_DIR / "candidates"

# A candidate must beat the current prompt by at least this many percentage
# points before we bother a human with a promotion request.
MIN_WIN_DELTA_PP = 5.0


def parse_promptfoo_results(data: dict) -> dict[str, dict]:
    """
    Per-prompt pass rates from promptfoo's JSON output.
    Returns {prompt_label: {"passed": n, "total": n, "rate": pct}}.
    """
    stats: dict[str, dict] = defaultdict(lambda: {"passed": 0, "total": 0})
    results = data.get("results", {})
    rows = results.get("results", results if isinstance(results, list) else [])
    for row in rows:
        prompt = row.get("prompt", {})
        label = prompt.get("label") or prompt.get("display") or prompt.get("raw", "?")[:60]
        stats[label]["total"] += 1
        if row.get("success"):
            stats[label]["passed"] += 1
    for s in stats.values():
        s["rate"] = round(100.0 * s["passed"] / s["total"], 1) if s["total"] else 0.0
    return dict(stats)


def _is_candidate(label: str) -> bool:
    return "candidates/" in label or "candidates\\" in label


def build_report(stats: dict[str, dict]) -> str:
    lines = ["# Prompt improvement report", ""]
    for label, s in sorted(stats.items(), key=lambda kv: -kv[1]["rate"]):
        kind = "candidate" if _is_candidate(label) else "current"
        lines.append(f"- **{label}** ({kind}): {s['passed']}/{s['total']} — {s['rate']}%")
    return "\n".join(lines) + "\n"


def find_winning_candidate(stats: dict[str, dict]) -> dict | None:
    """Best candidate that beats the best current prompt by ≥ MIN_WIN_DELTA_PP."""
    current = [(l, s) for l, s in stats.items() if not _is_candidate(l)]
    candidates = [(l, s) for l, s in stats.items() if _is_candidate(l)]
    if not current or not candidates:
        return None
    best_current = max(current, key=lambda kv: kv[1]["rate"])
    best_candidate = max(candidates, key=lambda kv: kv[1]["rate"])
    delta = best_candidate[1]["rate"] - best_current[1]["rate"]
    if delta < MIN_WIN_DELTA_PP:
        return None
    return {
        "candidate_label": best_candidate[0],
        "candidate_rate": best_candidate[1]["rate"],
        "current_label": best_current[0],
        "current_rate": best_current[1]["rate"],
        "delta_pp": round(delta, 1),
    }


def propose_promotion(conn, winner: dict, *, report_path: str) -> dict | None:
    """Insert the approval-gated prompt_revision job for a winning candidate."""
    # Resolve the candidate file and the live prompt it would replace: a
    # candidate named triage-agent.md replaces prompts/triage-agent.md.
    label = winner["candidate_label"]
    candidate_name = Path(label.split("file://")[-1]).name
    candidate_path = CANDIDATES_DIR / candidate_name
    target_path = PROMPTS_DIR / candidate_name

    payload = {
        "title": (f"Promote prompt candidate {candidate_name}: "
                  f"{winner['candidate_rate']}% vs {winner['current_rate']}% "
                  f"(+{winner['delta_pp']}pp)"),
        "intent": "prompt_revision",
        "source_type": "webhook",
        "approval_required": True,
        "confidence": 1.0,
        "candidate_path": str(candidate_path.relative_to(REPO_ROOT)),
        "target_path": str(target_path.relative_to(REPO_ROOT)),
        "report_path": report_path,
        "win": winner,
    }
    job_id = insert_job(
        conn,
        source_type="webhook",
        source_ref=f"improvement:{candidate_name}:{winner['delta_pp']}",
        payload=payload,
        intent="prompt_revision",
        approval_required=True,
    )
    if job_id is None:
        return None  # same proposal already pending
    approval_id = insert_approval(
        conn, job_id=job_id,
        requested_action=payload["title"],
    )
    notify_approval_needed(payload, approval_id)
    return {"job_id": job_id, "approval_id": approval_id, **winner}


def run(conn, *, input_path: Path, stamp: str) -> dict:
    data = json.loads(input_path.read_text())
    stats = parse_promptfoo_results(data)
    report = build_report(stats)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{stamp}.md"
    report_path.write_text(report)
    try:
        report_ref = str(report_path.relative_to(REPO_ROOT))
    except ValueError:
        report_ref = str(report_path)

    winner = find_winning_candidate(stats)
    proposal = None
    if winner:
        proposal = propose_promotion(conn, winner, report_path=report_ref)
    return {"report": str(report_path), "prompts": len(stats),
            "winner": winner, "proposal": proposal}


def main() -> None:
    parser = argparse.ArgumentParser(description="Prompt improvement report")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report")
    rep.add_argument("--input", required=True)
    rep.add_argument("--stamp", default="latest")
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        result = run(conn, input_path=Path(args.input), stamp=args.stamp)
        print(json.dumps(result, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
