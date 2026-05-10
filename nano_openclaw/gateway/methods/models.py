"""models.* RPC handlers — list."""

from __future__ import annotations

from typing import Any

from nano_openclaw.gateway.context import GatewayContext


async def models_list(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    choices = await ctx.backend.models_list()
    return {
        "models": [
            {
                "ref": m.ref,
                "id": m.id,
                "provider": m.provider,
                "context_window": m.context_window,
                "is_default": m.is_default,
            }
            for m in choices
        ],
    }


HANDLERS = {
    "models.list": models_list,
}
