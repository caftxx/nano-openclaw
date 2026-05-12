"""sessions.* RPC handlers — list / get / delete / reset / compact."""

from __future__ import annotations

from typing import Any

from nano_openclaw.gateway.context import GatewayContext


async def sessions_list(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    result = await ctx.backend.sessions_list()
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "title": s.title,
                "preview": s.preview,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "model": s.model,
                "message_count": s.message_count,
                "compaction_count": s.compaction_count,
                "current": s.current,
                "active_turn_id": s.active_turn_id,
            }
            for s in result.sessions
        ],
        "last_session_id": result.last_session_id,
    }


async def sessions_get(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    session_id = str(params.get("session_id") or "")
    details = await ctx.backend.sessions_get(session_id)
    return {
        "session_id": details.session_id,
        "title": details.title,
        "history": details.history,
        "activities": details.activities,
        "model": details.model,
        "active_turn_id": details.active_turn_id,
    }


async def sessions_delete(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    session_id = str(params.get("session_id") or "")
    await ctx.backend.sessions_delete(session_id)
    return {"ok": True}


async def sessions_reset(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    session_key = str(params.get("session_key") or "")
    reason = str(params.get("reason") or "reset")
    if reason not in ("new", "reset"):
        reason = "reset"
    info = await ctx.backend.sessions_reset(session_key, reason=reason)  # type: ignore[arg-type]
    return {
        "session_id": info.session_id,
        "title": info.title,
        "preview": info.preview,
        "created_at": info.created_at,
        "updated_at": info.updated_at,
        "model": info.model,
        "message_count": info.message_count,
        "compaction_count": info.compaction_count,
        "current": info.current,
        "active_turn_id": info.active_turn_id,
    }


async def sessions_compact(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    session_key = str(params.get("session_key") or "")
    result = await ctx.backend.sessions_compact(session_key)
    return {
        "success": result.success,
        "summary": result.summary,
        "tokens_before": result.tokens_before,
        "tokens_after": result.tokens_after,
    }


async def sessions_usage(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    session_key = str(params.get("session_key") or "")
    report = await ctx.backend.sessions_usage(session_key)
    return {
        "session_id": report.session_id,
        "last_input_tokens": report.last_input_tokens,
        "last_output_tokens": report.last_output_tokens,
        "last_cache_read_tokens": report.last_cache_read_tokens,
        "last_cache_creation_tokens": report.last_cache_creation_tokens,
        "total_input_tokens": report.total_input_tokens,
        "total_output_tokens": report.total_output_tokens,
        "total_cache_read_tokens": report.total_cache_read_tokens,
        "total_cache_creation_tokens": report.total_cache_creation_tokens,
        "compactions_fired": report.compactions_fired,
        "turns_recorded": report.turns_recorded,
        "cache_hit_ratio": report.cache_hit_ratio,
        "context_budget": report.context_budget,
        "context_window": report.context_window,
        "cache_ttl": report.cache_ttl,
    }


HANDLERS = {
    "sessions.list": sessions_list,
    "sessions.get": sessions_get,
    "sessions.delete": sessions_delete,
    "sessions.reset": sessions_reset,
    "sessions.compact": sessions_compact,
    "sessions.usage": sessions_usage,
}
