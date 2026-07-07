"""
apps/orchestrator/approval_actions.py
One-tap phone approvals: HMAC-signed action URLs for ntfy notification buttons.

Flow:
    intake inserts approval → notifier attaches Approve/Reject buttons whose
    URLs carry (approval_id, action, expiry, signature) → tapping hits the n8n
    approval-actions webhook → n8n runs this module's CLI → verify + decide().

Security properties:
    - Signed: HMAC-SHA256 over "approval_id:action:expiry" with
      APPROVAL_ACTION_SECRET. Forging a URL for another approval or flipping
      approve→reject requires the secret.
    - Expiring: links die after APPROVAL_ACTION_TTL_HOURS (default 24).
    - Single-use: decide() refuses an already-decided approval, so a replayed
      link is rejected.
    - Constant-time comparison via hmac.compare_digest.

Environment variables:
    APPROVAL_ACTION_SECRET  -- required to issue/verify action URLs
    APPROVAL_ACTION_URL     -- n8n webhook base, e.g.
                               http://pi5.tailnet:5678/webhook/approval-action
    APPROVAL_ACTION_TTL_HOURS (default 24)
    DB_PATH                 (default: db/assistant.db)
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from approval_service import decide
from db import open_db

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("DB_PATH", str(REPO_ROOT / "db" / "assistant.db")))

ACTION_SECRET = os.environ.get("APPROVAL_ACTION_SECRET", "")
ACTION_URL = os.environ.get("APPROVAL_ACTION_URL", "")
TTL_HOURS = int(os.environ.get("APPROVAL_ACTION_TTL_HOURS", "24"))

VALID_ACTIONS = ("approved", "rejected")


class ActionVerificationError(Exception):
    """Signature, expiry, or action validation failed."""


def _sign(approval_id: int, action: str, expires_ts: int, secret: str) -> str:
    msg = f"{approval_id}:{action}:{expires_ts}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def make_action_url(
    approval_id: int,
    action: str,
    *,
    base_url: str = "",
    secret: str = "",
    now: datetime | None = None,
) -> str:
    """Build a signed, expiring URL for one Approve/Reject button."""
    base = base_url or ACTION_URL
    key = secret or ACTION_SECRET
    if not base or not key:
        raise EnvironmentError(
            "APPROVAL_ACTION_URL and APPROVAL_ACTION_SECRET must be set"
        )
    if action not in VALID_ACTIONS:
        raise ValueError(f"action must be one of {VALID_ACTIONS}, got {action!r}")
    current = now or datetime.now(timezone.utc)
    expires_ts = int((current + timedelta(hours=TTL_HOURS)).timestamp())
    query = urlencode({
        "id": approval_id,
        "action": action,
        "exp": expires_ts,
        "sig": _sign(approval_id, action, expires_ts, key),
    })
    return f"{base}?{query}"


def verify_and_decide(
    conn,
    *,
    approval_id: int,
    action: str,
    expires_ts: int,
    signature: str,
    secret: str = "",
    now: datetime | None = None,
) -> dict:
    """
    Verify a tapped action link and record the decision.
    Raises ActionVerificationError on any validation failure; decide() itself
    raises ValueError for unknown or already-decided approvals (single-use).
    """
    key = secret or ACTION_SECRET
    if not key:
        raise ActionVerificationError("APPROVAL_ACTION_SECRET is not configured")
    if action not in VALID_ACTIONS:
        raise ActionVerificationError(f"Invalid action {action!r}")

    current = now or datetime.now(timezone.utc)
    if current.timestamp() > expires_ts:
        raise ActionVerificationError("Action link has expired")

    expected = _sign(approval_id, action, expires_ts, key)
    if not hmac.compare_digest(expected, signature):
        raise ActionVerificationError("Bad signature")

    return decide(conn, approval_id, action, reason="one-tap via ntfy")


def action_buttons(approval_id: int) -> str:
    """
    ntfy 'Actions' header for a notification: Approve + Reject HTTP buttons.
    Returns "" when action URLs aren't configured (buttons silently omitted).
    """
    if not ACTION_URL or not ACTION_SECRET:
        return ""
    approve = make_action_url(approval_id, "approved")
    reject = make_action_url(approval_id, "rejected")
    return (
        f"http, ✅ Approve, {approve}, method=POST, clear=true; "
        f"http, ❌ Reject, {reject}, method=POST, clear=true"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="One-tap approval action handler")
    parser.add_argument("--id", type=int, required=True, dest="approval_id")
    parser.add_argument("--action", required=True, choices=VALID_ACTIONS)
    parser.add_argument("--exp", type=int, required=True, dest="expires_ts")
    parser.add_argument("--sig", required=True, dest="signature")
    args = parser.parse_args()

    conn = open_db(DB_PATH)
    try:
        result = verify_and_decide(
            conn,
            approval_id=args.approval_id,
            action=args.action,
            expires_ts=args.expires_ts,
            signature=args.signature,
        )
        print(json.dumps(result, indent=2))
    except (ActionVerificationError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
