"""Approval module tests."""
import json
import pytest
from pathlib import Path
from nano_openclaw.approvals.types import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalPolicy,
    ToolApprovalConfig,
    AllowlistEntry,
    DEFAULT_AGENT_ID,
)


def test_approval_decision_values():
    """ApprovalDecision has three values."""
    assert ApprovalDecision.ALLOW_ONCE == "allow-once"
    assert ApprovalDecision.ALLOW_ALWAYS == "allow-always"
    assert ApprovalDecision.DENY == "deny"


def test_approval_request_creation():
    """ApprovalRequest captures tool invocation."""
    req = ApprovalRequest(
        tool_name="bash",
        tool_args={"command": "rm -rf /"},
        risk_level="high",
        reason="destructive command pattern detected",
    )
    assert req.tool_name == "bash"
    assert req.tool_args == {"command": "rm -rf /"}
    assert req.risk_level == "high"


def test_approval_policy_defaults():
    """Default policy has sensible values."""
    policy = ApprovalPolicy()
    assert policy.ask_mode == "on-miss"
    assert policy.security_mode == "allowlist"
    assert len(policy.dangerous_tools) > 0
    assert policy.agent_id == DEFAULT_AGENT_ID


def test_tool_approval_config():
    """ToolApprovalConfig defines per-tool settings."""
    config = ToolApprovalConfig(
        tool_name="bash",
        requires_approval=True,
        dangerous_patterns=["rm -rf", "dd if="],
    )
    assert config.tool_name == "bash"
    assert "rm -rf" in config.dangerous_patterns


def test_allowlist_entry():
    """AllowlistEntry has rich metadata."""
    entry = AllowlistEntry(
        id="abc123",
        pattern="/usr/bin/ls",
        source="allow-always",
        commandText="ls -la",
        lastUsedAt=1730000000.0,
    )
    assert entry.id == "abc123"
    assert entry.pattern == "/usr/bin/ls"
    assert entry.source == "allow-always"
    assert entry.commandText == "ls -la"
    assert entry.lastUsedAt == 1730000000.0


def test_default_agent_id():
    """DEFAULT_AGENT_ID is 'default'."""
    assert DEFAULT_AGENT_ID == "default"


def test_default_dangerous_patterns():
    """Default dangerous bash patterns include rm -rf."""
    from nano_openclaw.approvals.policy import DEFAULT_DANGEROUS_BASH_PATTERNS
    
    assert "rm -rf" in DEFAULT_DANGEROUS_BASH_PATTERNS
    assert "dd if=" in DEFAULT_DANGEROUS_BASH_PATTERNS


def test_policy_evaluator_match_danger():
    """Evaluator detects dangerous bash commands."""
    from nano_openclaw.approvals.policy import ApprovalPolicyEvaluator
    
    policy = ApprovalPolicy()
    evaluator = ApprovalPolicyEvaluator(policy)
    
    result = evaluator.evaluate("bash", {"command": "rm -rf /home/user"})
    assert result.requires_approval == True
    assert result.risk_level == "high"
    assert "destructive" in result.reason.lower() or "dangerous" in result.reason.lower() or "pattern" in result.reason.lower()


def test_policy_evaluator_safe_command():
    """Evaluator allows safe commands."""
    from nano_openclaw.approvals.policy import ApprovalPolicyEvaluator
    
    policy = ApprovalPolicy()
    evaluator = ApprovalPolicyEvaluator(policy)
    
    result = evaluator.evaluate("bash", {"command": "ls -la"})
    assert result.requires_approval == False


def test_policy_evaluator_write_file():
    """Evaluator triggers approval for write_file by default."""
    from nano_openclaw.approvals.policy import ApprovalPolicyEvaluator
    
    policy = ApprovalPolicy(dangerous_tools=["write_file"])
    evaluator = ApprovalPolicyEvaluator(policy)
    
    result = evaluator.evaluate("write_file", {"path": "/etc/passwd", "content": "test"})
    assert result.requires_approval == True
    assert result.risk_level == "high"


