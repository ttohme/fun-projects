"""
apps/orchestrator/todoist_client.py
Thin Todoist REST API client for executing approved jobs.

Used after an approval is recorded. Calls the Todoist REST API directly
(not MCP) for programmatic job execution from the approval service.

Environment variables:
    TODOIST_API_TOKEN  (required)
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import requests

TODOIST_API_BASE = "https://api.todoist.com/rest/v2"
_TOKEN = os.environ.get("TODOIST_API_TOKEN", "")


@dataclass
class TodoistTask:
    content: str
    project_id: str | None = None
    due_string: str | None = None
    priority: int = 4
    labels: list[str] = field(default_factory=list)
    description: str | None = None


def _headers(token: str = "") -> dict:
    tok = token or _TOKEN
    if not tok:
        raise EnvironmentError("TODOIST_API_TOKEN is not set")
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }


def get_projects(token: str = "") -> list[dict]:
    resp = requests.get(f"{TODOIST_API_BASE}/projects", headers=_headers(token), timeout=10)
    resp.raise_for_status()
    return resp.json()


def find_project_id(name: str, token: str = "") -> str | None:
    """Return project id for the given name, or None if not found."""
    for project in get_projects(token):
        if project.get("name", "").lower() == name.lower():
            return project["id"]
    return None


def create_task(task: TodoistTask, token: str = "") -> dict:
    """Create a Todoist task. Returns the created task object."""
    body: dict = {"content": task.content, "priority": task.priority}
    if task.project_id:
        body["project_id"] = task.project_id
    if task.due_string:
        body["due_string"] = task.due_string
    if task.labels:
        body["labels"] = task.labels
    if task.description:
        body["description"] = task.description

    resp = requests.post(
        f"{TODOIST_API_BASE}/tasks",
        headers=_headers(token),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_task(task_id: str, token: str = "") -> dict:
    resp = requests.get(
        f"{TODOIST_API_BASE}/tasks/{task_id}",
        headers=_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def close_task(task_id: str, token: str = "") -> None:
    resp = requests.post(
        f"{TODOIST_API_BASE}/tasks/{task_id}/close",
        headers=_headers(token),
        timeout=10,
    )
    resp.raise_for_status()


def execute_approved_job(job_payload: dict, token: str = "") -> dict:
    """
    Convert an approved job payload into a Todoist task.
    Returns the created task dict from the Todoist API.
    """
    project_name = job_payload.get("project", "Inbox")
    project_id = find_project_id(project_name, token) if project_name != "Inbox" else None

    task = TodoistTask(
        content=job_payload.get("title", "(untitled)"),
        project_id=project_id,
        due_string=job_payload.get("due_string"),
        priority=job_payload.get("priority", 4),
        labels=job_payload.get("labels", []),
        description=job_payload.get("description"),
    )
    return create_task(task, token)
