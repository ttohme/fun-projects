import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from approval_miner import build_case, get_last_run, mine, propose_rules
from db import insert_approval, insert_job, open_db, record_approval_decision
from improvement_report import (
    find_winning_candidate,
    parse_promptfoo_results,
    run as report_run,
)
from openclaw_log_miner import aggregate, parse_lines, to_markdown


def fresh_db():
    return open_db(":memory:")


def _decided(conn, *, decision, title="Send reply to Bob", intent="send_email",
             labels=None, reason=None, ref=None):
    payload = {"title": title, "intent": intent, "source_type": "email",
               "approval_required": True, "confidence": 0.8,
               "labels": labels or []}
    job_id = insert_job(conn, source_type="email",
                        source_ref=ref or f"e-{title[:20]}",
                        payload=payload, intent=intent, approval_required=True)
    approval_id = insert_approval(conn, job_id=job_id, requested_action=title)
    record_approval_decision(conn, approval_id, decision, reason=reason)
    return approval_id


# ── approval_miner ────────────────────────────────────────────────────────────

def test_rejected_decision_becomes_gating_case(tmp_path):
    conn = fresh_db()
    aid = _decided(conn, decision="rejected", reason="never email automatically")
    counts = mine(conn, out_dir=tmp_path)
    assert counts["written"] == 1
    case = yaml.safe_load((tmp_path / f"approval-{aid}.yaml").read_text())[0]
    assert "rejected" in case["description"]
    assert "never email automatically" in case["description"]
    js = [a for a in case["assert"] if a["type"] == "javascript"][0]
    assert "approval_required === true" in js["value"]


def test_private_payloads_never_mined(tmp_path):
    conn = fresh_db()
    _decided(conn, decision="rejected", title="Therapy appointment notes",
             labels=["private"])
    counts = mine(conn, out_dir=tmp_path)
    assert counts["written"] == 0
    assert counts["skipped_private_or_empty"] == 1
    assert list(tmp_path.glob("*.yaml")) == []


def test_mine_is_incremental(tmp_path):
    conn = fresh_db()
    _decided(conn, decision="rejected", title="First", ref="r1")
    assert mine(conn, out_dir=tmp_path)["written"] == 1
    # Second run with no new decisions mines nothing.
    assert mine(conn, out_dir=tmp_path)["decided"] == 0
    assert get_last_run(conn) > "2020"


def test_approved_cases_are_sampled():
    conn = fresh_db()
    for i in range(6):
        _decided(conn, decision="approved", title=f"Task {i}",
                 intent="create_task", ref=f"a{i}")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        counts = mine(conn, out_dir=Path(td))
    assert counts["written"] == 2      # 1 in 3 kept
    assert counts["sampled_out"] == 4


def test_propose_rules_requires_unanimous_approvals():
    conn = fresh_db()
    for i in range(5):
        _decided(conn, decision="approved", title=f"T{i}",
                 intent="create_task", ref=f"p{i}")
    _decided(conn, decision="rejected", title="Bad email",
             intent="send_email", ref="p-reject")
    rules = propose_rules(conn, min_count=5)
    assert len(rules) == 1
    assert rules[0]["intent"] == "create_task"


# ── improvement_report ────────────────────────────────────────────────────────

PROMPTFOO_FIXTURE = {
    "results": {
        "results": (
            [{"prompt": {"label": "file://apps/orchestrator/prompts/triage-agent.md"},
              "success": i < 6} for i in range(10)]      # current: 60%
            + [{"prompt": {"label": "file://apps/orchestrator/prompts/candidates/triage-agent.md"},
                "success": i < 9} for i in range(10)]    # candidate: 90%
        )
    }
}


def test_parse_and_winner_detection():
    stats = parse_promptfoo_results(PROMPTFOO_FIXTURE)
    assert stats["file://apps/orchestrator/prompts/triage-agent.md"]["rate"] == 60.0
    winner = find_winning_candidate(stats)
    assert winner is not None
    assert winner["delta_pp"] == 30.0


def test_no_promotion_below_threshold():
    fixture = {"results": {"results": (
        [{"prompt": {"label": "file://p/triage-agent.md"}, "success": i < 8} for i in range(10)]
        + [{"prompt": {"label": "file://p/candidates/triage-agent.md"}, "success": i < 8}
           for i in range(10)]
    )}}
    assert find_winning_candidate(parse_promptfoo_results(fixture)) is None


