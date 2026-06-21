"""Regression tests for MCP runtime transport handling."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType

from nano_openclaw.config.types import McpServerConfig
from nano_openclaw.features.mcp.runtime import McpRuntime


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


def test_stdio_mcp_proxy_config_passes_command_args_and_env(monkeypatch):
    runtime = McpRuntime()
    ready = threading.Event()
    cfg = McpServerConfig(
        command="mcp-proxy",
        transport="stdio",
        args=[
            "--transport=streamablehttp",
            "--stateless",
            "http://ha.lan/api/mcp",
        ],
        env={"API_ACCESS_TOKEN": "abc"},
    )
    captured: dict[str, object] = {}

    def fake_stdio_client(params):
        captured["command"] = params.command
        captured["args"] = list(params.args)
        captured["env"] = dict(params.env or {})
        captured["cwd"] = params.cwd
        return _AsyncContextManager(("reader", "writer"))

    async def fake_manage_session(name, read, write, ready_event):
        captured["name"] = name
        captured["read"] = read
        captured["write"] = write
        ready_event.set()

    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(runtime, "_manage_session", fake_manage_session)

    asyncio.run(runtime._run_stdio_server("HomeAssistant", cfg, ready))

    assert captured == {
        "command": "mcp-proxy",
        "args": [
            "--transport=streamablehttp",
            "--stateless",
            "http://ha.lan/api/mcp",
        ],
        "env": {"API_ACCESS_TOKEN": "abc"},
        "cwd": None,
        "name": "HomeAssistant",
        "read": "reader",
        "write": "writer",
    }
    assert ready.is_set()


def test_run_server_signals_ready_when_connection_fails(caplog, monkeypatch):
    runtime = McpRuntime()
    ready = threading.Event()
    cfg = McpServerConfig(command="missing-command", connectionTimeoutMs=500)

    async def fail_server(name, cfg, ready_event):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "_run_stdio_server", fail_server)

    asyncio.run(runtime._run_server("broken", cfg, ready))

    assert ready.is_set()
    assert "server 'broken' connection failed: RuntimeError: boom" in caplog.text
    status = runtime.status_snapshot()
    assert status["failed"] == 1
    assert status["servers"][0]["name"] == "broken"
    assert status["servers"][0]["status"] == "failed"
    assert "boom" in status["servers"][0]["error"]


def test_initialize_records_timeout_as_failed_status(monkeypatch):
    runtime = McpRuntime()
    cfg = McpServerConfig(command="slow-command", connectionTimeoutMs=1)

    async def never_ready(name, cfg, ready_event):
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_run_stdio_server", never_ready)

    async def run():
        try:
            await runtime.initialize({"slow": cfg})
        finally:
            await runtime.close()

    asyncio.run(run())

    status = runtime.status_snapshot()
    assert status["failed"] == 1
    assert status["servers"][0]["name"] == "slow"
    assert status["servers"][0]["status"] == "failed"
    assert "timeout" in status["servers"][0]["error"]
