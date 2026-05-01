"""Approval module tests."""
import pytest
from nano_openclaw.approvals.types import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalPolicy,
    ToolApprovalConfig,
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


def test_tool_approval_config():
    """ToolApprovalConfig defines per-tool settings."""
    config = ToolApprovalConfig(
        tool_name="bash",
        requires_approval=True,
        dangerous_patterns=["rm -rf", "dd if="],
    )
    assert config.tool_name == "bash"
    assert "rm -rf" in config.dangerous_patterns


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
    """Evaluator respects allow-always patterns."""
    from nano_openclaw.approvals.policy import ApprovalPolicyEvaluator
    
    policy = ApprovalPolicy(allow_always_patterns=["ls *", "cat *.txt"])
    evaluator = ApprovalPolicyEvaluator(policy)
    
    result = evaluator.evaluate("bash", {"command": "ls -la"})
    assert result.requires_approval == False


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
    """Manager persists allow-always decisions."""
    from nano_openclaw.approvals import ApprovalManager
    
    store_path = tmp_path / "approvals.json"
    policy = ApprovalPolicy(allow_always_store=str(store_path))
    manager = ApprovalManager(policy)
    
    req = manager.create_request("bash", {"command": "ls -la"})
    manager.record_decision(req.request_id, ApprovalDecision.ALLOW_ALWAYS)
    
    assert store_path.exists()
    
    patterns = manager.load_allow_always_patterns()
    assert len(patterns) > 0


def test_manager_check_allow_always():
    """Manager checks stored allow-always patterns."""
    from nano_openclaw.approvals import ApprovalManager
    
    policy = ApprovalPolicy(allow_always_patterns=["ls *", "cat *.txt"])
    manager = ApprovalManager(policy)
    
    result = manager.check_request("bash", {"command": "ls -la"})
    assert result.requires_approval == False


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