def test_report_inserts_gated_prompt_revision_job(tmp_path):
    import improvement_report
    conn = fresh_db()
    input_file = tmp_path / "latest.json"
    input_file.write_text(json.dumps(PROMPTFOO_FIXTURE))
    with patch.object(improvement_report, "REPORTS_DIR", tmp_path / "reports"), \
         patch("improvement_report.notify_approval_needed"):
        result = report_run(conn, input_path=input_file, stamp="test")
    assert result["proposal"] is not None
    job = conn.execute("SELECT * FROM jobs WHERE intent='prompt_revision'").fetchone()
    assert job is not None
    assert job["approval_required"] == 1
    assert job["status"] == "pending"
    approval = conn.execute("SELECT * FROM approvals WHERE job_id=?",
                            (job["id"],)).fetchone()
    assert approval is not None
    assert (tmp_path / "reports" / "test.md").exists()


def test_prompt_revision_never_auto_proceeds(tmp_path):
    # The executor must re-flag a prompt_revision even if mis-flagged auto.
    from job_executor import HIGH_RISK_INTENTS, run_once
    assert "prompt_revision" in HIGH_RISK_INTENTS
    conn = fresh_db()
    job_id = insert_job(conn, source_type="webhook", source_ref="imp-1",
                        payload={"title": "Promote prompt", "intent": "prompt_revision"},
                        intent="prompt_revision", approval_required=False)
    with patch("job_executor._dispatch") as mock:
        run_once(conn)
    mock.assert_not_called()
    row = conn.execute("SELECT approval_required FROM jobs WHERE id=?",
                       (job_id,)).fetchone()
    assert row["approval_required"] == 1


def test_approved_promotion_copies_and_archives(tmp_path):
    import job_executor
    from job_executor import _execute_prompt_revision
    prompts = tmp_path / "apps" / "orchestrator" / "prompts"
    (prompts / "candidates").mkdir(parents=True)
    (prompts / "triage-agent.md").write_text("OLD PROMPT")
    (prompts / "candidates" / "triage-agent.md").write_text("NEW PROMPT")
    with patch.object(job_executor, "REPO_ROOT", tmp_path):
        result = _execute_prompt_revision({
            "candidate_path": "apps/orchestrator/prompts/candidates/triage-agent.md",
            "target_path": "apps/orchestrator/prompts/triage-agent.md",
        })
    assert (prompts / "triage-agent.md").read_text() == "NEW PROMPT"
    archived = list((prompts / "archive").glob("triage-agent.*.md"))
    assert len(archived) == 1
    assert archived[0].read_text() == "OLD PROMPT"
    assert result["archived_previous"]


def test_promotion_rejects_path_traversal(tmp_path):
    import job_executor
    from job_executor import _execute_prompt_revision
    with patch.object(job_executor, "REPO_ROOT", tmp_path):
        with pytest.raises(ValueError, match="escapes"):
            _execute_prompt_revision({
                "candidate_path": "../../etc/passwd",
                "target_path": "apps/orchestrator/prompts/triage-agent.md",
            })


# ── openclaw_log_miner ────────────────────────────────────────────────────────

LOG_LINES = [
    json.dumps({"tag": "agent", "model": "local-agent", "status": "ok",
                "fallback": False, "latency_ms": 800}),
    json.dumps({"tag": "agent", "model": "assistant-small", "status": "ok",
                "fallback": True, "latency_ms": 1200}),
    json.dumps({"MESSAGE": json.dumps({"tag": "private", "model": "local-private",
                                       "status": "error", "latency_ms": 50})}),
    "not json at all",
] + [json.dumps({"tag": "agent", "model": "assistant-small", "status": "ok",
                 "fallback": True, "latency_ms": 900})] * 4


def test_log_miner_aggregates_and_flags_fallback_anomaly():
    records = parse_lines(LOG_LINES)
    assert len(records) == 7  # bad line dropped, journald wrapper unwrapped
    agg = aggregate(records)
    assert agg["tags"]["agent"]["requests"] == 6
    assert agg["tags"]["agent"]["fallback_pct"] > 80
    assert any("fell back" in a for a in agg["anomalies"])
    md = to_markdown(agg)
    assert "OpenClaw routing" in md and "⚠" in md
