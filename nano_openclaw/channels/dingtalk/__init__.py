"""DingTalk channel — wraps the DingTalk Stream client as a ``Channel``.

Importing this module registers ``DingtalkChannel`` with the global
``ChannelRegistry``. The daemon imports this at startup so
``channels.start("dingtalk", account, runtime)`` resolves.
"""

from nano_openclaw.channels.dingtalk.channel import DingtalkChannel
from nano_openclaw.channels.registry import get_channel_registry

get_channel_registry().register(DingtalkChannel)

__all__ = ["DingtalkChannel"]
