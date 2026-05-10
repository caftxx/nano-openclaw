"""Compatibility shim — sessions module moved to gateway.agent_backend_session.

Phase 0 of the gateway port (plan: /home/caft/.claude/plans/1-5000-2-tender-dusk.md)
promoted the WebUI session abstraction to be the shared in-memory entity owned
by the Backend. Old names re-exported here to keep tests and any external
imports working until the Phase ends.

Renames:
    WebSession         -> AgentBackendSession
    WebSessionManager  -> BackendSessionManager
"""

from nano_openclaw.gateway.agent_backend_session import (
    AgentBackendSession as WebSession,
    BackendSessionManager as WebSessionManager,
    SessionSummary,
    display_history,
    is_subagent_announcement,
    message_text,
    message_to_json,
    session_preview,
    session_search_text,
    session_title,
)

__all__ = [
    "WebSession",
    "WebSessionManager",
    "SessionSummary",
    "display_history",
    "is_subagent_announcement",
    "message_text",
    "message_to_json",
    "session_preview",
    "session_search_text",
    "session_title",
]
