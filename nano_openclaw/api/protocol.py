"""JSON-RPC frame definitions for the daemon WebSocket API.

Three frame kinds, all newline-free JSON:

- ``Request`` — client → daemon: ``{id, method, params}``
- ``Response`` — daemon → client: ``{id, ok, payload?, error?}``
- ``PushFrame`` — daemon → client: ``{event, payload, seq}``

The protocol surface (method names + payload shapes) is documented as the
v1 ``Backend`` Protocol — handlers are thin wrappers calling
``EmbeddedBackend`` so embedded TUI and remote TUI share one code path.

This file purposely stays small: shape contracts + JSON encoding only. Method
dispatch lives in ``ws_route.py`` and ``methods/``.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from nano_openclaw.services.event_payload import jsonable


# ────────────────────────────────────────────────────────────────────────────
# Error codes
# ────────────────────────────────────────────────────────────────────────────


class ErrorCode(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNKNOWN_METHOD = "UNKNOWN_METHOD"
    BUSY = "BUSY"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    INTERNAL = "INTERNAL"


class ErrorShape(BaseModel):
    """Wire-form error attached to a Response with ``ok=False``."""
    model_config = ConfigDict(populate_by_name=True)

    code: ErrorCode
    message: str
    retryable: bool = False
    retry_after_ms: int = Field(default=0, ge=0)
    details: dict[str, Any] = Field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────────
# Frame types
# ────────────────────────────────────────────────────────────────────────────


class Request(BaseModel):
    """Client → daemon. ``id`` correlates a Response."""
    model_config = ConfigDict(populate_by_name=True)

    id: str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class Response(BaseModel):
    """Daemon → client. Either ``ok=True`` with ``payload``, or ``ok=False``
    with ``error``. Mirrors openclaw's RespondFn shape (server-methods/shared-types.ts:35).
    """
    model_config = ConfigDict(populate_by_name=True)

    id: str
    ok: bool
    payload: Optional[Any] = None
    error: Optional[ErrorShape] = None


class PushFrame(BaseModel):
    """Daemon → client unsolicited event. ``seq`` is monotonic per connection
    so a client can detect drops and call ``chat.history(after_seq=...)``.

    ``event`` is the kind tag (``agent.event`` / ``approval.request`` /
    ``session.changed`` / ``channel.changed`` / ``gap``); ``payload`` is the
    kind-specific dict (already serialized by the Backend).
    """
    model_config = ConfigDict(populate_by_name=True)

    event: str
    payload: dict[str, Any]
    seq: int


# ────────────────────────────────────────────────────────────────────────────
# Encoding helpers
# ────────────────────────────────────────────────────────────────────────────


def encode_response(response: Response) -> str:
    """JSON-serialize a Response. Payload may contain Backend dataclasses
    (SessionInfo, ChannelStatusEntry, ...); ``jsonable`` walks them.
    """
    obj: dict[str, Any] = {"id": response.id, "ok": response.ok}
    if response.payload is not None:
        obj["payload"] = jsonable(response.payload)
    if response.error is not None:
        obj["error"] = response.error.model_dump(mode="json")
    return json.dumps(obj, ensure_ascii=False, default=str)


def encode_push(frame: PushFrame) -> str:
    """JSON-serialize a PushFrame."""
    return json.dumps(
        {"event": frame.event, "payload": jsonable(frame.payload), "seq": frame.seq},
        ensure_ascii=False,
        default=str,
    )


def make_error_response(req_id: str, code: ErrorCode, message: str, **kwargs: Any) -> Response:
    """Convenience: build a ``Response(ok=False, error=...)``."""
    return Response(
        id=req_id,
        ok=False,
        error=ErrorShape(code=code, message=message, **kwargs),
    )


def make_ok_response(req_id: str, payload: Any = None) -> Response:
    return Response(id=req_id, ok=True, payload=payload)


# ────────────────────────────────────────────────────────────────────────────
# Method name catalog (v1) — single source of truth for valid methods.
# ────────────────────────────────────────────────────────────────────────────


METHODS = frozenset({
    # Chat
    "chat.send", "chat.abort", "chat.history",
    # Sessions
    "sessions.list", "sessions.get", "sessions.delete", "sessions.reset", "sessions.compact",
    "sessions.usage",
    # Todos
    "todos.get",
    # Approvals
    "approvals.list", "approvals.respond",
    # Models
    "models.list",
    # Runtime
    "runtime.get", "runtime.update",
    # Slash commands
    "slash.run",
    # Channels
    "channels.status", "channels.start", "channels.stop",
    # Subagents
    "subagents.list", "subagents.kill",
    # WebUI / Talk / voice
    "webui.state", "voice.token", "talk.config", "talk.speak",
    "podcast.start", "podcast.input", "podcast.stop", "podcast.remove_agent", "podcast.update_agent",
    # Features (active-memory / dreaming / review-fork / curator / checkpoint)
    "active_memory.get", "active_memory.set",
    "dreaming.get", "dreaming.set", "dreaming.run",
    "review_fork.get", "review_fork.set", "review_fork.run",
    "curator.get", "curator.set", "curator.run",
    "checkpoint.list", "checkpoint.create", "checkpoint.restore",
    "mcp.status",
    # Introspection (tools / skills / plugins / hooks)
    "tools.list", "skills.list", "plugins.list", "hooks.list",
    # Misc
    "health",
    # Gateway lifecycle
    "gateway.restart",
})
