"""podcast.* RPC handlers for Web Voice AI podcast mode."""

from __future__ import annotations

from typing import Any

from nano_openclaw.api.context import GatewayContext


async def podcast_start(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.podcast_start(
        session_key=str(params.get("session_key") or params.get("session_id") or ""),
        topic=str(params.get("topic") or ""),
        agents=list(params.get("agents") or []),
        rounds=int(params.get("rounds") or 20),
        host_voice_id=str(params.get("host_voice_id") or params.get("hostVoiceId") or ""),
        host_voice_label=str(params.get("host_voice_label") or params.get("hostVoiceLabel") or ""),
    )


async def podcast_input(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.podcast_input(
        run_id=str(params.get("run_id") or params.get("runId") or ""),
        text=str(params.get("text") or ""),
    )


async def podcast_stop(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.podcast_stop(
        run_id=str(params.get("run_id") or params.get("runId") or ""),
    )


HANDLERS = {
    "podcast.start": podcast_start,
    "podcast.input": podcast_input,
    "podcast.stop": podcast_stop,
}
