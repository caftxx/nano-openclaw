"""Data types for session persistence in nano-openclaw.

Mirrors OpenClaw's session types in `src/config/sessions/types.ts`
and `src/config/sessions/transcript.ts`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass
class SessionInfo:
    """Lightweight metadata stored in sessions.json per session."""
    session_id: str
    created_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    updated_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    model: str = ""
    message_count: int = 0
    compaction_count: int = 0


@dataclass
class SessionHeader:
    """First entry in a .jsonl transcript file."""
    type: Literal["session"] = "session"
    version: int = 1
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cwd: str = ""
    model: str = ""


@dataclass
class TranscriptMessage:
    """A single message entry in a .jsonl transcript."""
    type: Literal["message"] = "message"
    id: str = field(default_factory=lambda: f"msg-{uuid.uuid4().hex[:8]}")
    parent_id: str = ""
    role: Literal["user", "assistant"] = "user"
    content: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TranscriptCompaction:
    """A compaction entry in a .jsonl transcript."""
    type: Literal["compaction"] = "compaction"
    id: str = field(default_factory=lambda: f"comp-{uuid.uuid4().hex[:8]}")
    parent_id: str = ""
    summary: str = ""


TranscriptEntry = SessionHeader | TranscriptMessage | TranscriptCompaction


def new_session_id() -> str:
    """Generate a new UUID-based session ID."""
    return str(uuid.uuid4())
