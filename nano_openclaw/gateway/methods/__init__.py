"""RPC method registry for the gateway WebSocket.

Each ``method`` family lives in its own file (``chat.py``, ``sessions.py``,
...). Each handler is ``async def(ctx: GatewayContext, params: dict) -> Any``
and just unpacks ``params`` + delegates to ``ctx.backend.METHOD(...)``.
This keeps the wire surface and the in-process Backend interface in lockstep.

``CORE_HANDLERS`` is the dispatch table consumed by ``ws_route.py``.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from nano_openclaw.gateway.context import GatewayContext
from nano_openclaw.gateway.methods import (
    approvals as _approvals,
    channels as _channels,
    chat as _chat,
    features as _features,
    gateway as _gateway,
    health as _health,
    introspection as _introspection,
    models as _models,
    runtime as _runtime,
    sessions as _sessions,
    subagents as _subagents,
)


Handler = Callable[[GatewayContext, dict], Awaitable[object]]


CORE_HANDLERS: dict[str, Handler] = {
    **_chat.HANDLERS,
    **_sessions.HANDLERS,
    **_approvals.HANDLERS,
    **_models.HANDLERS,
    **_runtime.HANDLERS,
    **_channels.HANDLERS,
    **_subagents.HANDLERS,
    **_features.HANDLERS,
    **_introspection.HANDLERS,
    **_health.HANDLERS,
    **_gateway.HANDLERS,
}


__all__ = ["CORE_HANDLERS", "Handler"]
