"""runtime.* RPC handlers — get / update.

``runtime.update`` raises BusyError when any turn is in flight (Phase 7
formalizes that with an RWLock; v1 EmbeddedBackend already short-circuits).
"""

from __future__ import annotations

from typing import Any

from nano_openclaw.gateway.context import GatewayContext


def _snapshot_payload(snap) -> dict[str, Any]:
    return {
        "agent_id": snap.agent_id,
        "model_ref": snap.model_ref,
        "model_id": snap.model_id,
        "image_model_ref": snap.image_model_ref,
        "thinking_level": snap.thinking_level,
        "workspace_dir": snap.workspace_dir,
        "state_dir": snap.state_dir,
        "context_budget": snap.context_budget,
        "context_threshold": snap.context_threshold,
        "context_recent_turns": snap.context_recent_turns,
        "context_window": snap.context_window,
    }


async def runtime_get(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    snap = await ctx.backend.runtime_get()
    return _snapshot_payload(snap)


async def runtime_update(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    snap = await ctx.backend.runtime_update(
        agent_id=params.get("agent_id"),
        model_ref=params.get("model_ref"),
        image_model_ref=params.get("image_model_ref"),
        thinking_level=params.get("thinking_level"),
    )
    return _snapshot_payload(snap)


HANDLERS = {
    "runtime.get": runtime_get,
    "runtime.update": runtime_update,
}
