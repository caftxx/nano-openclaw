"""tools.* / skills.* / plugins.* / hooks.* RPC — runtime introspection.

These cheap reads back the ``/tools`` ``/skills`` ``/plugins`` ``/hooks``
slash commands when the TUI runs in remote mode. Embedded mode reads the
same data directly off ``runtime.registry`` / ``runtime.cfg``; the RPC
parity matters so both modes render the same panels.
"""

from __future__ import annotations

from typing import Any

from nano_openclaw.api.context import GatewayContext


async def tools_list(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"tools": await ctx.backend.tools_list()}


async def skills_list(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"skills": await ctx.backend.skills_list()}


async def plugins_list(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"plugins": await ctx.backend.plugins_list()}


async def hooks_list(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    return {"hooks": await ctx.backend.hooks_list()}


HANDLERS = {
    "tools.list": tools_list,
    "skills.list": skills_list,
    "plugins.list": plugins_list,
    "hooks.list": hooks_list,
}
