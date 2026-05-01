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