import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from db import open_db
from triage import (
    CONFIDENCE_THRESHOLD,
    HIGH_RISK_INTENTS,
    _strip_frontmatter,
    render_prompt,
    should_require_approval,
    triage,
)


# ── _strip_frontmatter ────────────────────────────────────────────────────────

def test_strip_frontmatter_removes_yaml_block():
    text = "---\nmodel: planner\n---\n\nActual prompt content"
    result = _strip_frontmatter(text)
    assert result == "Actual prompt content"

def test_strip_frontmatter_no_frontmatter():
    text = "Just a plain prompt"
    assert _strip_frontmatter(text) == text


# ── render_prompt ─────────────────────────────────────────────────────────────

def test_render_prompt_substitutes_variables():
    template = "Source: {{source_type}}\nRef: {{source_ref}}\nContent: {{input_content}}"
    rendered = render_prompt(template, input_content="hello", source_type="email", source_ref="id-1")
    assert "email" in rendered
    assert "id-1" in rendered
    assert "hello" in rendered
    assert "{{" not in rendered


# ── should_require_approval ───────────────────────────────────────────────────

def test_high_risk_intent_requires_approval():
    for intent in HIGH_RISK_INTENTS:
        task = {"intent": intent, "confidence": 0.95, "approval_required": False}
        assert should_require_approval(task) is True

def test_low_confidence_requires_approval():
    task = {"intent": "create_task", "confidence": CONFIDENCE_THRESHOLD - 0.01,
            "approval_required": False}
    assert should_require_approval(task) is True

def test_high_confidence_create_task_no_approval():
    task = {"intent": "create_task", "confidence": 0.95, "approval_required": False}
    assert should_require_approval(task) is False

def test_model_sets_approval_required_true():
    task = {"intent": "create_task", "confidence": 0.95, "approval_required": True}
    assert should_require_approval(task) is True


# ── triage (integration, with mocked LiteLLM) ────────────────────────────────

GOOD_TASK = {
    "title": "Book dentist appointment",
    "intent": "create_task",
    "source_type": "email",
    "approval_required": False,
    "confidence": 0.92,
}

def _mock_call_litellm(task_dict: dict):
    """Patch call_litellm to return the given task as JSON."""
    return patch("triage.call_litellm", return_value=json.dumps(task_dict))


def test_triage_dry_run_returns_task():
    with _mock_call_litellm(GOOD_TASK):
        result = triage(
            input_content="Please book me a dentist appointment",
            source_type="email",
            source_ref="email-001",
            dry_run=True,
        )
    assert result["title"] == "Book dentist appointment"
    assert result["intent"] == "create_task"
    assert "_job_id" not in result


def test_triage_writes_job_to_db(tmp_path):
    db = tmp_path / "test.db"
    with _mock_call_litellm(GOOD_TASK):
        result = triage(
            input_content="Book dentist",
            source_type="email",
            source_ref="email-002",
            db_path=db,
        )
    assert "_job_id" in result
    conn = open_db(db)
    rows = conn.execute("SELECT * FROM jobs WHERE id = ?", (result["_job_id"],)).fetchone()
    assert rows["status"] == "pending"
    assert rows["intent"] == "create_task"


def test_triage_duplicate_sets_skipped_flag(tmp_path):
    db = tmp_path / "test.db"
    task = {**GOOD_TASK, "dedupe_key": "fixed-key"}
    with _mock_call_litellm(task):
        triage(input_content="x", source_type="email", source_ref="dup-ref", db_path=db)
        result = triage(input_content="x", source_type="email", source_ref="dup-ref", db_path=db)
    assert result.get("_skipped") == "duplicate"


def test_triage_high_risk_intent_forces_approval(tmp_path):
    db = tmp_path / "test.db"
    task = {**GOOD_TASK, "intent": "send_email", "approval_required": False}
    with _mock_call_litellm(task):
        result = triage(
            input_content="Send a reply to John",
            source_type="email",
            source_ref="email-003",
            db_path=db,
        )
    assert result["approval_required"] is True
    assert "_approval_id" in result


def test_triage_low_confidence_forces_approval(tmp_path):
    db = tmp_path / "test.db"
    task = {**GOOD_TASK, "confidence": 0.5, "approval_required": False}
    with _mock_call_litellm(task):
        result = triage(
            input_content="Maybe do something",
            source_type="email",
            source_ref="email-004",
            db_path=db,
        )
    assert result["approval_required"] is True


def test_triage_invalid_schema_raises(tmp_path):
    from schema_validator import ValidationError
    bad_task = {"title": "No required fields"}  # missing intent, source_type, etc.
    with _mock_call_litellm(bad_task):
        try:
            triage(input_content="x", source_type="email",
                   source_ref="email-005", dry_run=True)
            assert False, "Should have raised ValidationError"
        except (ValidationError, Exception) as e:
            assert "invalid" in str(e).lower() or "missing" in str(e).lower()
