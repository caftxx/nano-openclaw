"""Session path resolution.

Mirrors openclaw's src/config/sessions/paths.ts:
- resolve_agent_sessions_dir: {stateDir}/agents/{agentId}/sessions/
- resolve_session_store_path: sessions.json location
- resolve_session_transcript_path: {sessionId}.jsonl location
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

DEFAULT_AGENT_ID = "default"


def resolve_agent_sessions_dir(
    state_dir: Path,
    agent_id: Optional[str] = None,
) -> Path:
    """
    Resolve agent session directory.
    
    Mirrors openclaw's resolveAgentSessionsDir():
    {stateDir}/agents/{agentId}/sessions/
    
    Args:
        state_dir: State directory (from resolve_state_dir)
        agent_id: Agent identifier, defaults to "default"
    
    Returns:
        Session directory path
    """
    agent_id = agent_id or DEFAULT_AGENT_ID
    return state_dir / "agents" / agent_id / "sessions"


def resolve_session_store_path(sessions_dir: Path) -> Path:
    """
    Return sessions.json path.
    
    Args:
        sessions_dir: Agent session directory
    
    Returns:
        Path to sessions.json
    """
    return sessions_dir / "sessions.json"


def resolve_session_transcript_path(sessions_dir: Path, session_id: str) -> Path:
    """
    Return session transcript file path.
    
    Args:
        sessions_dir: Agent session directory
        session_id: Session UUID
    
    Returns:
        Path to {sessionId}.jsonl
    """
    return sessions_dir / f"{session_id}.jsonl"
