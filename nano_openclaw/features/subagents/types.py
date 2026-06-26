"""Sub-agent types and data structures.

Mirrors openclaw's subagent system but simplified for TUI scenario:
- No thread bindings (Discord-specific)
- No nested subagents (depth > 1)
- No ACP runtime
- Simplified announce mechanism
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional
import uuid


class SubagentStatus(Enum):
    """Subagent run status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"
    KILLED = "killed"


class SubagentContextMode(Enum):
    """Context mode for subagent spawn."""
    ISOLATED = "isolated"
    FORK = "fork"


class SubagentCleanupMode(Enum):
    """Cleanup mode after subagent completion."""
    KEEP = "keep"
    DELETE = "delete"


@dataclass
class SpawnParams:
    """Parameters for spawning a subagent."""
    task: str
    label: Optional[str] = None
    model: Optional[str] = None
    thinking: Optional[str] = None
    run_timeout_seconds: Optional[int] = None
    cleanup: SubagentCleanupMode = SubagentCleanupMode.KEEP
    context: SubagentContextMode = SubagentContextMode.ISOLATED

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "label": self.label,
            "model": self.model,
            "thinking": self.thinking,
            "runTimeoutSeconds": self.run_timeout_seconds,
            "cleanup": self.cleanup.value,
            "context": self.context.value,
        }


@dataclass
class SubagentRunRecord:
    """Record tracking a subagent run."""
    run_id: str
    child_session_key: str
    requester_session_key: str
    task: str
    label: Optional[str] = None
    model: Optional[str] = None
    status: SubagentStatus = SubagentStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    cleanup: SubagentCleanupMode = SubagentCleanupMode.KEEP
    outcome: Optional[str] = None
    result_text: Optional[str] = None
    elapsed_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    error_message: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        """Check if run has reached terminal state."""
        return self.status in {
            SubagentStatus.COMPLETED,
            SubagentStatus.ERROR,
            SubagentStatus.TIMEOUT,
            SubagentStatus.KILLED,
        }

    @property
    def session_id(self) -> str:
        """Extract session ID from session key."""
        return self.child_session_key.split(":")[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "childSessionKey": self.child_session_key,
            "requesterSessionKey": self.requester_session_key,
            "task": self.task,
            "label": self.label,
            "model": self.model,
            "status": self.status.value,
            "createdAt": self.created_at.isoformat(),
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "endedAt": self.ended_at.isoformat() if self.ended_at else None,
            "cleanup": self.cleanup.value,
            "outcome": self.outcome,
            "elapsedMs": self.elapsed_ms,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "errorMessage": self.error_message,
        }


def new_subagent_run_id() -> str:
    """Generate a new subagent run ID."""
    return uuid.uuid4().hex[:8]


def new_subagent_session_id() -> str:
    """Generate a new subagent session ID."""
    return uuid.uuid4().hex


def build_subagent_session_key(agent_id: str, session_id: str) -> str:
    """Build session key for subagent: agent:{agentId}:subagent:{uuid}."""
    return f"agent:{agent_id}:subagent:{session_id}"


def parse_session_key(session_key: str) -> dict[str, str]:
    """Parse session key into components."""
    parts = session_key.split(":")
    if len(parts) < 3 or parts[0] != "agent":
        return {"agentId": "default", "type": "main", "sessionId": session_key}
    
    agent_id = parts[1]
    if len(parts) == 3:
        return {"agentId": agent_id, "type": "main", "sessionId": parts[2]}
    
    if parts[2] == "subagent":
        return {"agentId": agent_id, "type": "subagent", "sessionId": parts[3]}
    
    return {"agentId": agent_id, "type": "unknown", "sessionId": parts[-1]}


@dataclass
class SubagentConfig:
    """Configuration for subagent behavior."""
    max_concurrent: int = 10
    max_spawn_depth: int = 1
    run_timeout_seconds: int = 0
    archive_after_minutes: int = 60
    model: Optional[str] = None
    thinking: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "maxConcurrent": self.max_concurrent,
            "maxSpawnDepth": self.max_spawn_depth,
            "runTimeoutSeconds": self.run_timeout_seconds,
            "archiveAfterMinutes": self.archive_after_minutes,
            "model": self.model,
            "thinking": self.thinking,
        }
