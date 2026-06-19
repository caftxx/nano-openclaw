"""todos.* RPC handlers — currently only ``todos.get``."""

from __future__ import annotations

from typing import Any

from nano_openclaw.api.context import GatewayContext


async def todos_get(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    session_key = str(params.get("session_key") or "")
    items = await ctx.backend.get_todos(session_key)
    return {"todos": items}


HANDLERS = {
    "todos.get": todos_get,
}
