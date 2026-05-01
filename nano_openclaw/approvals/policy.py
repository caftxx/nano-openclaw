"""Approval policy evaluator.

Mirrors openclaw's exec-approvals-allowlist.ts:
- Pattern matching for dangerous commands
- Risk assessment
- Allowlist evaluation
"""

import fnmatch
from dataclasses import dataclass
from typing import Dict, List

from nano_openclaw.approvals.types import ApprovalPolicy


DEFAULT_DANGEROUS_BASH_PATTERNS: List[str] = [
    "rm -rf",
    "rm -fr",
    "dd if=",
    "dd of=",
    "mkfs",
    "fdisk",
    ":(){ :|:& };:",
    "chmod -R 777",
    "chown -R",
    "> /dev/sda",
    "mv /*",
    "wget * | sh",
    "curl * | sh",
    "sudo rm",
    "sudo dd",
]

DEFAULT_DANGEROUS_WRITE_PATTERNS: List[str] = [
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    ".ssh/authorized_keys",
    "id_rsa",
    ".pem",
    ".key",
]


@dataclass
class EvaluationResult:
    """Result of policy evaluation."""
    requires_approval: bool
    risk_level: str
    reason: str
    matched_pattern: str = ""


class ApprovalPolicyEvaluator:
    """Evaluates tool invocations against approval policy."""
    
    def __init__(self, policy: ApprovalPolicy) -> None:
        self.policy = policy
        self._dangerous_patterns: Dict[str, List[str]] = {
            "bash": DEFAULT_DANGEROUS_BASH_PATTERNS,
            "write_file": DEFAULT_DANGEROUS_WRITE_PATTERNS,
        }
    
    def evaluate(self, tool_name: str, tool_args: Dict[str, object]) -> EvaluationResult:
        """Evaluate whether a tool invocation requires approval.
        
        Args:
            tool_name: Tool name (bash, write_file, etc.)
            tool_args: Tool arguments dict
        
        Returns:
            EvaluationResult with requires_approval, risk_level, reason
        """
        if self.policy.ask_mode == "off":
            return EvaluationResult(
                requires_approval=False,
                risk_level="low",
                reason="ask_mode is off"
            )
        
        if tool_name not in self.policy.dangerous_tools:
            return EvaluationResult(
                requires_approval=False,
                risk_level="low",
                reason=f"tool {tool_name} not in dangerous_tools"
            )
        
        tool_config = self.policy.tool_configs.get(tool_name)
        if tool_config and tool_config.requires_approval:
            return EvaluationResult(
                requires_approval=True,
                risk_level="medium",
                reason=f"tool {tool_name} always requires approval"
            )
        
        patterns = self._dangerous_patterns.get(tool_name, [])
        target = self._get_pattern_target(tool_name, tool_args)
        
        if target:
            for pattern in patterns:
                if self._pattern_match(pattern, target):
                    return EvaluationResult(
                        requires_approval=True,
                        risk_level="high",
                        reason=f"matched dangerous pattern: {pattern}",
                        matched_pattern=pattern
                    )
        
        if self.policy.ask_mode == "always":
            return EvaluationResult(
                requires_approval=True,
                risk_level="medium",
                reason="ask_mode is always"
            )
        
        return EvaluationResult(
            requires_approval=False,
            risk_level="low",
            reason="no dangerous patterns matched"
        )
    
    def _get_pattern_target(self, tool_name: str, tool_args: Dict[str, object]) -> str:
        """Get the string to match patterns against."""
        if tool_name == "bash":
            return str(tool_args.get("command", ""))
        if tool_name == "write_file":
            return str(tool_args.get("path", ""))
        return ""
    
    def _pattern_match(self, pattern: str, target: str) -> bool:
        """Check if pattern matches target.
        
        Uses fnmatch for glob-style patterns (*, ?) and
        contains check for literal patterns.
        """
        if "*" in pattern:
            return fnmatch.fnmatch(target.lower(), pattern.lower())
        return pattern.lower() in target.lower()
    
    def check_allow_always(self, tool_name: str, tool_args: Dict[str, object], stored_patterns: List[str]) -> bool:
        """Check if invocation matches stored allow-always patterns."""
        target = self._get_pattern_target(tool_name, tool_args)
        for pattern in stored_patterns:
            if self._pattern_match(pattern, target):
                return True
        return False