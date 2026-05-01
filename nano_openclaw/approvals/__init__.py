"""Approval module for nano-openclaw."""

from nano_openclaw.approvals.types import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalPolicy,
    ToolApprovalConfig,
)
from nano_openclaw.approvals.manager import ApprovalManager

__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalPolicy",
    "ToolApprovalConfig",
    "ApprovalManager",
]