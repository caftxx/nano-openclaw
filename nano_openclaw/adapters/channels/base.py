"""Channel adapter protocol re-export.

The service layer owns the protocol and lifecycle manager; concrete adapters
import these names here for local readability.
"""

from nano_openclaw.services.channels import (
    ChannelAccount,
    ChannelAdapter,
    ChannelContext,
    ChannelState,
    ChannelStatus,
)

__all__ = [
    "ChannelAccount",
    "ChannelAdapter",
    "ChannelContext",
    "ChannelState",
    "ChannelStatus",
]
