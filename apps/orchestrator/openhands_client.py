"""
apps/orchestrator/openhands_client.py
Thin client for dispatching approved code_job intents to OpenHands.

OpenHands runs Docker-sandboxed on the on-demand Windows box (port 3333) — the
AGENTS.md-mandated sandbox for code work and dependency installs. A code_job
is always approval-gated upstream; this client only fires after a human said
yes. If the box is off, the connection error surfaces as a normal transient
failure and the executor's retry/backoff ladder keeps the job alive.

The OpenHands REST surface has shifted between releases; the conversation
endpoints used here match the 0.3x API. If a newer image changes them, this
module is the only place to update.

Environment variables:
    OPENHANDS_URL   e.g. http://windows-pc.tailnet.ts.net:3333
    OPENHANDS_POLL_SECONDS   (default 15)
    OPENHANDS_TIMEOUT_SECONDS (default 1800 — code jobs are slow)
"""
import os
import time

import requests

OPENHANDS_URL = os.environ.get("OPENHANDS_URL", "")
POLL_SECONDS = int(os.environ.get("OPENHANDS_POLL_SECONDS", "15"))
TIMEOUT_SECONDS = int(os.environ.get("OPENHANDS_TIMEOUT_SECONDS", "1800"))

TERMINAL_STATES = {"finished", "stopped", "error", "failed"}


def _base() -> str:
    if not OPENHANDS_URL:
        raise EnvironmentError("OPENHANDS_URL is not set")
    return OPENHANDS_URL.rstrip("/")


def start_conversation(task_description: str, *, repo: str = "") -> str:
    """Kick off an OpenHands conversation; returns its id."""
    body: dict = {"initial_user_msg": task_description}
    if repo:
        body["selected_repository"] = repo
    resp = requests.post(f"{_base()}/api/conversations", json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    conv_id = data.get("conversation_id") or data.get("id")
    if not conv_id:
        raise ValueError(f"OpenHands returned no conversation id: {data!r}")
    return str(conv_id)


def get_status(conversation_id: str) -> dict:
    resp = requests.get(f"{_base()}/api/conversations/{conversation_id}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def run_code_job(payload: dict) -> dict:
    """
    Execute one approved code_job: start a conversation, poll to a terminal
    state (bounded by OPENHANDS_TIMEOUT_SECONDS), return the outcome.
    """
    description = payload.get("description") or payload.get("title", "")
    if not description:
        raise ValueError("code_job payload has no description/title")
    repo = payload.get("repo", "")

    conv_id = start_conversation(description, repo=repo)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    status = {}
    while time.monotonic() < deadline:
        status = get_status(conv_id)
        state = str(status.get("status", "")).lower()
        if state in TERMINAL_STATES:
            return {
                "conversation_id": conv_id,
                "state": state,
                "url": f"{_base()}/conversations/{conv_id}",
            }
        time.sleep(POLL_SECONDS)

    # Timed out waiting — the sandbox keeps running; hand the human the link.
    return {
        "conversation_id": conv_id,
        "state": "still_running",
        "url": f"{_base()}/conversations/{conv_id}",
        "note": f"did not reach a terminal state within {TIMEOUT_SECONDS}s",
    }
