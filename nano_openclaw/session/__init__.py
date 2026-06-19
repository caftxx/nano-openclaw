"""Session persistence for nano-openclaw.

Mirrors openclaw's session system:
- Session storage in {stateDir}/agents/{agentId}/sessions/
- Transcript files in JSONL format
- Session index in sessions.json

Public interface for session management: store, transcript, truncation, paths.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    # Types
    "SessionInfo": ("nano_openclaw.session.types", "SessionInfo"),
    "SessionHeader": ("nano_openclaw.session.types", "SessionHeader"),
    "TranscriptMessage": ("nano_openclaw.session.types", "TranscriptMessage"),
    "TranscriptCompaction": ("nano_openclaw.session.types", "TranscriptCompaction"),
    "new_session_id": ("nano_openclaw.session.types", "new_session_id"),
    # Store
    "load_session_store": ("nano_openclaw.session.store", "load_session_store"),
    "save_session_store": ("nano_openclaw.session.store", "save_session_store"),
    "get_last_session": ("nano_openclaw.session.store", "get_last_session"),
    "update_session": ("nano_openclaw.session.store", "update_session"),
    "list_sessions": ("nano_openclaw.session.store", "list_sessions"),
    # Transcript
    "TranscriptWriter": ("nano_openclaw.session.transcript", "TranscriptWriter"),
    "TranscriptReader": ("nano_openclaw.session.transcript", "TranscriptReader"),
    # Truncate
    "truncate_tool_result": ("nano_openclaw.session.truncate", "truncate_tool_result"),
    "MAX_TOOL_RESULT_CHARS": ("nano_openclaw.session.truncate", "MAX_TOOL_RESULT_CHARS"),
    # Paths
    "DEFAULT_AGENT_ID": ("nano_openclaw.session.paths", "DEFAULT_AGENT_ID"),
    "resolve_agent_sessions_dir": ("nano_openclaw.session.paths", "resolve_agent_sessions_dir"),
    "resolve_session_store_path": ("nano_openclaw.session.paths", "resolve_session_store_path"),
    "resolve_session_transcript_path": (
        "nano_openclaw.session.paths",
        "resolve_session_transcript_path",
    ),
}


def __getattr__(name: str) -> Any:
    """Lazy-load public session helpers without importing transcript/provider."""
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value

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
