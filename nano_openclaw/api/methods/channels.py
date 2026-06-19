"""channels.* RPC handlers — status / start / stop."""

from __future__ import annotations

from typing import Any

from nano_openclaw.api.context import GatewayContext


def _entry_to_dict(entry) -> dict[str, Any]:
    return {
        "channel_id": entry.channel_id,
        "account_id": entry.account_id,
        "state": entry.state,
        "error": entry.error,
        "started_at": entry.started_at,
    }


async def channels_status(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return live status for all running channel/account pairs."""
    statuses = await ctx.backend.channels_status()
    return {"channels": [_entry_to_dict(s) for s in statuses]}


async def channels_start(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(params.get("channel_id") or "")
    account_id = str(params.get("account_id") or "default")
    entry = await ctx.backend.channels_start(channel_id, account_id)
    return _entry_to_dict(entry)


async def channels_stop(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(params.get("channel_id") or "")
    account_id = str(params.get("account_id") or "default")
    entry = await ctx.backend.channels_stop(channel_id, account_id)
    return _entry_to_dict(entry)


HANDLERS = {
    "channels.status": channels_status,
    "channels.start": channels_start,
    "channels.stop": channels_stop,
}
