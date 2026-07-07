#!/usr/bin/env python3
"""
Pre-tool-use policy hook for Codex.

Protocol: Codex writes a JSON blob to stdin describing the tool call.
Exits 0 to allow, 1 to deny (reason on stderr), or writes approval_required to stdout to pause.

Stdin schema:
{
  "tool": "str",             # e.g. "bash", "write_file"
  "mcp_server": "str|null",  # MCP server name if applicable
  "action": "str",           # action/method name
  "params": {}               # call parameters
}
"""
import json
import re
import sys
from pathlib import Path

READ_ONLY_TOOLS = frozenset({
    "read_file", "list_directory", "grep", "glob",
})

IRREVERSIBLE_PATTERNS = [
    re.compile(r"\brm\s+-[rf]"),
    re.compile(r"\bdd\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bgit\s+push\s+--force"),
    re.compile(r"\bgit\s+reset\s+--hard"),
    re.compile(r"\bchmod\s+777\b"),
    re.compile(r"\bcurl\s+.*\|\s*(sh|bash|python)"),
]

WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "run_python", "bash",
    "create_file", "delete_file", "move_file",
})


def load_mcp_registry() -> dict:
    try:
        repo_root = Path(__file__).resolve().parents[2]
        registry_path = repo_root / "mcp" / "registry.yaml"
        import yaml
        with registry_path.open() as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


def classify_mcp_action(server_name: str, action: str, registry: dict) -> str:
    """Return 'allow', 'approval', or 'deny'."""
    global_policies = registry.get("global_policies", {})
    servers = registry.get("servers", {})

    server = servers.get(server_name)
    if server is None:
        return global_policies.get("default_unknown_server", "deny")

    if not server.get("enabled", True):
        return "deny"

    # An action is read-only when it STARTS with a read verb AND contains no
    # write-indicating token anywhere — a bare prefix check would let names
    # like get_or_create_project or fetch_and_apply bypass the approval gate.
    has_read_prefix = bool(
        re.match(r"^(get|list|search|read|fetch|describe)($|_|[A-Z0-9])", action)
        or re.match(r"^(get|list|search|read|fetch|describe)($|_)", action, re.I)
    )
    has_write_token = bool(re.search(
        r"(create|update|delete|write|apply|set|send|remove|add|move|rename|execute|run|post|put|patch)",
        action, re.I,
    ))
    is_read = has_read_prefix and not has_write_token
    if is_read:
        policy = server.get("read_policy", global_policies.get("default_read_policy", "allow"))
    else:
        policy = server.get("write_policy", global_policies.get("default_write_policy", "approval"))

    if policy in ("read_only", "allow"):
        return "allow" if is_read else "deny"
    if policy == "write_with_approval":
        return "approval"
    return "deny"


def is_irreversible_bash(command: str) -> bool:
    return any(p.search(command) for p in IRREVERSIBLE_PATTERNS)


def evaluate(payload: dict, registry: dict) -> tuple[str, str]:
    tool = payload.get("tool", "")
    mcp_server = payload.get("mcp_server")
    action = payload.get("action", "")
    params = payload.get("params", {})

    if mcp_server:
        decision = classify_mcp_action(mcp_server, action, registry)
        if decision == "allow":
            return "allow", f"MCP read on {mcp_server} auto-approved"
        if decision == "approval":
            return "approval_required", f"MCP write on {mcp_server}/{action} requires approval"
        return "deny", f"MCP server '{mcp_server}' action '{action}' denied by policy"

    if tool in READ_ONLY_TOOLS:
        return "allow", "Read-only tool auto-approved"

    if tool == "bash":
        cmd = params.get("command", "")
        if is_irreversible_bash(cmd):
            return "deny", "Irreversible shell pattern detected in command"
        return "approval_required", "Shell command requires approval"

    if tool in WRITE_TOOLS:
        return "approval_required", f"Write tool '{tool}' requires approval"

    default = registry.get("global_policies", {}).get("default_write_policy", "approval")
    if default == "approval":
        return "approval_required", f"Unknown tool '{tool}' subject to default approval policy"
    return "deny", f"Unknown tool '{tool}' denied by default policy"


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"decision": "deny", "reason": f"Malformed hook payload: {e}"}))
        sys.exit(1)

    registry = load_mcp_registry()
    decision, reason = evaluate(payload, registry)

    print(json.dumps({"decision": decision, "reason": reason}))

    if decision == "deny":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
