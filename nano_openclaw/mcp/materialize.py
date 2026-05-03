"""Materialize MCP tools as nano-openclaw Tool objects.

Mirrors openclaw's pi-bundle-mcp-materialize.ts:
- Converts McpToolInfo to Tool objects
- Wraps runtime.call_tool() as sync run function
- Names: {server}__{tool} format (safe sanitization)
- Description prefix: [MCP:server_name]
"""

import re
from typing import Any, Dict, List, Set

from nano_openclaw.mcp.runtime import McpRuntime, McpToolInfo
from nano_openclaw.tools import Tool


def materialize_mcp_tools(
    runtime: McpRuntime,
    existing_names: Set[str],
) -> List[Tool]:
    """Convert MCP tools to nano-openclaw Tool objects.
    
    Args:
        runtime: McpRuntime instance with connected servers
        existing_names: Set of already-registered tool names (avoid conflicts)
    
    Returns:
        List of Tool objects ready for registry.register()
    """
    tools: List[Tool] = []
    
    for info in runtime.get_mcp_tools():
        safe_server = re.sub(r'[^a-zA-Z0-9_]', '_', info.server_name)
        safe_tool = re.sub(r'[^a-zA-Z0-9_]', '_', info.tool_name)
        full_name = f"{safe_server}__{safe_tool}"
        
        if full_name in existing_names:
            continue
            
        description = f"[MCP:{info.server_name}] {info.description}"
        
        def make_run(server_name: str, tool_name: str):
            def run(args: Dict[str, Any]) -> str:
                return runtime.call_tool(server_name, tool_name, args)
            return run
            
        tool = Tool(
            name=full_name,
            description=description,
            input_schema=info.input_schema,
            run=make_run(info.server_name, info.tool_name),
        )
        tools.append(tool)
        
    return tools