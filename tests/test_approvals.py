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