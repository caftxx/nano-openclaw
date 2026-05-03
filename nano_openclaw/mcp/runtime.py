"""MCP Runtime implementation.

Mirrors openclaw's SessionMcpRuntime:
- Persistent MCP server connections in background asyncio thread
- run_coroutine_threadsafe bridge for sync->async calls
- Supports stdio, SSE, and streamable-http transports
"""

import asyncio
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from nano_openclaw.config.types import McpServerConfig


@dataclass
class McpToolInfo:
    """MCP tool information."""
    server_name: str
    tool_name: str
    description: str
    input_schema: Dict[str, Any]


class McpRuntime:
    """MCP runtime managing persistent connections to MCP servers.
    
    Design:
    - Background asyncio thread holds ClientSession context managers open
    - run_coroutine_threadsafe bridges sync calls to async context
    - initialize() blocks until all servers ready or timeout
    - call_tool() is sync wrapper for async tool execution
    - close() signals shutdown and waits for thread cleanup
    """
    
    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._sessions: Dict[str, Any] = {}
        self._tool_infos: List[McpToolInfo] = []
        self._shutdown = threading.Event()
        self._ready_events: Dict[str, threading.Event] = {}
        
    def initialize(self, servers: Dict[str, McpServerConfig]) -> None:
        """Initialize connections to all configured MCP servers.
        
        Blocks until all servers are ready or timeout (default 10s per server).
        Failed servers are skipped without blocking others.
        """
        if not servers:
            return
            
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
        )
        self._thread.start()
        
        for name, cfg in servers.items():
            ready = threading.Event()
            self._ready_events[name] = ready
            
            future = asyncio.run_coroutine_threadsafe(
                self._run_server(name, cfg, ready),
                self._loop,
            )
            
            timeout_ms = cfg.connectionTimeoutMs or 10000
            if not ready.wait(timeout_ms / 1000):
                print(
                    f"MCP: server '{name}' connection timeout after {timeout_ms}ms, skipping",
                    file=sys.stderr,
                )
                
    def _run_loop(self) -> None:
        """Run the asyncio event loop in background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        
    async def _run_server(
        self,
        name: str,
        cfg: McpServerConfig,
        ready: threading.Event,
    ) -> None:
        """Run a single MCP server connection persistently.
        
        Keeps ClientSession context manager open until shutdown signal.
        """
        try:
            if cfg.command:
                await self._run_stdio_server(name, cfg, ready)
            elif cfg.transport == "streamable-http" and cfg.url:
                await self._run_streamable_http_server(name, cfg, ready)
            elif cfg.url:
                await self._run_sse_server(name, cfg, ready)
            else:
                print(
                    f"MCP: server '{name}' has no valid transport config, skipping",
                    file=sys.stderr,
                )
                ready.set()
        except Exception as e:
            print(
                f"MCP: server '{name}' connection failed: {e}",
                file=sys.stderr,
            )
            ready.set()
            
    async def _run_stdio_server(
        self,
        name: str,
        cfg: McpServerConfig,
        ready: threading.Event,
    ) -> None:
        """Run stdio transport MCP server."""
        from mcp.client.stdio import stdio_client, StdioServerParameters
        
        env = {}
        if cfg.env:
            for k, v in cfg.env.items():
                env[k] = str(v)
                
        cwd = cfg.cwd or cfg.workingDirectory
        
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args or [],
            env=env,
            cwd=cwd,
        )
        
        async with stdio_client(params) as (read, write):
            await self._manage_session(name, read, write, ready)
            
    async def _run_sse_server(
        self,
        name: str,
        cfg: McpServerConfig,
        ready: threading.Event,
    ) -> None:
        """Run SSE transport MCP server."""
        from mcp.client.sse import sse_client
        
        headers = {}
        if cfg.headers:
            for k, v in cfg.headers.items():
                headers[k] = str(v)
                
        async with sse_client(cfg.url, headers) as (read, write):
            await self._manage_session(name, read, write, ready)
            
    async def _run_streamable_http_server(
        self,
        name: str,
        cfg: McpServerConfig,
        ready: threading.Event,
    ) -> None:
        """Run streamable-http transport MCP server."""
        from mcp.client.streamable_http import streamablehttp_client
        
        headers = {}
        if cfg.headers:
            for k, v in cfg.headers.items():
                headers[k] = str(v)
                
        async with streamablehttp_client(cfg.url, headers) as (read, write, _get_session_id):
            await self._manage_session(name, read, write, ready)
            
    async def _manage_session(
        self,
        name: str,
        read: Any,
        write: Any,
        ready: threading.Event,
    ) -> None:
        """Manage ClientSession lifecycle for a server.
        
        Opens session, lists tools, signals ready, then waits for shutdown.
        """
        from mcp.client.session import ClientSession
        
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            tools_result = await session.list_tools()
            for tool in tools_result.tools:
                self._tool_infos.append(McpToolInfo(
                    server_name=name,
                    tool_name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema or {},
                ))
                
            self._sessions[name] = session
            ready.set()
            
            while not self._shutdown.is_set():
                await asyncio.sleep(0.5)
                
    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: Dict[str, Any],
    ) -> str:
        """Call a MCP tool synchronously.
        
        Returns tool result as text.
        """
        if server_name not in self._sessions:
            return f"Error: server '{server_name}' not connected"
            
        session = self._sessions[server_name]
        
        async def _call():
            result = await session.call_tool(tool_name, args)
            if result.isError:
                return f"Error: {result.content}"
            texts = []
            for content in result.content:
                if content.type == "text":
                    texts.append(content.text)
            return "\n".join(texts) if texts else ""
            
        future = asyncio.run_coroutine_threadsafe(_call(), self._loop)
        return future.result(timeout=30)
        
    def get_mcp_tools(self) -> List[McpToolInfo]:
        """Get list of all MCP tools from connected servers."""
        return self._tool_infos
        
    def close(self) -> None:
        """Shutdown all MCP connections and cleanup."""
        if not self._thread:
            return
            
        self._shutdown.set()
        
        for name, session in self._sessions.items():
            try:
                async def _close(s):
                    await s.__aexit__(None, None, None)
                asyncio.run_coroutine_threadsafe(_close(session), self._loop)
            except Exception:
                pass
                
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        
        self._sessions.clear()
        self._tool_infos.clear()
        self._loop = None
        self._thread = None
