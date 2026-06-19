"""Subagent registry for tracking runs.

In-memory registry that tracks all spawned subagent runs,
their status, and lifecycle events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from nano_openclaw.features.subagents.types import (
    SubagentRunRecord,
    SubagentStatus,
    new_subagent_run_id,
)


@dataclass
class SubagentRegistry:
    """Registry tracking all subagent runs."""
    
    _runs: dict[str, SubagentRunRecord] = field(default_factory=dict)
    _on_status_change: Optional[Callable[[SubagentRunRecord], None]] = None
    
    def register(
        self,
        requester_session_key: str,
        task: str,
        label: Optional[str] = None,
        model: Optional[str] = None,
        cleanup: str = "keep",
    ) -> SubagentRunRecord:
        """Register a new subagent run."""
        run_id = new_subagent_run_id()
        from nano_openclaw.features.subagents.types import SubagentCleanupMode, build_subagent_session_key, new_subagent_session_id
        
        cleanup_mode = SubagentCleanupMode(cleanup)
        session_id = new_subagent_session_id()
        
        parsed = parse_session_key_safe(requester_session_key)
        child_session_key = build_subagent_session_key(parsed["agentId"], session_id)
        
        record = SubagentRunRecord(
            run_id=run_id,
            child_session_key=child_session_key,
            requester_session_key=requester_session_key,
            task=task,
            label=label,
            model=model,
            status=SubagentStatus.PENDING,
            cleanup=cleanup_mode,
        )
        
        self._runs[run_id] = record
        return record
    
    def get(self, run_id: str) -> Optional[SubagentRunRecord]:
        """Get a run by ID."""
        return self._runs.get(run_id)
    
    def get_by_session_key(self, session_key: str) -> Optional[SubagentRunRecord]:
        """Get a run by child session key."""
        for run in self._runs.values():
            if run.child_session_key == session_key:
                return run
        return None
    
    def list_for_requester(self, requester_session_key: str) -> list[SubagentRunRecord]:
        """List all runs for a requester session."""
        return [
            run for run in self._runs.values()
            if run.requester_session_key == requester_session_key
        ]
    
    def list_active(self) -> list[SubagentRunRecord]:
        """List all active (non-terminal) runs."""
        return [
            run for run in self._runs.values()
            if not run.is_terminal
        ]
    
    def list_all(self) -> list[SubagentRunRecord]:
        """List all runs."""
        return list(self._runs.values())
    
    def count_active(self) -> int:
        """Count active runs."""
        return len(self.list_active())
    
    def count_active_for_requester(self, requester_session_key: str) -> int:
        """Count active runs for a requester session."""
        return len([
            run for run in self._runs.values()
            if run.requester_session_key == requester_session_key and not run.is_terminal
        ])
    
    def mark_started(self, run_id: str) -> None:
        """Mark a run as started."""
        run = self._runs.get(run_id)
        if run:
            run.status = SubagentStatus.RUNNING
            run.started_at = datetime.now()
            self._notify_status_change(run)
    
    def mark_completed(
        self,
        run_id: str,
        result_text: Optional[str] = None,
        elapsed_ms: Optional[int] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> None:
        """Mark a run as completed successfully."""
        run = self._runs.get(run_id)
        if run:
            run.status = SubagentStatus.COMPLETED
            run.ended_at = datetime.now()
            run.outcome = "ok"
            run.result_text = result_text
            run.elapsed_ms = elapsed_ms
            run.input_tokens = input_tokens
            run.output_tokens = output_tokens
            self._notify_status_change(run)
    
    def mark_error(
        self,
        run_id: str,
        error_message: str,
        elapsed_ms: Optional[int] = None,
    ) -> None:
        """Mark a run as failed with error."""
        run = self._runs.get(run_id)
        if run:
            run.status = SubagentStatus.ERROR
            run.ended_at = datetime.now()
            run.outcome = "error"
            run.error_message = error_message
            run.elapsed_ms = elapsed_ms
            self._notify_status_change(run)
    
    def mark_timeout(self, run_id: str) -> None:
        """Mark a run as timed out."""
        run = self._runs.get(run_id)
        if run:
            run.status = SubagentStatus.TIMEOUT
            run.ended_at = datetime.now()
            run.outcome = "timeout"
            self._notify_status_change(run)
    
    def mark_killed(self, run_id: str) -> None:
        """Mark a run as killed by user."""
        run = self._runs.get(run_id)
        if run:
            run.status = SubagentStatus.KILLED
            run.ended_at = datetime.now()
            run.outcome = "killed"
            self._notify_status_change(run)
    
    def remove(self, run_id: str) -> Optional[SubagentRunRecord]:
        """Remove a run from registry."""
        return self._runs.pop(run_id, None)
    
    def clear_terminated(self, max_age_minutes: int = 60) -> list[str]:
        """Clear terminated runs older than max_age_minutes."""
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
        removed = []
        for run_id, run in list(self._runs.items()):
            if run.is_terminal and run.ended_at and run.ended_at < cutoff:
                del self._runs[run_id]
                removed.append(run_id)
        return removed
    
    def set_status_change_callback(
        self,
        callback: Optional[Callable[[SubagentRunRecord], None]],
    ) -> None:
        """Set callback for status changes."""
        self._on_status_change = callback
    
    def _notify_status_change(self, run: SubagentRunRecord) -> None:
        """Notify callback of status change."""
        if self._on_status_change:
            self._on_status_change(run)


def parse_session_key_safe(session_key: str) -> dict[str, str]:
    """Parse session key with safe defaults."""
    from nano_openclaw.features.subagents.types import parse_session_key
    result = parse_session_key(session_key)
    if "agentId" not in result:
        result["agentId"] = "default"
    return result


_registry: Optional[SubagentRegistry] = None


def get_registry() -> SubagentRegistry:
    """Get the global registry instance."""
    global _registry
    if _registry is None:
        _registry = SubagentRegistry()
    return _registry


def reset_registry() -> None:
    """Reset the global registry (for testing)."""
    global _registry
    _registry = None