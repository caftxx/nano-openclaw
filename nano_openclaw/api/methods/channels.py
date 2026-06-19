"""channels.* RPC handlers — status / start / stop."""

from __future__ import annotations

from typing import Any

from nano_openclaw.adapters.channels.base import ChannelAccount
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
    """Return live status for all running channel/account pairs.

    Pulls directly from ``ChannelManager`` rather than going through
    ``backend.channels_status`` because Phase 0 left the backend method
    as a placeholder; the registry is the source of truth in v1.
    """
    statuses = ctx.channel_manager.list_status()
    return {"channels": [_entry_to_dict(s) for s in statuses]}


async def channels_start(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(params.get("channel_id") or "")
    account_id = str(params.get("account_id") or "default")
    raw_config = params.get("config") or {}
    if not isinstance(raw_config, dict):
        raw_config = {}

    # For wechat: caller may pass ``config={}`` and let WechatChannel.start()
    # load the token + base_url from ``state_dir/wechat-tokens.{id}.json``
    # (written by ``nano-openclaw wechat login``). There's no config-file
    # fallback any more — login is the single source of truth.
    account = ChannelAccount(id=account_id, config=raw_config)
    # Pass ``ctx`` as gateway so channels (e.g. WechatChannel) can wire
    # ``backend`` into their bot. Without this, WechatBot would fall back to
    # its hand-rolled slash handler — drift from TUI/WebUI ``/help``.
    instance = await ctx.channel_manager.start(channel_id, account, ctx.runtime, ctx)
    return _entry_to_dict(instance.status())


async def channels_stop(ctx: GatewayContext, params: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(params.get("channel_id") or "")
    account_id = str(params.get("account_id") or "default")
    await ctx.channel_manager.stop(channel_id, account_id)
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
