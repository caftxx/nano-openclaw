"""Channel abstraction — IM/messaging integrations as first-class daemon plugins.

A "channel" is a kind of integration (wechat, telegram, slack, ...). Each
channel can have multiple "accounts" (a single ilink token, a single bot
deck, etc.). The gateway daemon hosts all enabled channels, each running as
an asyncio task sharing the same ``AgentRuntime``.

Mirrors openclaw's ``channels/plugins/*`` design — but in nano we keep the
channels package much smaller (no plugin SDK ceremony).
"""

from nano_openclaw.channels.base import Channel, ChannelAccount, ChannelStatus
from nano_openclaw.channels.registry import ChannelRegistry, get_channel_registry

__all__ = [
    "Channel",
    "ChannelAccount",
    "ChannelStatus",
    "ChannelRegistry",
    "get_channel_registry",
]
