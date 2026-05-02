"""Approval request manager.

Mirrors openclaw's exec-approval-manager.ts and exec-approvals.ts:
- Create/register/resolve approval requests
- Track pending requests
- Persist allow-always decisions with rich metadata
- Per-agent allowlist storage
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
    AllowlistEntry,
    DEFAULT_AGENT_ID,
)
from nano_openclaw.approvals.policy import ApprovalPolicyEvaluator, EvaluationResult


APPROVALS_FILE_VERSION = 1


class ApprovalManager:
    """Manages approval requests and decisions."""
    
    def __init__(self, policy: ApprovalPolicy) -> None:
        self.policy = policy
        self.evaluator = ApprovalPolicyEvaluator(policy)
        self._pending_requests: Dict[str, ApprovalRequest] = {}
        self._decisions: Dict[str, ApprovalDecision] = {}
        self._allowlist: List[AllowlistEntry] = list(policy.allowlist)
        self._file_cache: Optional[Dict] = None
    
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

        Mirrors openclaw's requiresExecApproval():
          ask=off           → never
          ask=always        → always
          ask=on-miss + security=allowlist + allowlist miss → require approval
        """
        if self.policy.ask_mode == "off":
            return EvaluationResult(
                requires_approval=False,
                risk_level="low",
                reason="ask_mode is off",
            )

        if tool_name not in self.policy.dangerous_tools:
            return EvaluationResult(
                requires_approval=False,
                risk_level="low",
                reason=f"tool {tool_name} not in dangerous_tools",
            )

        patterns = [e.pattern for e in self._allowlist]
        allowlist_satisfied = self.evaluator.check_allow_always(tool_name, tool_args, patterns)

        if allowlist_satisfied:
            return EvaluationResult(
                requires_approval=False,
                risk_level="low",
                reason="matches allowlist entry",
            )

        # Mirror requiresExecApproval(): ask=always → always require
        if self.policy.ask_mode == "always":
            return EvaluationResult(
                requires_approval=True,
                risk_level="medium",
                reason="ask_mode is always",
            )

        # Mirror requiresExecApproval(): ask=on-miss + security=allowlist + not satisfied → require
        if self.policy.ask_mode == "on-miss" and self.policy.security_mode == "allowlist":
            eval_result = self.evaluator.evaluate(tool_name, tool_args)
            return EvaluationResult(
                requires_approval=True,
                risk_level=eval_result.risk_level,
                reason=eval_result.reason,
                matched_pattern=eval_result.matched_pattern,
            )

        return EvaluationResult(
            requires_approval=False,
            risk_level="low",
            reason="no approval needed",
        )
    
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
                self._add_allowlist_entry(request)
    
    def get_decision(self, request_id: str) -> Optional[ApprovalDecision]:
        """Get the recorded decision for a request."""
        return self._decisions.get(request_id)
    
    def _add_allowlist_entry(self, request: ApprovalRequest) -> None:
        """Add an allowlist entry from an approved request.
        
        Mirrors openclaw's addAllowlistEntry():
        - Generate UUID
        - Extract pattern from request
        - Add source="allow-always"
        - Add commandText, lastUsedAt metadata
        - Persist to file
        """
        pattern = self._extract_pattern(request)
        if not pattern:
            return
        
        now = time.time()
        entry = AllowlistEntry(
            id=uuid.uuid4().hex,
            pattern=pattern,
            source="allow-always",
            commandText=self._get_command_text(request),
            lastUsedAt=now,
        )
        
        self._allowlist.append(entry)
        self._persist_allowlist_entry(entry)
    
    def _extract_pattern(self, request: ApprovalRequest) -> str:
        """Extract a pattern from the request for allowlist matching.
        
        For bash: parse executable path from command
        For write_file: use the path directly
        """
        tool_name = request.tool_name
        tool_args = request.tool_args
        
        if tool_name == "bash":
            command = str(tool_args.get("command", ""))
            return self._command_to_pattern(command)
        
        if tool_name == "write_file":
            path = str(tool_args.get("path", ""))
            return path
        
        return ""
    
    def _get_command_text(self, request: ApprovalRequest) -> str:
        """Get the original command text for metadata."""
        if request.tool_name == "bash":
            return str(request.tool_args.get("command", ""))
        if request.tool_name == "write_file":
            return str(request.tool_args.get("path", ""))
        return ""
    
    def _command_to_pattern(self, command: str) -> str:
        """Convert a command to a pattern for allowlist matching.
        
        Mirrors openclaw's resolveAllowAlwaysPatternEntries():
        - Parse the command to find the executable
        - Return the executable path as pattern (if absolute)
        - Otherwise return base command
        """
        command = command.strip()
        if not command:
            return ""
        
        parts = command.split()
        if not parts:
            return ""
        
        base_cmd = parts[0]
        
        if base_cmd.startswith("/") or base_cmd.startswith("~"):
            return base_cmd
        
        return base_cmd
    
    def load_allowlist(self) -> List[AllowlistEntry]:
        """Load allowlist from store file for current agent.
        
        File structure (mirrors openclaw):
        {
          "version": 1,
          "agents": {
            "default": {
              "allowlist": [
                { "id": "...", "pattern": "...", "source": "allow-always", ... }
              ]
            }
          }
        }
        """
        if not self.policy.allow_always_store:
            return []
        
        store_path = Path(self.policy.allow_always_store)
        if not store_path.exists():
            return []
        
        try:
            data = json.loads(store_path.read_text())
            if data.get("version") != APPROVALS_FILE_VERSION:
                return []
            
            agents = data.get("agents", {})
            agent_data = agents.get(self.policy.agent_id, {})
            allowlist_raw = agent_data.get("allowlist", [])
            
            return [AllowlistEntry(**entry) for entry in allowlist_raw]
        except (json.JSONDecodeError, IOError, TypeError):
            return []
    
    def _persist_allowlist_entry(self, entry: AllowlistEntry) -> None:
        """Persist a single allowlist entry to the store file.
        
        Mirrors openclaw's saveExecApprovals():
        - Load existing file
        - Add/update entry for agent
        - Save file
        """
        if not self.policy.allow_always_store:
            return
        
        store_path = Path(self.policy.allow_always_store)
        store_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = self._load_or_create_file(store_path)
        
        agents = data.get("agents", {})
        agent_data = agents.get(self.policy.agent_id, {"allowlist": []})
        allowlist = agent_data.get("allowlist", [])
        
        entry_dict = entry.model_dump()
        
        existing_idx = None
        for i, existing in enumerate(allowlist):
            if existing.get("pattern") == entry.pattern:
                existing_idx = i
                break
        
        if existing_idx is not None:
            allowlist[existing_idx] = entry_dict
        else:
            allowlist.append(entry_dict)
        
        agent_data["allowlist"] = allowlist
        agents[self.policy.agent_id] = agent_data
        data["agents"] = agents
        
        store_path.write_text(json.dumps(data, indent=2))
    
    def _load_or_create_file(self, store_path: Path) -> Dict:
        """Load existing file or create new exec-approvals.json skeleton."""
        if store_path.exists():
            try:
                data = json.loads(store_path.read_text(encoding="utf-8"))
                if data.get("version") == APPROVALS_FILE_VERSION:
                    return data
            except (json.JSONDecodeError, IOError):
                pass
        return {"version": APPROVALS_FILE_VERSION}
    
    def clear_pending(self, request_id: str) -> None:
        """Clear a pending request after it's resolved."""
        self._pending_requests.pop(request_id, None)
    
    def get_allowlist_patterns(self) -> List[str]:
        """Get all allowlist patterns for matching."""
        return [e.pattern for e in self._allowlist]