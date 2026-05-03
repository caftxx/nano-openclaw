"""MCP Runtime implementation.

Mirrors openclaw's SessionMcpRuntime — persistent MCP server connections.
Runs entirely within the main asyncio event loop (no background thread).

Transports supported: stdio, SSE, streamable-http.
"""

import asyncio
import sys
from dataclasses import dataclass, field
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

    Design (async-native):
    - initialize() launches one asyncio.Task per server; each task keeps the
      ClientSession context manager open until cancelled.
    - call_tool() awaits the session directly — no thread bridges needed.
    - close() cancels all server tasks and waits for cleanup.
    """

    def __init__(self):
        self._sessions: Dict[str, Any] = {}
        self._tool_infos: List[McpToolInfo] = []
        self._server_tasks: Dict[str, asyncio.Task] = {}
        self._ready_events: Dict[str, asyncio.Event] = {}

    async def initialize(self, servers: Dict[str, McpServerConfig]) -> None:
        """Initialize connections to all configured MCP servers.

        Waits until each server signals ready (or times out).
        Failed servers are skipped without blocking others.
        """
        if not servers:
            return

        for name, cfg in servers.items():
            ready = asyncio.Event()
            self._ready_events[name] = ready
            task = asyncio.create_task(
                self._run_server(name, cfg, ready),
                name=f"mcp-{name}",
            )
            self._server_tasks[name] = task

        for name, cfg in servers.items():
            timeout_ms = cfg.connectionTimeoutMs or 10000
            ready = self._ready_events[name]
            try:
                await asyncio.wait_for(ready.wait(), timeout=timeout_ms / 1000)
            except asyncio.TimeoutError:
                print(
                    f"MCP: server '{name}' connection timeout after {timeout_ms}ms, skipping",
                    file=sys.stderr,
                )

    async def _run_server(
        self,
        name: str,
        cfg: McpServerConfig,
        ready: asyncio.Event,
    ) -> None:
        """Run a single MCP server connection persistently."""
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
        except asyncio.CancelledError:
            raise
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
        ready: asyncio.Event,
    ) -> None:
        from mcp.client.stdio import stdio_client, StdioServerParameters

        env = {k: str(v) for k, v in (cfg.env or {}).items()}
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
        ready: asyncio.Event,
    ) -> None:
        from mcp.client.sse import sse_client

        headers = {k: str(v) for k, v in (cfg.headers or {}).items()}

        async with sse_client(cfg.url, headers) as (read, write):
            await self._manage_session(name, read, write, ready)

    async def _run_streamable_http_server(
        self,
        name: str,
        cfg: McpServerConfig,
        ready: asyncio.Event,
    ) -> None:
        from mcp.client.streamable_http import streamablehttp_client

        headers = {k: str(v) for k, v in (cfg.headers or {}).items()}

        async with streamablehttp_client(cfg.url, headers) as (read, write, _):
            await self._manage_session(name, read, write, ready)

    async def _manage_session(
        self,
        name: str,
        read: Any,
        write: Any,
        ready: asyncio.Event,
    ) -> None:
        """Open a ClientSession, enumerate tools, signal ready, hold open."""
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

            # Hold session open until this task is cancelled.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: Dict[str, Any],
    ) -> str:
        """Call a MCP tool. Returns tool result as text."""
        if server_name not in self._sessions:
            return f"Error: server '{server_name}' not connected"

        session = self._sessions[server_name]
        result = await session.call_tool(tool_name, args)
        if result.isError:
            return f"Error: {result.content}"
        texts = [c.text for c in result.content if c.type == "text"]
        return "\n".join(texts)

    def get_mcp_tools(self) -> List[McpToolInfo]:
        """Get list of all MCP tools from connected servers."""
        return self._tool_infos

    async def close(self) -> None:
        """Cancel all server tasks and clean up."""
        for task in self._server_tasks.values():
            task.cancel()

        if self._server_tasks:
            await asyncio.gather(*self._server_tasks.values(), return_exceptions=True)

        self._sessions.clear()
        self._tool_infos.clear()
        self._server_tasks.clear()
        self._ready_events.clear()