def test_policy_evaluator_allowlist():
    """Evaluator respects allowlist patterns."""
    from nano_openclaw.approvals.policy import ApprovalPolicyEvaluator
    
    policy = ApprovalPolicy(allowlist=[
        AllowlistEntry(pattern="ls", source="allow-always"),
        AllowlistEntry(pattern="cat", source="allow-always"),
    ])
    evaluator = ApprovalPolicyEvaluator(policy)
    
    # Should not require approval for matching pattern
    result = evaluator.check_allow_always("bash", {"command": "ls -la"}, ["ls", "cat"])
    assert result == True


def test_manager_create_request():
    """Manager creates approval requests."""
    from nano_openclaw.approvals import ApprovalManager
    
    policy = ApprovalPolicy()
    manager = ApprovalManager(policy)
    
    req = manager.create_request("bash", {"command": "rm -rf /"})
    assert req.tool_name == "bash"
    assert req.risk_level == "high"
    assert req.request_id != ""


def test_manager_record_decision():
    """Manager records decisions."""
    from nano_openclaw.approvals import ApprovalManager
    
    policy = ApprovalPolicy()
    manager = ApprovalManager(policy)
    
    req = manager.create_request("bash", {"command": "rm -rf /"})
    manager.record_decision(req.request_id, ApprovalDecision.ALLOW_ONCE)
    
    assert manager.get_decision(req.request_id) == ApprovalDecision.ALLOW_ONCE


def test_manager_allow_always_persistence(tmp_path):
    """Manager persists allow-always decisions with rich metadata."""
    from nano_openclaw.approvals import ApprovalManager
    
    store_path = tmp_path / "approvals.json"
    policy = ApprovalPolicy(allow_always_store=str(store_path), agent_id="test-agent")
    manager = ApprovalManager(policy)
    
    req = manager.create_request("bash", {"command": "ls -la"})
    manager.record_decision(req.request_id, ApprovalDecision.ALLOW_ALWAYS)
    
    assert store_path.exists()
    
    data = json.loads(store_path.read_text())
    assert data["version"] == 1
    assert "test-agent" in data["agents"]
    
    allowlist = data["agents"]["test-agent"]["allowlist"]
    assert len(allowlist) > 0
    
    entry = allowlist[0]
    assert entry["pattern"] == "ls"
    assert entry["source"] == "allow-always"
    assert entry["commandText"] == "ls -la"
    assert "id" in entry
    assert "lastUsedAt" in entry


def test_manager_per_agent_storage(tmp_path):
    """Manager stores allowlist per agent."""
    from nano_openclaw.approvals import ApprovalManager
    
    store_path = tmp_path / "approvals.json"
    
    # Agent 1
    policy1 = ApprovalPolicy(allow_always_store=str(store_path), agent_id="agent-1")
    manager1 = ApprovalManager(policy1)
    req1 = manager1.create_request("bash", {"command": "ls"})
    manager1.record_decision(req1.request_id, ApprovalDecision.ALLOW_ALWAYS)
    
    # Agent 2
    policy2 = ApprovalPolicy(allow_always_store=str(store_path), agent_id="agent-2")
    manager2 = ApprovalManager(policy2)
    req2 = manager2.create_request("bash", {"command": "cat"})
    manager2.record_decision(req2.request_id, ApprovalDecision.ALLOW_ALWAYS)
    
    # Check file
    data = json.loads(store_path.read_text())
    assert "agent-1" in data["agents"]
    assert "agent-2" in data["agents"]
    
    # Each agent should have different patterns
    agent1_patterns = [e["pattern"] for e in data["agents"]["agent-1"]["allowlist"]]
    agent2_patterns = [e["pattern"] for e in data["agents"]["agent-2"]["allowlist"]]
    
    assert "ls" in agent1_patterns
    assert "cat" in agent2_patterns
    assert "ls" not in agent2_patterns
    assert "cat" not in agent1_patterns


def test_manager_load_allowlist(tmp_path):
    """Manager loads existing allowlist from file."""
    from nano_openclaw.approvals import ApprovalManager
    
    store_path = tmp_path / "approvals.json"
    
    # Pre-populate file
    existing_data = {
        "version": 1,
        "agents": {
            "default": {
                "allowlist": [
                    {
                        "id": "existing-id",
                        "pattern": "echo",
                        "source": "allow-always",
                        "commandText": "echo hello",
                        "lastUsedAt": 1730000000.0,
                    }
                ]
            }
        }
    }
    store_path.write_text(json.dumps(existing_data))
    
    policy = ApprovalPolicy(allow_always_store=str(store_path))
    manager = ApprovalManager(policy)
    
    allowlist = manager.load_allowlist()
    assert len(allowlist) == 1
    assert allowlist[0].pattern == "echo"
    assert allowlist[0].source == "allow-always"


