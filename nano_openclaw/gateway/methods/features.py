"""Feature-control RPC handlers — active-memory + dreaming.

Same delegation pattern as the other method modules: unpack params, call
into ``ctx.backend.METHOD(...)``, return whatever dict the Backend layer
already produced. Mutating handlers (``.set``) accept any subset of the
field names the Backend exposes and ignore unknowns.
"""

from __future__ import annotations

from typing import Any

from nano_openclaw.gateway.context import GatewayContext


# ────────────────────────────────────────────────────────────────────────────
# Active Memory
# ────────────────────────────────────────────────────────────────────────────


async def active_memory_get(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.active_memory_get()


async def active_memory_set(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    # Pass everything along — Backend.active_memory_set does field-by-field
    # filtering and validation (raises BackendError for bad enum values).
    return await ctx.backend.active_memory_set(**params)


# ────────────────────────────────────────────────────────────────────────────
# Dreaming
# ────────────────────────────────────────────────────────────────────────────


async def dreaming_get(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.dreaming_get()


async def dreaming_set(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.dreaming_set(**params)


async def dreaming_run(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.dreaming_run()


# ────────────────────────────────────────────────────────────────────────────
# Review Fork
# ────────────────────────────────────────────────────────────────────────────


async def review_fork_get(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.review_fork_get()


async def review_fork_set(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return await ctx.backend.review_fork_set(**params)


async def review_fork_run(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    session_key = params.get("session_key") if params else None
    return await ctx.backend.review_fork_run(session_key=session_key)


HANDLERS = {
    "active_memory.get": active_memory_get,
    "active_memory.set": active_memory_set,
    "dreaming.get": dreaming_get,
    "dreaming.set": dreaming_set,
    "dreaming.run": dreaming_run,
    "review_fork.get": review_fork_get,
    "review_fork.set": review_fork_set,
    "review_fork.run": review_fork_run,
}
