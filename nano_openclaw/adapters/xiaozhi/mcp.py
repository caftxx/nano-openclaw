"""MCP client over the xiaozhi control WebSocket."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from nano_openclaw.adapters.xiaozhi.protocol import mcp_envelope, sanitize_tool_name
from nano_openclaw.core.tools import Tool
from nano_openclaw.logger import get_logger


SendJson = Callable[[dict[str, Any]], Awaitable[None]]
log = get_logger(__name__)


@dataclass(frozen=True)
class DeviceTool:
    name: str
    description: str
    input_schema: dict[str, Any]


class DeviceMcpPeer:
    def __init__(
        self,
        *,
        session_id: str,
        send_json: SendJson,
        vision_url: str,
        token: str,
        timeout_ms: int,
    ) -> None:
        self.session_id = session_id
        self._send_json = send_json
        self._vision_url = vision_url
        self._token = token
        self._timeout = timeout_ms / 1000
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.tools: list[DeviceTool] = []
        self.ready = asyncio.Event()
        self._closed = False

    async def initialize(self) -> None:
        try:
            await self.request(
                "initialize",
                {
                    "capabilities": {
                        "vision": {"url": self._vision_url, "token": self._token},
                    }
                },
            )
            cursor = ""
            tools: list[DeviceTool] = []
            while True:
                # Camera and other explicitly user-triggered tools are marked
                # user_only by the firmware and are omitted unless requested.
                result = await self.request(
                    "tools/list", {"cursor": cursor, "withUserTools": True}
                )
                for item in result.get("tools") or []:
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    schema = item.get("inputSchema")
                    tools.append(DeviceTool(
                        name=str(item["name"]),
                        description=str(item.get("description") or ""),
                        input_schema=schema if isinstance(schema, dict) else {"type": "object"},
                    ))
                cursor = str(result.get("nextCursor") or "")
                if not cursor:
                    break
            self.tools = tools
        finally:
            self.ready.set()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("xiaozhi device disconnected")
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            await self._send_json(mcp_envelope(self.session_id, payload))
            response = await asyncio.wait_for(future, timeout=self._timeout)
        finally:
            self._pending.pop(request_id, None)
        error = response.get("error")
        if isinstance(error, dict):
            raise RuntimeError(str(error.get("message") or error))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def handle(self, payload: dict[str, Any]) -> bool:
        request_id = payload.get("id")
        if not isinstance(request_id, int):
            return False
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(payload)
        return True

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        log.info("xiaozhi.mcp.call", f"session={self.session_id} tool={name}")
        result = await self.request("tools/call", {"name": name, "arguments": args})
        texts: list[str] = []
        for content in result.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "text":
                texts.append(str(content.get("text") or ""))
        if result.get("isError"):
            log.warning("xiaozhi.mcp.call.error", f"session={self.session_id} tool={name}")
            return "Error: " + ("\n".join(texts) or "device tool failed")
        log.info("xiaozhi.mcp.call.done", f"session={self.session_id} tool={name}")
        return "\n".join(texts)

    def materialize_tools(self) -> list[Tool]:
        result: list[Tool] = []
        used: set[str] = set()
        for info in self.tools:
            tool_name = sanitize_tool_name(info.name)
            if tool_name in used:
                continue
            used.add(tool_name)

            def make_run(remote_name: str):
                async def run(args: dict[str, Any]) -> str:
                    return await self.call_tool(remote_name, args)
                return run

            result.append(Tool(
                name=tool_name,
                description=f"[Xiaozhi device] {info.description}",
                input_schema=info.input_schema,
                run=make_run(info.name),
            ))
        return result

    def close(self) -> None:
        self._closed = True
        self.ready.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("xiaozhi device disconnected"))
        self._pending.clear()


class XiaozhiHub:
    def __init__(self) -> None:
        self._connections: dict[str, Any] = {}

    def add(self, device_id: str, connection: Any) -> Any | None:
        previous = self._connections.get(device_id)
        self._connections[device_id] = connection
        return previous

    def remove(self, device_id: str, connection: Any) -> None:
        if self._connections.get(device_id) is connection:
            self._connections.pop(device_id, None)

    def get(self, device_id: str) -> Any | None:
        return self._connections.get(device_id)

    def all(self) -> list[Any]:
        return list(self._connections.values())