def test_manager_check_allow_always():
    """Manager checks stored allowlist patterns."""
    from nano_openclaw.approvals import ApprovalManager

    policy = ApprovalPolicy(allowlist=[
        AllowlistEntry(pattern="ls", source="allow-always"),
        AllowlistEntry(pattern="cat", source="allow-always"),
    ])
    manager = ApprovalManager(policy)

    result = manager.check_request("bash", {"command": "ls -la"})
    assert result.requires_approval == False


def test_on_miss_requires_approval_for_safe_command():
    """Mirrors requiresExecApproval(): on-miss + allowlist + miss -> require approval.

    Bug scenario: ask_mode=on-miss, empty allowlist, safe command like 'ls -la'
    should require approval (not just dangerous patterns).
    """
    from nano_openclaw.approvals import ApprovalManager

    policy = ApprovalPolicy(
        ask_mode="on-miss",
        security_mode="allowlist",
        dangerous_tools=["bash"],
        allowlist=[],
    )
    manager = ApprovalManager(policy)

    result = manager.check_request("bash", {"command": "ls -la"})
    assert result.requires_approval is True


def test_on_miss_no_approval_when_allowlist_hit():
    """on-miss: allowlist hit -> no approval needed."""
    from nano_openclaw.approvals import ApprovalManager

    policy = ApprovalPolicy(
        ask_mode="on-miss",
        security_mode="allowlist",
        dangerous_tools=["bash"],
        allowlist=[AllowlistEntry(pattern="ls", source="allow-always")],
    )
    manager = ApprovalManager(policy)

    result = manager.check_request("bash", {"command": "ls -la"})
    assert result.requires_approval is False


def test_ask_off_never_requires_approval():
    """ask_mode=off -> never require approval, even for dangerous commands."""
    from nano_openclaw.approvals import ApprovalManager

    policy = ApprovalPolicy(ask_mode="off", dangerous_tools=["bash"], allowlist=[])
    manager = ApprovalManager(policy)

    result = manager.check_request("bash", {"command": "rm -rf /"})
    assert result.requires_approval is False


def test_ui_render_approval_request():
    """UI renders approval request panel."""
    from nano_openclaw.approvals.ui import ApprovalUI
    from rich.console import Console
    console = Console()
    ui = ApprovalUI(console)
    
    req = ApprovalRequest(
        request_id="abc123",
        tool_name="bash",
        tool_args={"command": "rm -rf /"},
        risk_level="high",
        reason="destructive command",
    )
    
    ui.render_request(req)


def test_ui_format_args():
    """UI formats args correctly."""
    from nano_openclaw.approvals.ui import ApprovalUI
    from rich.console import Console
    console = Console()
    ui = ApprovalUI(console)
    
    short_args = {"path": "/tmp/test.txt"}
    assert ui._format_args(short_args) == '{"path": "/tmp/test.txt"}'
    
    long_args = {"content": "x" * 100}
    formatted = ui._format_args(long_args)
    assert len(formatted) <= 63


def test_ui_render_denied():
    """UI renders denial message."""
    from nano_openclaw.approvals.ui import ApprovalUI
    from rich.console import Console
    console = Console()
    ui = ApprovalUI(console)
    
    req = ApprovalRequest(
        request_id="abc123",
        tool_name="bash",
        tool_args={"command": "rm -rf /"},
        risk_level="high",
        reason="destructive command",
    )
    
    ui.render_denied(req)


def test_ui_render_allowed():
    """UI renders approval message."""
    from nano_openclaw.approvals.ui import ApprovalUI
    from rich.console import Console
    console = Console()
    ui = ApprovalUI(console)
    
    req = ApprovalRequest(
        request_id="abc123",
        tool_name="bash",
        tool_args={"command": "ls -la"},
        risk_level="medium",
        reason="ask_mode is always",
    )
    
    ui.render_allowed(req, ApprovalDecision.ALLOW_ONCE)
    ui.render_allowed(req, ApprovalDecision.ALLOW_ALWAYS)