"""
apps/orchestrator/notifier.py
Best-effort push notifications via a self-hosted ntfy server.

Every call is fire-and-forget: a notification failure must never break the
pipeline, so all errors are swallowed (logged to stderr) and notify() returns
False instead of raising.

Environment variables:
    NTFY_URL    -- base URL of the ntfy server, e.g. http://localhost:8090
                   (empty = notifications disabled, notify() is a no-op)
    NTFY_TOPIC  -- topic to publish to (default: assistant)
"""
import os
import sys

import requests

NTFY_URL = os.environ.get("NTFY_URL", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "assistant")


def notify(
    message: str,
    *,
    title: str = "",
    priority: str = "default",
    tags: str = "",
    url: str = "",
    topic: str = "",
    actions: str = "",
) -> bool:
    """
    Publish a notification. Returns True on success, False otherwise.
    No-op (returns False) when NTFY_URL is not configured.
    `actions` is a raw ntfy Actions header (buttons on the notification).
    """
    base = url or NTFY_URL
    if not base:
        return False

    headers = {"Priority": priority}
    if title:
        headers["Title"] = title
    if tags:
        headers["Tags"] = tags
    if actions:
        headers["Actions"] = actions

    try:
        resp = requests.post(
            f"{base.rstrip('/')}/{topic or NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=5,
        )
        return resp.ok
    except requests.RequestException as exc:
        print(f"notifier: failed to send notification: {exc}", file=sys.stderr)
        return False


def notify_approval_needed(task: dict, approval_id: int | None = None) -> bool:
    """
    Notify that a new job is waiting for human approval. When one-tap actions
    are configured (APPROVAL_ACTION_URL/SECRET), the push carries signed
    Approve/Reject buttons; otherwise it's a plain notification.
    """
    actions = ""
    if approval_id is not None:
        try:
            from approval_actions import action_buttons
            actions = action_buttons(approval_id)
        except Exception as exc:  # buttons are optional sugar, never fatal
            print(f"notifier: could not build action buttons: {exc}", file=sys.stderr)

    ref = f" (approval #{approval_id})" if approval_id is not None else ""
    return notify(
        f"{task.get('title', '(no title)')} — intent {task.get('intent', '?')}{ref}\n"
        f"Review with: make approve",
        title="Approval needed",
        priority="high",
        tags="hand",
        actions=actions,
    )
