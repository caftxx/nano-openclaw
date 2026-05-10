"""channels.* RPC handlers — status / start / stop."""

from __future__ import annotations

from typing import Any

from nano_openclaw.channels.base import ChannelAccount
from nano_openclaw.gateway.context import GatewayContext


def _entry_to_dict(entry) -> dict[str, Any]:
    return {
        "channel_id": entry.channel_id,
        "account_id": entry.account_id,
        "state": entry.state,
        "error": entry.error,
        "started_at": entry.started_at,
    }


async def channels_status(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return live status for all running channel/account pairs.

    Pulls directly from ``ChannelRegistry`` rather than going through
    ``backend.channels_status`` because Phase 0 left the backend method
    as a placeholder; the registry is the source of truth in v1.
    """
    statuses = ctx.channel_registry.list_status()
    return {"channels": [_entry_to_dict(s) for s in statuses]}


async def channels_start(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(params.get("channel_id") or "")
    account_id = str(params.get("account_id") or "default")
    raw_config = params.get("config") or {}
    if not isinstance(raw_config, dict):
        raw_config = {}

    # Merge config from ``runtime.config.<channel_id>.accounts`` when caller
    # didn't supply one explicitly. v1: only wechat populated this way.
    if not raw_config and channel_id == "wechat":
        wechat_cfg = ctx.runtime.config.wechat
        for acc in wechat_cfg.accounts:
            if acc.id == account_id:
                raw_config = {
                    "ilink_token": acc.ilink_token,
                    "ilink_base_url": acc.ilink_base_url,
                    "notify_queue_path": acc.notify_queue_path,
                }
                break

    account = ChannelAccount(id=account_id, config=raw_config)
    instance = await ctx.channel_registry.start(channel_id, account, ctx.runtime)
    return _entry_to_dict(instance.status())


async def channels_stop(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(params.get("channel_id") or "")
    account_id = str(params.get("account_id") or "default")
    await ctx.channel_registry.stop(channel_id, account_id)
    return {
        "channel_id": channel_id,
        "account_id": account_id,
        "state": "stopped",
        "error": None,
        "started_at": None,
    }


HANDLERS = {
    "channels.status": channels_status,
    "channels.start": channels_start,
    "channels.stop": channels_stop,
}
