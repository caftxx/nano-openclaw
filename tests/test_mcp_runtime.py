"""Regression tests for MCP runtime transport handling."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType

from nano_openclaw.config.types import McpServerConfig
from nano_openclaw.mcp.runtime import McpRuntime


class _AsyncContextManager:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_streamable_http_uses_current_module_and_three_value_client(monkeypatch):
    runtime = McpRuntime()
    ready = threading.Event()
    cfg = McpServerConfig(
        url="https://example.test/mcp",
        transport="streamable-http",
        headers={"x-test": 123},
    )

    captured: dict[str, object] = {}

    def fake_streamablehttp_client(url, headers):
        captured["url"] = url
        captured["headers"] = headers
        return _AsyncContextManager(("reader", "writer", lambda: "session-1"))

    async def fake_manage_session(name, read, write, ready_event):
        captured["name"] = name
        captured["read"] = read
        captured["write"] = write
        ready_event.set()

    fake_module = ModuleType("mcp.client.streamable_http")
    fake_module.streamablehttp_client = fake_streamablehttp_client
    fake_module.streamable_http_client = fake_streamablehttp_client
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", fake_module)
    monkeypatch.delitem(sys.modules, "mcp.client.streamablehttp", raising=False)
    monkeypatch.setattr(runtime, "_manage_session", fake_manage_session)

    asyncio.run(runtime._run_streamable_http_server("demo", cfg, ready))

    assert captured == {
        "url": "https://example.test/mcp",
        "headers": {"x-test": "123"},
        "name": "demo",
        "read": "reader",
        "write": "writer",
    }
    assert ready.is_set()


def test_run_server_signals_ready_when_connection_fails(capsys, monkeypatch):
    runtime = McpRuntime()
    ready = threading.Event()
    cfg = McpServerConfig(command="missing-command", connectionTimeoutMs=500)

    async def fail_server(name, cfg, ready_event):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "_run_stdio_server", fail_server)

    asyncio.run(runtime._run_server("broken", cfg, ready))

    assert ready.is_set()
    assert "server 'broken' connection failed: boom" in capsys.readouterr().err
