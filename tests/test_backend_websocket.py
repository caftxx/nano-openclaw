"""WebSocketBackend round-trip tests.

Strategy: spin up a small uvicorn server with the real ``register_ws_route``
mounted on a ``GatewayContext``, then point WebSocketBackend at it and
verify the Backend Protocol round-trips correctly over the wire.

We don't reuse FastAPI TestClient here — its WebSocket path is sync-blocking,
which doesn't compose with WebSocketBackend's asyncio receive loop. A real
uvicorn on a free localhost port keeps the test client and server isolated
on different event loops.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from nano_openclaw.services.channels import ChannelManager
from nano_openclaw.services.backend import (
    Backend,
    BackendError,
    BusyError,
    NotFoundError,
)
from nano_openclaw.services.backend_embedded import EmbeddedBackend
from nano_openclaw.api.backend_websocket import WebSocketBackend
from nano_openclaw.api.context import GatewayContext
from nano_openclaw.api.ws_route import register_ws_route
from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.core.tools import ToolRegistry
from nano_openclaw.plugins.registry import HookRegistry


# ────────────────────────────────────────────────────────────────────────────
# Test harness — uvicorn server in the same event loop as the test
# ────────────────────────────────────────────────────────────────────────────


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _fake_runtime(tmp_path: Path) -> SimpleNamespace:
    from nano_openclaw.services.runs import RunRegistry
    from nano_openclaw.services.runtime_update import RuntimeUpdateGuard
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    cfg = LoopConfig(model="test-model", workspace_dir=workspace, session_key="default")
    return SimpleNamespace(
        agent_id="default",
        session_id="default",
        config=SimpleNamespace(),
        warnings=[],
        client=None,
        registry=ToolRegistry(),
        cfg=cfg,
        hook_registry=None,
        state_dir=state,
        session_dir=sd,
        store_path=tmp_path / "sessions.json",
        workspace_dir=workspace,
        model_ref="test/test-model",
        model_id="test-model",
        image_model_ref=None,
        run_registry=RunRegistry(),
        runtime_guard=RuntimeUpdateGuard(),
    )


class _RunningServer:
    """A uvicorn server running in-process for a single test.

    Holds the FastAPI app, the embedded backend, and the uvicorn task so the
    test can ``await stop()`` cleanly.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.runtime = _fake_runtime(tmp_path)
        self.backend = EmbeddedBackend(self.runtime)
        self.ctx = GatewayContext(
            runtime=self.runtime,
            backend=self.backend,
            channel_manager=ChannelManager(),
        )
        self.app = FastAPI()
        register_ws_route(self.app, self.ctx)
        self.port = _free_port()
        self._server: Any = None
        self._serve_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        import uvicorn
        config = uvicorn.Config(
            app=self.app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            access_log=False,
            lifespan="on",
            ws="websockets-sansio",
        )
        self._server = uvicorn.Server(config)
        self._serve_task = asyncio.create_task(self._server.serve(), name="test-uvicorn")
        # Wait until uvicorn reports started
        for _ in range(50):
            if getattr(self._server, "started", False):
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("uvicorn server did not start within 2.5s")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            try:
                await asyncio.wait_for(self._serve_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._serve_task.cancel()
        await self.backend.aclose()


# ────────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────────


def test_websocket_backend_satisfies_protocol():
    ws = WebSocketBackend("ws://127.0.0.1:65535/rpc")
    assert isinstance(ws, Backend)


def test_health_round_trip(tmp_path: Path):
    async def run():
        server = _RunningServer(tmp_path)
        await server.start()
        client = WebSocketBackend(f"ws://127.0.0.1:{server.port}/rpc")
        try:
            await client.aopen()
            health = await client.health()
            assert health.runtime_ready is True
            assert health.in_flight_turns == 0
        finally:
            await client.aclose()
            await server.stop()

    asyncio.run(run())


def test_runtime_get_round_trip(tmp_path: Path):
    async def run():
        server = _RunningServer(tmp_path)
        await server.start()
        client = WebSocketBackend(f"ws://127.0.0.1:{server.port}/rpc")
        try:
            await client.aopen()
            snap = await client.runtime_get()
            assert snap.agent_id == "default"
            assert snap.model_id == "test-model"
        finally:
            await client.aclose()
            await server.stop()

    asyncio.run(run())


def test_models_list_round_trip(tmp_path: Path):
    async def run():
        server = _RunningServer(tmp_path)
        await server.start()
        client = WebSocketBackend(f"ws://127.0.0.1:{server.port}/rpc")
        try:
            await client.aopen()
            models = await client.models_list()
            assert any(m.id == "test-model" for m in models)
        finally:
            await client.aclose()
            await server.stop()

    asyncio.run(run())


def test_plugin_slash_runs_on_daemon_over_websocket(tmp_path: Path):
    async def plugin_slash(_backend, renderer, _state, _args, _cmd):
        renderer.text("daemon plugin slash ok")

    async def run():
        server = _RunningServer(tmp_path)
        hooks = HookRegistry()
        hooks.register_slash("/plugin-remote", plugin_slash, "Remote plugin")
        server.runtime.hook_registry = hooks
        await server.start()
        client = WebSocketBackend(f"ws://127.0.0.1:{server.port}/rpc")
        try:
            await client.aopen()
            result = await client.slash_run("/plugin-remote", session_key="s1")
            assert result.handled is True
            assert result.session_key == "s1"
            assert "daemon plugin slash ok" in result.text
        finally:
            await client.aclose()
            await server.stop()

    asyncio.run(run())


def test_busy_error_propagates_over_wire(tmp_path: Path):
    """Daemon's BusyError → wire BUSY code → WebSocketBackend re-raises."""
    async def run():
        server = _RunningServer(tmp_path)
        await server.start()
        client = WebSocketBackend(f"ws://127.0.0.1:{server.port}/rpc")
        try:
            await client.aopen()
            # Pre-acquire a session lock on the server side
            session = server.backend.manager.create()
            await session.lock.acquire()
            session.active_turn_id = "fake"
            try:
                with pytest.raises(BusyError) as exc_info:
                    await client.chat_send(session_key=session.session_id, text="x")
                assert exc_info.value.retry_after_ms == 500
                assert exc_info.value.details.get("session_id") == session.session_id
            finally:
                session.lock.release()
                session.active_turn_id = None
        finally:
            await client.aclose()
            await server.stop()

    asyncio.run(run())


def test_not_found_error_propagates(tmp_path: Path):
    async def run():
        server = _RunningServer(tmp_path)
        await server.start()
        client = WebSocketBackend(f"ws://127.0.0.1:{server.port}/rpc")
        try:
            await client.aopen()
            with pytest.raises(NotFoundError):
                await client.approvals_respond("unknown-id", allow=True)
        finally:
            await client.aclose()
            await server.stop()

    asyncio.run(run())


def test_unknown_method_raises_backend_error(tmp_path: Path):
    """Force a wire-level UNKNOWN_METHOD by calling _call directly."""
    async def run():
        server = _RunningServer(tmp_path)
        await server.start()
        client = WebSocketBackend(f"ws://127.0.0.1:{server.port}/rpc")
        try:
            await client.aopen()
            with pytest.raises(BackendError) as exc_info:
                await client._call("does.not.exist", {})
            assert "UNKNOWN_METHOD" in str(exc_info.value)
        finally:
            await client.aclose()
            await server.stop()

    asyncio.run(run())


def test_push_event_received_via_subscribe(tmp_path: Path):
    """sessions.reset emits session.changed — verify the subscriber pulls it."""
    async def run():
        server = _RunningServer(tmp_path)
        await server.start()
        client = WebSocketBackend(f"ws://127.0.0.1:{server.port}/rpc")
        try:
            await client.aopen()
            sub = client.subscribe(events=["session.changed"])

            # Trigger a session.changed push
            info = await client.sessions_reset("any-key", reason="new")
            assert info.session_id

            # Wait for the corresponding push event (with a small budget)
            received = None

            async def reader():
                nonlocal received
                async for evt in sub:
                    received = evt
                    return

            await asyncio.wait_for(reader(), timeout=2.0)
            assert received is not None
            assert received.event == "session.changed"
        finally:
            await client.aclose()
            await server.stop()

    asyncio.run(run())


def test_aclose_unblocks_pending_calls(tmp_path: Path):
    """A request that's still awaiting must wake with BackendError on aclose."""
    async def run():
        server = _RunningServer(tmp_path)
        await server.start()
        client = WebSocketBackend(f"ws://127.0.0.1:{server.port}/rpc", request_timeout=0.5)
        await client.aopen()

        # Stop the server first — pending request should timeout / lose the conn
        await server.stop()

        with pytest.raises(BackendError):
            await client.health()

        await client.aclose()

    asyncio.run(run())
