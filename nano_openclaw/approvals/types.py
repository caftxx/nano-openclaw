"""Approval types for nano-openclaw.

Mirrors openclaw's exec-approvals.ts:
- ApprovalDecision (allow-once/allow-always/deny)
- ApprovalRequest (tool invocation capture)
- ApprovalPolicy (security/ask modes)
"""

from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ApprovalDecision(str, Enum):
    """User decision on approval request."""
    ALLOW_ONCE = "allow-once"
    ALLOW_ALWAYS = "allow-always"
    DENY = "deny"


class ApprovalRequest(BaseModel):
    """Approval request for a tool invocation."""
    model_config = ConfigDict(populate_by_name=True)
    
    request_id: str = Field(default="", description="Unique request ID")
    tool_name: str = Field(description="Tool name (e.g., bash, write_file)")
    tool_args: Dict[str, object] = Field(description="Tool arguments")
    risk_level: Literal["low", "medium", "high"] = Field(
        default="medium",
        description="Risk assessment level"
    )
    reason: str = Field(default="", description="Why approval is needed")
    timestamp: float = Field(default=0.0, description="Request timestamp")


class ToolApprovalConfig(BaseModel):
    """Per-tool approval configuration."""
    model_config = ConfigDict(populate_by_name=True)
    
    tool_name: str = Field(description="Tool name")
    requires_approval: bool = Field(default=False, description="Always requires approval")
    dangerous_patterns: List[str] = Field(
        default_factory=list,
        description="Patterns that trigger approval"
    )
    safe_patterns: List[str] = Field(
        default_factory=list,
        description="Patterns that bypass approval"
    )


class ApprovalPolicy(BaseModel):
    """Global approval policy configuration."""
    model_config = ConfigDict(populate_by_name=True)
    
    ask_mode: Literal["off", "on-miss", "always"] = Field(
        default="on-miss",
        description="off=never ask, on-miss=ask if not in allowlist, always=always ask"
    )
    security_mode: Literal["deny", "allowlist", "full"] = Field(
        default="allowlist",
        description="deny=block dangerous, allowlist=check whitelist, full=allow all"
    )
    dangerous_tools: List[str] = Field(
        default_factory=lambda: ["bash", "write_file"],
        description="Tools that may require approval"
    )
    tool_configs: Dict[str, ToolApprovalConfig] = Field(
        default_factory=dict,
        description="Per-tool approval settings"
    )
    allow_always_store: Optional[str] = Field(
        default=None,
        description="Path to store allow-always decisions"
    )
    allow_always_patterns: List[str] = Field(
        default_factory=list,
        description="Patterns that have been approved with allow-always"
    )