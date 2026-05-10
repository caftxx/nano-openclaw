"""gateway.* RPC handlers — daemon lifecycle (currently just restart)."""

from __future__ import annotations

from typing import Any

from nano_openclaw.gateway.context import GatewayContext


async def gateway_restart(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.gateway_restart()


HANDLERS = {
    "gateway.restart": gateway_restart,
}
