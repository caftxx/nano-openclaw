"""Session persistence for nano-openclaw.

Mirrors openclaw's session system:
- Session storage in {stateDir}/agents/{agentId}/sessions/
- Transcript files in JSONL format
- Session index in sessions.json

Public interface for session management: store, transcript, truncation, paths.
"""

from .types import SessionInfo, SessionHeader, TranscriptMessage, TranscriptCompaction, new_session_id
from .store import (
    load_session_store,
    save_session_store,
    get_last_session,
    update_session,
    list_sessions,
)
from .transcript import TranscriptWriter, TranscriptReader
from .truncate import truncate_tool_result, MAX_TOOL_RESULT_CHARS
from .paths import (
    DEFAULT_AGENT_ID,
    resolve_agent_sessions_dir,
    resolve_session_store_path,
    resolve_session_transcript_path,
)

__all__ = [
    # Types
    "SessionInfo",
    "SessionHeader",
    "TranscriptMessage",
    "TranscriptCompaction",
    "new_session_id",
    # Store
    "load_session_store",
    "save_session_store",
    "get_last_session",
    "update_session",
    "list_sessions",
    # Transcript
    "TranscriptWriter",
    "TranscriptReader",
    # Truncate
    "truncate_tool_result",
    "MAX_TOOL_RESULT_CHARS",
    # Paths
    "DEFAULT_AGENT_ID",
    "resolve_agent_sessions_dir",
    "resolve_session_store_path",
    "resolve_session_transcript_path",
]
