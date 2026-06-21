"""Regression tests for MCP runtime transport handling."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType

from nano_openclaw.config.types import McpServerConfig
from nano_openclaw.features.mcp.runtime import (
    McpRuntime,
    _simple_failure_reason,
    _stdio_env,
    _summarize_stdio_stderr,
)


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

    def fake_stdio_client(params, errlog=None):
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

    assert captured["command"] == "mcp-proxy"
    assert captured["args"] == [
        "--transport=streamablehttp",
        "--stateless",
        "http://ha.lan/api/mcp",
    ]
    assert captured["env"]["API_ACCESS_TOKEN"] == "abc"
    assert "PATH" in captured["env"]
    assert captured["cwd"] is None
    assert captured["name"] == "HomeAssistant"
    assert captured["read"] == "reader"
    assert captured["write"] == "writer"
    assert ready.is_set()


def test_stdio_env_adds_user_bin_path(monkeypatch, tmp_path):
    user_bin = tmp_path / ".local" / "bin"
    user_bin.mkdir(parents=True)

    import nano_openclaw.features.mcp.runtime as runtime_module

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(runtime_module, "_stdio_user_bin_dirs", lambda: [str(user_bin)])

    env = _stdio_env({"API_ACCESS_TOKEN": "abc"})

    assert env["API_ACCESS_TOKEN"] == "abc"
    assert env["PATH"].split(runtime_module.os.pathsep)[:2] == [str(user_bin), "/usr/bin"]


def test_simple_failure_reason_unwraps_exception_group():
    reason = _simple_failure_reason(ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [RuntimeError("connection refused")],
    ))

    assert "ExceptionGroup" in reason
    assert "RuntimeError: connection refused" in reason
    assert "unhandled errors in a TaskGroup" not in reason


def test_summarize_stdio_stderr_extracts_http_status_error():
    text = (
        "Traceback ... response.raise_for_status() | "
        "httpx.HTTPStatusError: Client error '403 Forbidden' "
        "for url 'http://ha.lan/api/mcp' For more information check: https://example.test"
    )

    assert _summarize_stdio_stderr(text) == (
        "httpx.HTTPStatusError: Client error '403 Forbidden' "
        "for url 'http://ha.lan/api/mcp'"
    )


def test_stdio_failure_includes_child_exception_and_stderr(monkeypatch):
    runtime = McpRuntime()
    ready = threading.Event()
    cfg = McpServerConfig(command="mcp-proxy", args=["http://ha.lan/api/mcp"])

    class FailingContext:
        async def __aenter__(self):
            return ("reader", "writer")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    def fake_stdio_client(params, errlog=None):
        if errlog is not None:
            errlog.write("401 Unauthorized from Home Assistant\n")
        return FailingContext()

    async def fake_manage_session(name, read, write, ready_event):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("initialize failed")])

    import mcp.client.stdio as stdio_module

    monkeypatch.setattr(stdio_module, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(runtime, "_manage_session", fake_manage_session)

    asyncio.run(runtime._run_server("Home Assistant", cfg, ready))

    status = runtime.status_snapshot()
    error = status["servers"][0]["error"]
    assert ready.is_set()
    assert "RuntimeError: initialize failed" in error
    assert "stderr: 401 Unauthorized from Home Assistant" in error


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
