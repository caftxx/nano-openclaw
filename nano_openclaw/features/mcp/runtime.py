"""MCP Runtime implementation.

Mirrors openclaw's SessionMcpRuntime — persistent MCP server connections.
Runs entirely within the main asyncio event loop (no background thread).

Transports supported: stdio, SSE, streamable-http.
"""

import asyncio
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, TextIO

from nano_openclaw.config.types import McpServerConfig
from nano_openclaw.logger import get_logger

log = get_logger(__name__)

HTTP_STATUS_ERROR_RE = re.compile(
    r"(httpx\.HTTPStatusError: .*? for url '[^']+')"
)


@dataclass
class McpToolInfo:
    """MCP tool information."""
    server_name: str
    tool_name: str
    description: str
    input_schema: Dict[str, Any]


def _simple_failure_reason(exc: BaseException | str) -> str:
    if isinstance(exc, BaseExceptionGroup):
        child_reasons = [
            _simple_failure_reason(child)
            for child in exc.exceptions
        ]
        text = "; ".join(reason for reason in child_reasons if reason)
        if text:
            text = f"{type(exc).__name__}: {text}"
            return text[:797].rstrip() + "..." if len(text) > 800 else text

    text = str(exc).strip() if not isinstance(exc, str) else exc.strip()
    if not text:
        text = type(exc).__name__ if not isinstance(exc, str) else "unknown error"
    text = " ".join(text.splitlines()).strip()
    if len(text) > 800:
        text = text[:797].rstrip() + "..."
    if not isinstance(exc, str) and type(exc).__name__ not in text:
        text = f"{type(exc).__name__}: {text}"
    return text


def _append_stdio_stderr(exc: BaseException, errlog: TextIO) -> RuntimeError:
    reason = _simple_failure_reason(exc)
    try:
        errlog.seek(0)
        stderr_raw = errlog.read()
    except Exception:
        stderr_raw = ""
    stderr = _summarize_stdio_stderr(stderr_raw)
    if stderr:
        if len(stderr) > 700:
            stderr = stderr[-700:].lstrip()
        reason = f"{reason}; stderr: {stderr}"
    return RuntimeError(reason)


def _summarize_stdio_stderr(stderr_raw: str) -> str:
    stderr = " ".join(stderr_raw.split()).strip()
    if not stderr:
        return ""
    match = HTTP_STATUS_ERROR_RE.search(stderr)
    if match:
        return match.group(1)
    return stderr


def _stdio_user_bin_dirs() -> list[str]:
    if os.name == "nt":
        return []
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".local", "bin"),
        os.path.join(home, "bin"),
    ]


def _augment_stdio_path(path: str) -> str:
    parts = [part for part in path.split(os.pathsep) if part]
    additions = [
        part
        for part in _stdio_user_bin_dirs()
        if os.path.isdir(part) and part not in parts
    ]
    return os.pathsep.join([*additions, *parts])


def _stdio_env(config_env: dict[str, Any] | None) -> dict[str, str]:
    env = {k: str(v) for k, v in (config_env or {}).items()}
    path = env.get("PATH") or os.environ.get("PATH", "")
    augmented_path = _augment_stdio_path(path)
    if augmented_path:
        env["PATH"] = augmented_path
    return env


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
        self._server_status: Dict[str, dict[str, Any]] = {}

    async def initialize(self, servers: Dict[str, McpServerConfig]) -> None:
        """Initialize connections to all configured MCP servers.

        Waits until each server signals ready (or times out).
        Failed servers are skipped without blocking others.
        """
        if not servers:
            return

        for name, cfg in servers.items():
            self._server_status[name] = {
                "name": name,
                "transport": self._transport_label(cfg),
                "status": "starting",
                "tools": 0,
                "error": "",
            }
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
                reason = f"connection timeout after {timeout_ms}ms"
                self._mark_failed(name, reason)
                task = self._server_tasks.get(name)
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                log.warning("mcp.server.timeout", f"server '{name}' {reason}, skipping")

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
                reason = "no valid transport config"
                self._mark_failed(name, reason)
                print(f"MCP: server '{name}' {reason}, skipping", file=sys.stderr)
                ready.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = _simple_failure_reason(e)
            self._mark_failed(name, reason)
            log.warning("mcp.server.error", f"server '{name}' connection failed: {reason}")
            ready.set()

    async def _run_stdio_server(
        self,
        name: str,
        cfg: McpServerConfig,
        ready: asyncio.Event,
    ) -> None:
        from mcp.client.stdio import stdio_client, StdioServerParameters

        env = _stdio_env(cfg.env)
        cwd = cfg.cwd or cfg.workingDirectory

        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args or [],
            env=env,
            cwd=cwd,
        )

        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errlog:
            try:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    await self._manage_session(name, read, write, ready)
            except Exception as exc:
                raise _append_stdio_stderr(exc, errlog) from exc

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
            tool_count = 0
            for tool in tools_result.tools:
                tool_count += 1
                self._tool_infos.append(McpToolInfo(
                    server_name=name,
                    tool_name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema or {},
                ))

            self._sessions[name] = session
            self._server_status[name] = {
                **self._server_status.get(name, {"name": name, "transport": "unknown"}),
                "status": "connected",
                "tools": tool_count,
                "error": "",
            }
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

    def status_snapshot(self) -> dict[str, Any]:
        """Return a lightweight, serializable MCP status snapshot."""
        servers = [
            dict(self._server_status[name])
            for name in sorted(self._server_status)
        ]
        return {
            "configured": bool(servers),
            "initialized": bool(self._server_tasks or servers),
            "servers": servers,
            "connected": sum(1 for s in servers if s.get("status") == "connected"),
            "failed": sum(1 for s in servers if s.get("status") == "failed"),
            "starting": sum(1 for s in servers if s.get("status") == "starting"),
            "total_tools": len(self._tool_infos),
        }

    def _mark_failed(self, name: str, reason: BaseException | str) -> None:
        self._server_status[name] = {
            **self._server_status.get(name, {"name": name, "transport": "unknown"}),
            "status": "failed",
            "tools": 0,
            "error": _simple_failure_reason(reason),
        }

    @staticmethod
    def _transport_label(cfg: McpServerConfig) -> str:
        if cfg.command:
            return "stdio"
        if cfg.transport == "streamable-http" and cfg.url:
            return "streamable-http"
        if cfg.url:
            return cfg.transport or "sse"
        return "unknown"

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
