"""WeChat channel — wraps the legacy ``WechatBot`` runner as a ``Channel``.

Importing this module registers ``WechatChannel`` with the global
``ChannelRegistry``. The daemon (Phase 3) imports this once at startup so
``channels.start("wechat", account)`` resolves.
"""

from nano_openclaw.channels.registry import get_channel_registry
from nano_openclaw.channels.wechat.channel import WechatChannel

# Register at import time. Idempotent — re-imports are safe.
get_channel_registry().register(WechatChannel)

__all__ = ["WechatChannel"]
