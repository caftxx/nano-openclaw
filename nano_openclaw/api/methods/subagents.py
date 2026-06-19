"""subagents.* RPC handlers — list / kill."""

from __future__ import annotations

from typing import Any

from nano_openclaw.api.context import GatewayContext


async def subagents_list(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    items = await ctx.backend.subagents_list()
    return {
        "subagents": [
            {
                "run_id": s.run_id,
                "label": s.label,
                "task": s.task,
                "status": s.status,
                "started_at": s.started_at,
            }
            for s in items
        ],
    }


async def subagents_kill(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    run_id = str(params.get("run_id") or "")
    await ctx.backend.subagents_kill(run_id)
    return {"ok": True}


HANDLERS = {
    "subagents.list": subagents_list,
    "subagents.kill": subagents_kill,
}
