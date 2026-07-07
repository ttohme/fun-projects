import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "orchestrator"))

from todoist_client import (
    TodoistTask,
    create_task,
    execute_approved_job,
    find_project_id,
    get_projects,
)

FAKE_TOKEN = "test-token-123"

PROJECTS = [
    {"id": "proj-1", "name": "Inbox"},
    {"id": "proj-2", "name": "Work"},
    {"id": "proj-3", "name": "Personal"},
]


def _mock_get(json_data, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _mock_post(json_data, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


# ── get_projects ──────────────────────────────────────────────────────────────

def test_get_projects_returns_list():
    with patch("todoist_client.requests.get", return_value=_mock_get(PROJECTS)):
        result = get_projects(token=FAKE_TOKEN)
    assert len(result) == 3
    assert result[0]["name"] == "Inbox"


def test_get_projects_sends_bearer_token():
    with patch("todoist_client.requests.get", return_value=_mock_get(PROJECTS)) as mock_get:
        get_projects(token=FAKE_TOKEN)
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


# ── find_project_id ───────────────────────────────────────────────────────────

def test_find_project_id_found():
    with patch("todoist_client.get_projects", return_value=PROJECTS):
        pid = find_project_id("Work", token=FAKE_TOKEN)
    assert pid == "proj-2"


def test_find_project_id_case_insensitive():
    with patch("todoist_client.get_projects", return_value=PROJECTS):
        pid = find_project_id("work", token=FAKE_TOKEN)
    assert pid == "proj-2"


def test_find_project_id_not_found():
    with patch("todoist_client.get_projects", return_value=PROJECTS):
        pid = find_project_id("Nonexistent", token=FAKE_TOKEN)
    assert pid is None


# ── create_task ───────────────────────────────────────────────────────────────

CREATED_TASK = {"id": "task-123", "content": "Buy milk", "priority": 4}


def test_create_task_posts_content():
    with patch("todoist_client.requests.post", return_value=_mock_post(CREATED_TASK)) as mock_post:
        task = TodoistTask(content="Buy milk")
        result = create_task(task, token=FAKE_TOKEN)
    assert result["id"] == "task-123"
    body = mock_post.call_args.kwargs["json"]
    assert body["content"] == "Buy milk"


def test_create_task_includes_due_string():
    with patch("todoist_client.requests.post", return_value=_mock_post(CREATED_TASK)) as mock_post:
        task = TodoistTask(content="Call dentist", due_string="tomorrow at 9am")
        create_task(task, token=FAKE_TOKEN)
    body = mock_post.call_args.kwargs["json"]
    assert body["due_string"] == "tomorrow at 9am"


def test_create_task_includes_labels():
    with patch("todoist_client.requests.post", return_value=_mock_post(CREATED_TASK)) as mock_post:
        task = TodoistTask(content="Review contract", labels=["review-required"])
        create_task(task, token=FAKE_TOKEN)
    body = mock_post.call_args.kwargs["json"]
    assert "review-required" in body["labels"]


def test_create_task_omits_empty_optional_fields():
    with patch("todoist_client.requests.post", return_value=_mock_post(CREATED_TASK)) as mock_post:
        task = TodoistTask(content="Simple task")
        create_task(task, token=FAKE_TOKEN)
    body = mock_post.call_args.kwargs["json"]
    assert "due_string" not in body
    assert "project_id" not in body
    assert "labels" not in body


def test_create_task_raises_without_token():
    with pytest.raises(EnvironmentError, match="TODOIST_API_TOKEN"):
        create_task(TodoistTask(content="x"), token="")


# ── execute_approved_job ──────────────────────────────────────────────────────

def test_execute_approved_job_maps_payload():
    payload = {
        "title": "Book dentist appointment",
        "project": "Personal",
        "due_string": "next Monday",
        "priority": 2,
        "labels": ["health"],
        "description": "Call Dr. Smith",
    }
    with patch("todoist_client.find_project_id", return_value="proj-3"), \
         patch("todoist_client.create_task", return_value=CREATED_TASK) as mock_create:
        result = execute_approved_job(payload, token=FAKE_TOKEN)
    assert result["id"] == "task-123"
    task_arg = mock_create.call_args.args[0]
    assert task_arg.content == "Book dentist appointment"
    assert task_arg.due_string == "next Monday"
    assert task_arg.priority == 2
    assert "health" in task_arg.labels


def test_execute_approved_job_inbox_skips_project_lookup():
    payload = {"title": "Quick note", "project": "Inbox", "priority": 4, "labels": []}
    with patch("todoist_client.find_project_id") as mock_find, \
         patch("todoist_client.create_task", return_value=CREATED_TASK):
        execute_approved_job(payload, token=FAKE_TOKEN)
    mock_find.assert_not_called()
