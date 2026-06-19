"""webui.* and voice.* RPC handlers."""

from __future__ import annotations

from typing import Any

from nano_openclaw.api.context import GatewayContext


async def webui_state(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.webui_state()


async def voice_token(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.voice_token()


HANDLERS = {
    "webui.state": webui_state,
    "voice.token": voice_token,
}
