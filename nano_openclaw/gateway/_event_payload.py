"""Event → JSON payload conversion shared between EmbeddedBackend and webui.

Lives at gateway/ level because both EmbeddedBackend (push-event emission) and
gateway/webui/server.py (WebSocket framing) consume it. Lifted from
gateway/webui/server.py during Phase 0 of the gateway port; webui imports
re-routed through here.
"""

from __future__ import annotations

from typing import Any

from nano_openclaw.attachments import AttachmentAttached, AttachmentError
from nano_openclaw.loop import (
    ActiveMemoryRecall,
    Compaction,
    ImageAttached,
    ImageDescribe,
    ImageError,
    ImageSkip,
    SkillInvoked,
    SubagentAnnounced,
    SubagentEvent,
    SubagentKilled,
    SubagentProgress,
    SubagentSpawned,
    ToolResult,
)
from nano_openclaw.provider import (
    MessageEnd,
    TextDelta,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
)


def event_to_payload(event: Any, turn_id: str, session_id: str) -> dict[str, Any]:
    """Convert one stream/loop event dataclass into a JSON-friendly dict.

    Output dict carries ``turn_id`` and ``session_id`` so multi-session
    subscribers can demux events. The ``type`` field is the wire identifier
    that frontends switch on.
    """
    base = {"turn_id": turn_id, "session_id": session_id}
    if isinstance(event, TextDelta):
        return {"type": "text.delta", **base, "text": event.text}
    if isinstance(event, ThinkingDelta):
        return {"type": "thinking.delta", **base, "text": event.text}
    if isinstance(event, ThinkingBlockComplete):
        return {"type": "thinking.done", **base, "redacted": event.redacted}
    if isinstance(event, ToolUseStart):
        return {"type": "tool.start", **base, "tool_use_id": event.id, "name": event.name}
    if isinstance(event, ToolUseDelta):
        return {"type": "tool.delta", **base, "tool_use_id": event.id, "partial_json": event.partial_json}
    if isinstance(event, ToolUseEnd):
        return {"type": "tool.end", **base, "tool_use_id": event.id}
    if isinstance(event, ToolResult):
        return {
            "type": "tool.result",
            **base,
            "tool_use_id": event.tool_use_id,
            "name": event.name,
            "args": event.args,
            "result": event.result,
        }
    if isinstance(event, MessageEnd):
        return {"type": "message.end", **base, "stop_reason": event.stop_reason, "usage": event.usage}
    if isinstance(event, Compaction):
        return {"type": "compaction", **base, "summary": event.summary}
    if isinstance(event, ImageDescribe):
        return {"type": "image.status", **base, "ref": event.ref, "status": "describing"}
    if isinstance(event, ImageAttached):
        return {"type": "image.status", **base, "refs": event.refs, "status": "described" if event.via_model else "attached"}
    if isinstance(event, ImageError):
        return {"type": "image.status", **base, "ref": event.ref, "status": "error", "error": event.error}
    if isinstance(event, ImageSkip):
        return {"type": "image.status", **base, "ref": event.ref, "status": "skipped", "reason": event.reason}
    if isinstance(event, AttachmentAttached):
        return {"type": "attachment.status", **base, "refs": event.refs, "status": "attached"}
    if isinstance(event, AttachmentError):
        return {"type": "attachment.status", **base, "ref": event.ref, "status": "error", "error": event.error}
    if isinstance(event, SkillInvoked):
        return {"type": "skill.invoked", **base, "skill_name": event.skill_name, "skill_path": event.skill_path}
    if isinstance(event, ActiveMemoryRecall):
        return {"type": "active_memory", **base, "result": jsonable(event.result)}
    if isinstance(event, SubagentSpawned):
        return {"type": "subagent.status", **base, "status": "spawned", **jsonable(event)}
    if isinstance(event, SubagentAnnounced):
        return {"type": "subagent.status", **base, "status": event.status, **jsonable(event)}
    if isinstance(event, SubagentKilled):
        return {"type": "subagent.status", **base, "status": "killed", **jsonable(event)}
    if isinstance(event, SubagentProgress):
        return {"type": "subagent.status", **base, "status": "progress", **jsonable(event)}
    if isinstance(event, SubagentEvent):
        nested = event_to_payload(event.event, turn_id, session_id)
        nested.pop("turn_id", None)
        nested.pop("session_id", None)
        return {
            "type": "subagent.event",
            **base,
            "run_id": event.run_id,
            "label": event.label,
            "task": event.task,
            "event": nested,
        }
    return {"type": "event", **base, "event_type": type(event).__name__, "payload": jsonable(event)}


def is_replayable_activity_payload(payload: dict[str, Any]) -> bool:
    """Whether a payload should be persisted in the session's activity log
    (for re-render after WebUI reload). High-frequency deltas are skipped.
    """
    kind = payload.get("type")
    if kind in {"text.delta", "tool.delta", "tool.end", "message.end"}:
        return False
    if kind in {"thinking.delta", "thinking.done", "tool.start", "tool.result", "compaction", "subagent.event"}:
        return True
    return bool(
        isinstance(kind, str)
        and (kind.endswith(".status") or kind.endswith(".invoked") or "memory" in kind)
    )


def jsonable(value: Any) -> Any:
    """Best-effort coerce dataclasses / pydantic / dicts / lists to JSON-friendly form."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dataclass_fields__"):
        return {key: jsonable(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value
