"""Built-in MCP plugin."""

import logging
from typing import Any

from nano_openclaw.plugins.types import PluginApi

logger = logging.getLogger(__name__)


class McpPlugin:
    id = "nano-mcp"
    name = "MCP"

    def register(self, api: PluginApi) -> None:
        if not api.config.mcp.servers:
            return

        initialized = False
        runtime = None

        async def initialize_mcp(_payload: dict[str, Any]) -> None:
            nonlocal initialized, runtime
            if initialized:
                return None
            initialized = True

            from nano_openclaw.mcp.materialize import materialize_mcp_tools
            from nano_openclaw.mcp.runtime import McpRuntime

            runtime = McpRuntime()
            await runtime.initialize(api.config.mcp.servers)
            mcp_tools = materialize_mcp_tools(runtime, existing_names=set(api.tool_names()))
            for tool in mcp_tools:
                api.register_tool(tool)
            logger.info(
                "MCP: loaded %d tools from %d server(s)",
                len(mcp_tools),
                len(api.config.mcp.servers),
            )
            return None

        async def close_mcp(_payload: dict[str, Any]) -> None:
            if runtime is not None:
                await runtime.close()
            return None

        api.register_hook("session_start", initialize_mcp, priority=0)
        api.register_hook("session_end", close_mcp, priority=0)
