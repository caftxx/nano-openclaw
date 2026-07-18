"""Native xiaozhi-esp32 gateway adapter."""

from nano_openclaw.adapters.xiaozhi.channel import XiaozhiChannel
from nano_openclaw.adapters.xiaozhi.routes import register_xiaozhi_routes

__all__ = ["XiaozhiChannel", "register_xiaozhi_routes"]
