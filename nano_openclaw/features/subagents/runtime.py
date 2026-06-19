"""Subagent runtime ports used by the core loop."""

from __future__ import annotations

import asyncio
from typing import Any

from nano_openclaw.features.subagents.runner import get_runner
from nano_openclaw.features.subagents.types import parse_session_key


def agent_id_from_session_key(session_key: str) -> str:
    parsed = parse_session_key(session_key)
    return parsed.get("agentId", "default")


async def wait_for_subagent_announcements(
    registry: Any,
    cfg: Any,
    cancellation_token: Any | None,
) -> list[Any]:
    spawn_context = getattr(registry, "_spawn_tool_context", None)
    requester_session_key = getattr(spawn_context, "requester_session_key", None) or cfg.session_key
    if not requester_session_key:
        return []

    runner = get_runner()
    try:
        await runner.wait_for_requester(requester_session_key, cancellation_token=cancellation_token)
    except asyncio.CancelledError:
        raise
    return runner.drain_announcements(requester_session_key)
