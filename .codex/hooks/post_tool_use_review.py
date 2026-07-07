#!/usr/bin/env python3
"""
Post-tool-use review hook for Codex.
Appends a JSONL record to logs/tool_use.jsonl for every completed tool call.
Always exits 0 — never blocks execution.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


def _summarize_params(params: dict) -> dict:
    summary = {}
    for k, v in params.items():
        if isinstance(v, str) and len(v) > 200:
            summary[k] = v[:200] + "…[truncated]"
        else:
            summary[k] = v
    return summary


def _summarize_result(result) -> "str | None":
    if result is None:
        return None
    text = json.dumps(result) if not isinstance(result, str) else result
    return text[:500] + "…[truncated]" if len(text) > 500 else text


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": payload.get("tool"),
        "mcp_server": payload.get("mcp_server"),
        "action": payload.get("action"),
        "status": payload.get("status"),
        "error": payload.get("error"),
        "params_summary": _summarize_params(payload.get("params", {})),
        "result_summary": _summarize_result(payload.get("result")),
    }

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "tool_use.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
