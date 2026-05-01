"""Session persistence for nano-openclaw.

Public interface for session management: store, transcript, truncation.
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

__all__ = [
    "SessionInfo",
    "SessionHeader",
    "TranscriptMessage",
    "TranscriptCompaction",
    "new_session_id",
    "load_session_store",
    "save_session_store",
    "get_last_session",
    "update_session",
    "list_sessions",
    "TranscriptWriter",
    "TranscriptReader",
    "truncate_tool_result",
    "MAX_TOOL_RESULT_CHARS",
]
