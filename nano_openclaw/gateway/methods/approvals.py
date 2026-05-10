"""approvals.* RPC handlers — list / respond."""

from __future__ import annotations

from typing import Any

from nano_openclaw.gateway.context import GatewayContext


async def approvals_list(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    pending = await ctx.backend.approvals_list()
    return {
        "approvals": [
            {
                "request_id": p.request_id,
                "tool_name": p.tool_name,
                "tool_args": p.tool_args,
                "risk_level": p.risk_level,
                "reason": p.reason,
                "timestamp": p.timestamp,
                "origin": p.origin,
                "turn_id": p.turn_id,
            }
            for p in pending
        ],
    }


async def approvals_respond(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    request_id = str(params.get("request_id") or "")
    allow = bool(params.get("allow"))
    scope_raw = str(params.get("scope") or "once")
    if scope_raw not in ("once", "session", "always"):
        scope_raw = "once"
    reason = str(params.get("reason") or "")
    await ctx.backend.approvals_respond(
        request_id,
        allow=allow,
        scope=scope_raw,  # type: ignore[arg-type]
        reason=reason,
    )
    return {"ok": True}


HANDLERS = {
    "approvals.list": approvals_list,
    "approvals.respond": approvals_respond,
}
