"""LLM-facing runtime introspection tools.

Wraps a curated subset of Backend RPCs as ToolRegistry-registered tools so
the model can drive slash-command equivalents through natural language —
``list_models`` to see the catalog, ``switch_model`` to swap the active
model (gated by the approval flow), and read-only introspection
(``get_runtime``, ``get_context``, ``list_sessions``, ``list_tools``,
``list_skills``, ``list_channels``, ``get_health``).

Why a separate module: ``tools.py``'s built-ins cover file / shell / memory
primitives; this set surfaces *gateway* state. Registration is wired into
``EmbeddedBackend.__init__`` so the closures hold a live ``Backend``
reference — ``runtime.registry`` is built before ``EmbeddedBackend`` exists,
so we can't bind these in ``build_agent_runtime``.

Approval gating: ``switch_model`` is in ``ApprovalPolicy.dangerous_tools``
+ ``tool_configs.requires_approval=True`` (see approvals/types.py), so
interactive turns prompt the user and cron / channel auto-turns deny by
default unless explicitly allowlisted. The same gating applies to
``restart`` (registered in runtime.py:_register_restart_tool — separate
module because it needs direct runtime mutation; semantically part of this
LLM-facing surface).

Destructive verbs not exposed as tools: /clear, /new, /compact. They
mutate session state in ways that should remain user-driven; the model
can describe what to do but the user runs the slash explicitly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from nano_openclaw.core.tools import Tool, ToolRegistry

if TYPE_CHECKING:
    from nano_openclaw.gateway.backend import Backend


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _err(message: str, **extras: Any) -> str:
    return json.dumps({"ok": False, "error": message, **extras}, ensure_ascii=False)


def register_runtime_tools(registry: ToolRegistry, backend: "Backend") -> None:
    """Register the runtime introspection / switch tools onto ``registry``.

    Idempotent: re-registering overwrites the existing entry. Safe to call
    after a runtime hot-reload — the new ``backend`` reference replaces the
    old closure.
    """

    # ─── list_models ───
    async def _list_models(args: dict[str, Any]) -> str:
        choices = await backend.models_list()
        snap = await backend.runtime_get()
        return _ok({
            "current_ref": snap.model_ref,
            "models": [
                {
                    "ref": m.ref,
                    "id": m.id,
                    "provider": m.provider,
                    "name": m.name or m.id,
                    "input": list(m.input),
                    "reasoning": m.reasoning,
                    "context_window": m.context_window,
                    "max_tokens": m.max_tokens,
                    "is_default": m.is_default,
                }
                for m in choices
            ],
        })

    registry.register(Tool(
        name="list_models",
        description=(
            "List every model declared under the project's `models.providers` config. "
            "Returns each model's ref (provider/model-id), display name, supported "
            "input modalities, reasoning support, and context window. Use this to "
            "discover available models before calling switch_model."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=_list_models,
    ))

    # ─── switch_model ───
    # Gated by ``ApprovalManager`` because changing the model mid-conversation
    # alters cost / behavior; require the human's nod for interactive turns,
    # and block by default on cron / channel auto-turns (the
    # NonInteractiveApprovalHandler returns DENY unless the user has
    # allowlisted ``switch_model`` for that context).
    async def _switch_model(args: dict[str, Any]) -> str:
        model_ref = str(args.get("model_ref") or "").strip()
        if not model_ref:
            return _err("model_ref is required (e.g. 'anthropic/claude-sonnet-4-5')")
        snap_before = await backend.runtime_get()
        if snap_before.model_ref == model_ref:
            return _ok({
                "noop": True,
                "model_ref": snap_before.model_ref,
                "message": f"already on {model_ref}",
            })
        try:
            from nano_openclaw.gateway.slash import _resolve_model_option
            target = _resolve_model_option(getattr(backend, "runtime").config, model_ref) if hasattr(backend, "runtime") else None
        except (KeyError, ValueError) as exc:
            return _err(f"unknown or ambiguous model: {exc}")
        target_ref = target["ref"] if target else model_ref
        try:
            new_snap = await backend.runtime_update(model_ref=target_ref)
        except Exception as exc:  # noqa: BLE001
            return _err(f"runtime_update failed: {type(exc).__name__}: {exc}")
        return _ok({
            "from": snap_before.model_ref,
            "to": new_snap.model_ref,
            "model_id": new_snap.model_id,
            "context_window": new_snap.context_window,
            "thinking_level": new_snap.thinking_level,
        })

    registry.register(Tool(
        name="switch_model",
        description=(
            "Switch the active model for the current conversation. Argument: "
            "`model_ref` like `provider/model-id`. Requires user approval in "
            "interactive sessions; cron / channel-driven turns are denied by "
            "default. Use list_models first to find a valid ref."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model_ref": {
                    "type": "string",
                    "description": "Model reference like 'anthropic/claude-sonnet-4-5'.",
                },
            },
            "required": ["model_ref"],
            "additionalProperties": False,
        },
        run=_switch_model,
    ))

    # ─── set_thinking ───
    # Not in ``dangerous_tools`` — adjusting thinking budget mid-conversation
    # is a routine UX knob (mirrors ``/thinking`` slash). The model can flip
    # it the same way it flips other knobs, no approval round-trip.
    async def _set_thinking(args: dict[str, Any]) -> str:
        from nano_openclaw.gateway.slash import THINKING_LEVELS

        level = str(args.get("level") or "").strip().lower()
        if not level:
            return _err(
                "level is required (one of: " + " | ".join(THINKING_LEVELS) + ")"
            )
        if level not in THINKING_LEVELS:
            return _err(
                f"unknown thinking level: {level}",
                allowed=list(THINKING_LEVELS),
            )
        snap_before = await backend.runtime_get()
        if snap_before.thinking_level == level:
            return _ok({
                "noop": True,
                "thinking_level": level,
                "message": f"already on {level}",
            })
        # This tool runs *inside* a turn, which holds the RuntimeUpdateGuard
        # reader — calling runtime_update (writer) here would raise BusyError.
        # Queue the change so the backend applies it after the turn ends;
        # it takes effect from the next turn, exactly like ``/thinking``.
        queue = getattr(backend, "queue_thinking_level", None)
        if callable(queue):
            queue(level)
            return _ok({
                "from": snap_before.thinking_level,
                "to": level,
                "effective": "next_turn",
                "message": (
                    f"thinking will switch from {snap_before.thinking_level} "
                    f"to {level} starting next turn"
                ),
            })
        # Fallback for backends without the queue (e.g. remote): best-effort
        # immediate update — only succeeds when no turn is in flight.
        try:
            new_snap = await backend.runtime_update(thinking_level=level)
        except Exception as exc:  # noqa: BLE001
            return _err(f"runtime_update failed: {type(exc).__name__}: {exc}")
        return _ok({
            "from": snap_before.thinking_level,
            "to": new_snap.thinking_level,
        })

    registry.register(Tool(
        name="set_thinking",
        description=(
            "Adjust the thinking-budget level for this conversation. "
            "Argument: `level` ∈ {off, minimal, low, medium, high, xhigh, "
            "adaptive, max}. Higher levels give the model more tokens to "
            "deliberate before responding (xhigh / max ~32k tokens). The "
            "change takes effect from your NEXT turn — the current turn keeps "
            "its level — mirroring the /thinking slash command. Use when an "
            "upcoming task warrants more (or less) deliberation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"],
                    "description": "Thinking level to switch to.",
                },
            },
            "required": ["level"],
            "additionalProperties": False,
        },
        run=_set_thinking,
    ))

    # ─── get_runtime ───
    async def _get_runtime(args: dict[str, Any]) -> str:
        snap = await backend.runtime_get()
        return _ok({
            "agent_id": snap.agent_id,
            "model_ref": snap.model_ref,
            "model_id": snap.model_id,
            "image_model_ref": snap.image_model_ref,
            "thinking_level": snap.thinking_level,
            "workspace_dir": snap.workspace_dir,
            "context_budget": snap.context_budget,
            "context_window": snap.context_window,
        })

    registry.register(Tool(
        name="get_runtime",
        description="Show the current runtime: active agent, model ref/id, thinking level, workspace dir, context budget.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=_get_runtime,
    ))

    # ─── get_context ───
    async def _get_context(args: dict[str, Any]) -> str:
        session_key = str(args.get("session_key") or "").strip()
        snap = await backend.runtime_get()
        msg_count = 0
        if session_key:
            try:
                payload = await backend.chat_history(session_key)
                msg_count = len(payload.history)
            except Exception:  # noqa: BLE001
                msg_count = 0
        return _ok({
            "session_key": session_key,
            "messages": msg_count,
            "context_budget": snap.context_budget,
            "context_threshold": snap.context_threshold,
            "context_recent_turns": snap.context_recent_turns,
            "context_window": snap.context_window,
            "thinking_level": snap.thinking_level,
        })

    registry.register(Tool(
        name="get_context",
        description=(
            "Report context-window usage for a session. Optional `session_key`; "
            "when omitted, returns budget/threshold without per-session counts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_key": {"type": "string", "description": "Session id to inspect (optional)."},
            },
            "additionalProperties": False,
        },
        run=_get_context,
    ))

    # ─── list_sessions ───
    async def _list_sessions(args: dict[str, Any]) -> str:
        result = await backend.sessions_list()
        return _ok({
            "sessions": [
                {
                    "session_id": s.session_id,
                    "title": s.title,
                    "preview": (s.preview or "")[:200],
                    "message_count": s.message_count,
                    "compaction_count": s.compaction_count,
                    "model": s.model,
                    "current": s.current,
                    "updated_at": s.updated_at,
                }
                for s in result.sessions
            ],
            "last_session_id": result.last_session_id,
        })

    registry.register(Tool(
        name="list_sessions",
        description="List saved sessions (id, title, message count, model, last update).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=_list_sessions,
    ))

    # ─── list_tools ───
    async def _list_tools(args: dict[str, Any]) -> str:
        tools = await backend.tools_list()
        return _ok({
            "tools": [
                {
                    "name": t.get("name", ""),
                    "description": (t.get("description") or "").splitlines()[0][:200],
                }
                for t in sorted(tools, key=lambda x: x.get("name", ""))
            ],
        })

    registry.register(Tool(
        name="list_tools",
        description="List every tool registered with the agent (name + first description line).",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=_list_tools,
    ))

    # ─── list_skills ───
    async def _list_skills(args: dict[str, Any]) -> str:
        skills = await backend.skills_list()
        return _ok({
            "skills": [
                {
                    "name": s.get("name", ""),
                    "source": s.get("source", ""),
                    "eligible": bool(s.get("eligible")),
                    "in_prompt": bool(s.get("in_prompt")),
                    "reason": (s.get("reason") or "")[:120],
                }
                for s in sorted(skills, key=lambda x: x.get("name", ""))
            ],
        })

    registry.register(Tool(
        name="list_skills",
        description="List skills available in the workspace and whether each is currently in-prompt.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=_list_skills,
    ))

    # ─── list_channels ───
    async def _list_channels(args: dict[str, Any]) -> str:
        statuses = await backend.channels_status()
        return _ok({
            "channels": [
                {
                    "channel_id": c.channel_id,
                    "account_id": c.account_id,
                    "state": c.state,
                    "error": c.error,
                    "started_at": c.started_at,
                }
                for c in statuses
            ],
        })

    registry.register(Tool(
        name="list_channels",
        description="List daemon channels (e.g. wechat) and their state.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=_list_channels,
    ))

    # ─── get_health ───
    async def _get_health(args: dict[str, Any]) -> str:
        h = await backend.health()
        return _ok({
            "runtime_ready": h.runtime_ready,
            "channels_running": h.channels_running,
            "sessions_loaded": h.sessions_loaded,
            "in_flight_turns": h.in_flight_turns,
        })

    registry.register(Tool(
        name="get_health",
        description="Snapshot of daemon health: runtime status, running channels, loaded sessions, in-flight turns.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=_get_health,
    ))
