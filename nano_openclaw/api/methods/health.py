"""health RPC handler — daemon liveness summary."""

from __future__ import annotations

from typing import Any

from nano_openclaw.api.context import GatewayContext


async def health(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    from nano_openclaw import resolve_version

    summary = await ctx.backend.health()
    payload = {
        "version": resolve_version(),
        "runtime_ready": summary.runtime_ready,
        "channels_running": summary.channels_running,
        "sessions_loaded": summary.sessions_loaded,
        "in_flight_turns": summary.in_flight_turns,
    }
    if summary.extra:
        payload["extra"] = dict(summary.extra)
    return payload


HANDLERS = {
    "health": health,
}
