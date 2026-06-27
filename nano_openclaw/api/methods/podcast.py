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
        host_model_ref=str(params.get("host_model_ref") or params.get("hostModelRef") or ""),
        host_model_label=str(params.get("host_model_label") or params.get("hostModelLabel") or ""),
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


async def podcast_remove_agent(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.podcast_remove_agent(
        run_id=str(params.get("run_id") or params.get("runId") or ""),
        agent_id=str(params.get("agent_id") or params.get("agentId") or ""),
    )


async def podcast_add_agent(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.podcast_add_agent(
        run_id=str(params.get("run_id") or params.get("runId") or ""),
        agent=dict(params.get("agent") or {}),
    )


async def podcast_update_agent(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.podcast_update_agent(
        run_id=str(params.get("run_id") or params.get("runId") or ""),
        agent=dict(params.get("agent") or {}),
    )


async def podcast_update_host(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.podcast_update_host(
        run_id=str(params.get("run_id") or params.get("runId") or ""),
        host_voice_id=str(params.get("host_voice_id") or params.get("hostVoiceId") or ""),
        host_voice_label=str(params.get("host_voice_label") or params.get("hostVoiceLabel") or ""),
        model_ref=str(params.get("model_ref") or params.get("modelRef") or ""),
        model_label=str(params.get("model_label") or params.get("modelLabel") or ""),
    )


HANDLERS = {
    "podcast.start": podcast_start,
    "podcast.input": podcast_input,
    "podcast.stop": podcast_stop,
    "podcast.remove_agent": podcast_remove_agent,
    "podcast.add_agent": podcast_add_agent,
    "podcast.update_agent": podcast_update_agent,
    "podcast.update_host": podcast_update_host,
}
