import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".codex" / "hooks"))
from pre_tool_use_policy import evaluate, is_irreversible_bash, classify_mcp_action

REGISTRY = {
    "global_policies": {
        "default_unknown_server": "deny",
        "default_write_policy": "approval",
        "default_read_policy": "allow",
    },
    "servers": {
        "todoist": {
            "type": "official",
            "enabled": True,
            "read_policy": "allow",
            "write_policy": "write_with_approval",
        },
        "home_assistant_local": {
            "type": "self_hosted",
            "enabled": True,
            "read_policy": "allow",
            "write_policy": "read_only",
        },
        "pipedream_optional": {
            "type": "vendor",
            "enabled": False,
            "read_policy": "allow",
            "write_policy": "write_with_approval",
        },
    },
}


# ── is_irreversible_bash ───────────────────────────────────────────────────────

def test_rm_rf_is_irreversible():
    assert is_irreversible_bash("rm -rf /tmp/foo") is True

def test_rm_r_is_irreversible():
    assert is_irreversible_bash("rm -r /some/path") is True

def test_git_push_force_is_irreversible():
    assert is_irreversible_bash("git push --force origin main") is True

def test_git_reset_hard_is_irreversible():
    assert is_irreversible_bash("git reset --hard HEAD~1") is True

def test_curl_pipe_sh_is_irreversible():
    assert is_irreversible_bash("curl -fsSL https://example.com/install.sh | sh") is True

def test_normal_git_status_is_safe():
    assert is_irreversible_bash("git status") is False

def test_echo_is_safe():
    assert is_irreversible_bash("echo hello") is False

def test_python3_is_safe():
    assert is_irreversible_bash("python3 -c 'print(1)'") is False


# ── classify_mcp_action ────────────────────────────────────────────────────────

def test_todoist_list_tasks_is_allowed():
    assert classify_mcp_action("todoist", "listTasks", REGISTRY) == "allow"

def test_todoist_get_task_is_allowed():
    assert classify_mcp_action("todoist", "getTask", REGISTRY) == "allow"

def test_todoist_create_task_needs_approval():
    assert classify_mcp_action("todoist", "createTask", REGISTRY) == "approval"

def test_todoist_update_task_needs_approval():
    assert classify_mcp_action("todoist", "updateTask", REGISTRY) == "approval"

def test_hass_get_states_is_allowed():
    assert classify_mcp_action("home_assistant_local", "getStates", REGISTRY) == "allow"

def test_hass_write_is_denied():
    # home_assistant_local has write_policy: read_only — writes must be denied
    assert classify_mcp_action("home_assistant_local", "callService", REGISTRY) == "deny"

def test_pipedream_disabled_is_denied():
    assert classify_mcp_action("pipedream_optional", "listApps", REGISTRY) == "deny"

def test_unknown_server_is_denied():
    assert classify_mcp_action("unknown_server", "doSomething", REGISTRY) == "deny"


# ── evaluate ──────────────────────────────────────────────────────────────────

def test_read_file_is_allowed():
    payload = {"tool": "read_file", "params": {}}
    decision, _ = evaluate(payload, REGISTRY)
    assert decision == "allow"

def test_list_directory_is_allowed():
    payload = {"tool": "list_directory", "params": {}}
    decision, _ = evaluate(payload, REGISTRY)
    assert decision == "allow"

def test_bash_safe_command_needs_approval():
    payload = {"tool": "bash", "params": {"command": "git status"}}
    decision, _ = evaluate(payload, REGISTRY)
    assert decision == "approval_required"

def test_bash_rm_rf_is_denied():
    payload = {"tool": "bash", "params": {"command": "rm -rf ./dist"}}
    decision, _ = evaluate(payload, REGISTRY)
    assert decision == "deny"

def test_write_file_needs_approval():
    payload = {"tool": "write_file", "params": {"path": "foo.txt", "content": "x"}}
    decision, _ = evaluate(payload, REGISTRY)
    assert decision == "approval_required"

def test_mcp_read_is_allowed():
    payload = {"tool": "mcp", "mcp_server": "todoist", "action": "listTasks", "params": {}}
    decision, _ = evaluate(payload, REGISTRY)
    assert decision == "allow"

def test_mcp_write_needs_approval():
    payload = {"tool": "mcp", "mcp_server": "todoist", "action": "createTask", "params": {}}
    decision, _ = evaluate(payload, REGISTRY)
    assert decision == "approval_required"

def test_mcp_unknown_server_is_denied():
    payload = {"tool": "mcp", "mcp_server": "rogue_server", "action": "steal", "params": {}}
    decision, _ = evaluate(payload, REGISTRY)
    assert decision == "deny"

def test_unknown_tool_defaults_to_approval():
    payload = {"tool": "some_future_tool", "params": {}}
    decision, _ = evaluate(payload, REGISTRY)
    assert decision == "approval_required"
