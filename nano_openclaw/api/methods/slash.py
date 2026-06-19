"""slash.* RPC handlers."""

from __future__ import annotations

from typing import Any

from nano_openclaw.api.context import GatewayContext


async def slash_run(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    command = str(params.get("command") or "")
    session_key = str(params.get("session_key") or "")
    result = await ctx.backend.slash_run(command, session_key=session_key)
    return {
        "handled": result.handled,
        "text": result.text,
        "session_key": result.session_key,
        "session_changed": result.session_changed,
    }


HANDLERS = {
    "slash.run": slash_run,
}
