"""Approval request manager.

Mirrors openclaw's exec-approval-manager.ts:
- Create/register/resolve approval requests
- Track pending requests
- Persist allow-always decisions
"""

import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from nano_openclaw.approvals.types import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalRequest,
)
from nano_openclaw.approvals.policy import ApprovalPolicyEvaluator, EvaluationResult


class ApprovalManager:
    """Manages approval requests and decisions."""
    
    def __init__(self, policy: ApprovalPolicy) -> None:
        self.policy = policy
        self.evaluator = ApprovalPolicyEvaluator(policy)
        self._pending_requests: Dict[str, ApprovalRequest] = {}
        self._decisions: Dict[str, ApprovalDecision] = {}
        self._allow_always_patterns: List[str] = list(policy.allow_always_patterns)
    
    def create_request(
        self,
        tool_name: str,
        tool_args: Dict[str, object],
    ) -> ApprovalRequest:
        """Create an approval request for a tool invocation."""
        request_id = uuid.uuid4().hex[:8]
        eval_result = self.evaluator.evaluate(tool_name, tool_args)
        
        request = ApprovalRequest(
            request_id=request_id,
            tool_name=tool_name,
            tool_args=tool_args,
            risk_level=eval_result.risk_level,
            reason=eval_result.reason,
            timestamp=time.time(),
        )
        
        self._pending_requests[request_id] = request
        return request
    
    def check_request(
        self,
        tool_name: str,
        tool_args: Dict[str, object],
    ) -> EvaluationResult:
        """Check if a tool invocation requires approval.
        
        Returns EvaluationResult with requires_approval flag.
        Also checks against stored allow-always patterns.
        """
        if self.evaluator.check_allow_always(
            tool_name, tool_args, self._allow_always_patterns
        ):
            return EvaluationResult(
                requires_approval=False,
                risk_level="low",
                reason="matches allow-always pattern"
            )
        
        return self.evaluator.evaluate(tool_name, tool_args)
    
    def get_pending_request(self, request_id: str) -> Optional[ApprovalRequest]:
        """Get a pending approval request."""
        return self._pending_requests.get(request_id)
    
    def record_decision(
        self,
        request_id: str,
        decision: ApprovalDecision,
    ) -> None:
        """Record a decision for an approval request."""
        self._decisions[request_id] = decision
        
        if decision == ApprovalDecision.ALLOW_ALWAYS:
            request = self._pending_requests.get(request_id)
            if request:
                pattern = self._extract_pattern(request)
                if pattern and pattern not in self._allow_always_patterns:
                    self._allow_always_patterns.append(pattern)
                    self._save_allow_always_patterns()
    
    def get_decision(self, request_id: str) -> Optional[ApprovalDecision]:
        """Get the recorded decision for a request."""
        return self._decisions.get(request_id)
    
    def _extract_pattern(self, request: ApprovalRequest) -> str:
        """Extract a pattern from the request for allow-always matching."""
        tool_name = request.tool_name
        tool_args = request.tool_args
        
        if tool_name == "bash":
            command = str(tool_args.get("command", ""))
            return self._command_to_pattern(command)
        
        if tool_name == "write_file":
            path = str(tool_args.get("path", ""))
            return path
        
        return ""
    
    def _command_to_pattern(self, command: str) -> str:
        """Convert a command to a glob-like pattern.
        
        e.g., "ls -la /home" -> "ls *"
        """
        parts = command.split()
        if not parts:
            return ""
        
        base_cmd = parts[0]
        return f"{base_cmd} *"
    
    def load_allow_always_patterns(self) -> List[str]:
        """Load allow-always patterns from store file."""
        if not self.policy.allow_always_store:
            return []
        
        store_path = Path(self.policy.allow_always_store)
        if not store_path.exists():
            return []
        
        try:
            data = json.loads(store_path.read_text())
            return data.get("allow_always_patterns", [])
        except (json.JSONDecodeError, IOError):
            return []
    
    def _save_allow_always_patterns(self) -> None:
        """Save allow-always patterns to store file."""
        if not self.policy.allow_always_store:
            return
        
        store_path = Path(self.policy.allow_always_store)
        store_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {"allow_always_patterns": self._allow_always_patterns}
        store_path.write_text(json.dumps(data, indent=2))
    
    def clear_pending(self, request_id: str) -> None:
        """Clear a pending request after it's resolved."""
        self._pending_requests.pop(request_id, None)