"""Smoke tests for EmbeddedBackend — interface + queue/pubsub path.

End-to-end tests with a real AgentRuntime + LLM live in test_webui.py and
similar; this file just validates the new abstraction's shape without
requiring API keys. Uses the project-wide ``asyncio.run`` pattern instead
of pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nano_openclaw.services.backend import (
    Backend,
    BusyError,
    NotFoundError,
    PushEvent,
)
from nano_openclaw.services.backend_embedded import (
    EmbeddedBackend,
    SUBSCRIBER_QUEUE_MAX,
)
from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.core.tools import Tool, ToolRegistry
from nano_openclaw.adapters.channels.base import ChannelAdapter, ChannelAccount
from nano_openclaw.services.channels import ChannelExitRequest, ChannelManager


class _RecordingChannel(ChannelAdapter):
    id = "recording"

    async def start(self, ctx):
        self._state = "running"
        self._started_at = time.time()
        self.gateway = ctx.gateway
        self.exits = []

    async def stop(self):
        self._state = "stopped"
        self._started_at = None

    async def exit_interaction(self, *, sender_key, reason=""):
        self.exits.append({"sender_key": sender_key, "reason": reason})


class _DecoratingChannel(_RecordingChannel):
    id = "decorating"

    def decorate_tools(self, base, sender_key: str):
        clone = base.clone()
        clone.register(Tool(
            name="sender_key",
            description="sender",
            input_schema={"type": "object", "properties": {}},
            run=lambda _args: sender_key,
        ))
        return clone


class _FailingStopChannel(ChannelAdapter):
    id = "failing-stop"

    async def start(self, ctx):
        self._state = "running"
        self._started_at = time.time()

    async def stop(self):
        raise RuntimeError("stop failed")


def _fake_runtime(tmp_path: Path) -> SimpleNamespace:
    """Minimal AgentRuntime stand-in. Only the fields EmbeddedBackend reads at
    construction + for sessions_get / health / models_list / runtime_get.
    """
    from nano_openclaw.services.runs import RunRegistry
    from nano_openclaw.services.runtime_update import RuntimeUpdateGuard

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    store_path = tmp_path / "sessions.json"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    cfg = LoopConfig(model="test-model", workspace_dir=workspace_dir, session_key="default")
    registry = ToolRegistry()

    return SimpleNamespace(
        agent_id="default",
        session_id="default",
        config=None,
        warnings=[],
        client=None,
        registry=registry,
        cfg=cfg,
        hook_registry=None,
        state_dir=state_dir,
        session_dir=session_dir,
        store_path=store_path,
        workspace_dir=workspace_dir,
        model_ref="test/test-model",
        model_id="test-model",
        image_model_ref=None,
        run_registry=RunRegistry(),
        runtime_guard=RuntimeUpdateGuard(),
    )


def test_embedded_backend_satisfies_protocol(tmp_path):
    """Runtime-checkable Backend Protocol → EmbeddedBackend instance is a Backend."""
    backend = EmbeddedBackend(_fake_runtime(tmp_path))
    assert isinstance(backend, Backend)


def test_turn_registry_preserves_workspace_write_hook(tmp_path):
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    calls = []

    def hook(tool_name, ctx):
        calls.append((tool_name, ctx.workspace_dir))

    runtime.registry.set_workspace_dir(runtime.workspace_dir)
    runtime.registry.set_before_workspace_write(hook)

    clone = backend._build_turn_registry("session-1")
    ctx = clone.execution_context()
    clone.before_workspace_write("apply_patch", ctx)

    assert calls == [("apply_patch", str(runtime.workspace_dir))]


def test_turn_registry_applies_channel_tool_decoration(tmp_path):
    runtime = _fake_runtime(tmp_path)
    channels = ChannelManager()
    channels.register(_DecoratingChannel)

    async def run():
        inst = await channels.start("decorating", ChannelAccount(id="work"), runtime)
        backend = EmbeddedBackend(runtime, channel_manager=channels)
        try:
            clone = backend._build_turn_registry(
                "session-1",
                channel_id="decorating",
                channel_account_id="work",
                channel_sender_key="user-123",
            )
            tool = clone.get("sender_key")
            assert tool is not None
            assert tool.run({}) == "user-123"
            assert inst.status().state == "running"
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_channel_turn_registry_registers_terminal_exit_tool(tmp_path):
    runtime = _fake_runtime(tmp_path)
    channels = ChannelManager()
    channels.register(_RecordingChannel)

    async def run():
        await channels.start("recording", ChannelAccount(id="work"), runtime)
        backend = EmbeddedBackend(runtime, channel_manager=channels)
        try:
            request = ChannelExitRequest()
            registry = backend._build_turn_registry(
                "session-1",
                channel_id="recording",
                channel_account_id="work",
                channel_sender_key="user-123",
                exit_request=request,
            )
            tool = registry.get("exit")
            assert tool is not None
            assert tool.terminal is True
            assert all(phrase in tool.description for phrase in ("再见", "退下", "等会儿聊"))
            result = tool.run({"reason": "talk later"})
            assert request.requested is True
            assert request.reason == "talk later"
            assert result == "Channel exit requested. End this turn now."
            assert runtime.registry.get("exit") is None
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_channel_exit_dispatch_runs_after_turn_is_released(tmp_path):
    runtime = _fake_runtime(tmp_path)
    channels = ChannelManager()
    channels.register(_RecordingChannel)

    async def fake_run_turn(agent_session, _text, **_kwargs):
        tool = agent_session.registry.get("exit")
        assert tool is not None
        result = tool.run({"reason": "user said goodbye"})
        assert "exit requested" in result.lower()

    async def run():
        instance = await channels.start(
            "recording",
            ChannelAccount(id="work"),
            runtime,
        )
        backend = EmbeddedBackend(runtime, channel_manager=channels)
        try:
            with patch(
                "nano_openclaw.services.backend_embedded.AgentSession.run_turn",
                fake_run_turn,
            ):
                turn_id = await backend.chat_send(
                    session_key="",
                    text="再见",
                    channel_id="recording",
                    channel_account_id="work",
                    channel_sender_key="user-123",
                )
                await backend.await_turn(turn_id)

            assert instance.exits == [{
                "sender_key": "user-123",
                "reason": "user said goodbye",
            }]
            assert runtime.run_registry.get(turn_id) is None
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_subscribe_yields_emitted_events(tmp_path):
    async def run():
        backend = EmbeddedBackend(_fake_runtime(tmp_path))
        iterator = backend.subscribe()

        backend._emit(PushEvent(event="agent.event", payload={"type": "hello"}, seq=1))
        backend._emit(PushEvent(event="session.changed", payload={"session_id": "x"}, seq=2))

        received = []

        async def reader():
            async for evt in iterator:
                received.append(evt)
                if len(received) == 2:
                    break

        await asyncio.wait_for(reader(), timeout=1.0)
        await backend.aclose()
        return received

    received = asyncio.run(run())
    assert len(received) == 2
    assert received[0].event == "agent.event"
    assert received[1].event == "session.changed"
    assert received[0].seq == 1
    assert received[1].seq == 2


def test_subscribe_filters_by_session_key(tmp_path):
    async def run():
        backend = EmbeddedBackend(_fake_runtime(tmp_path))
        only_a = backend.subscribe(session_key="A")

        backend._emit(PushEvent(event="agent.event", payload={"session_id": "A", "x": 1}, seq=1))
        backend._emit(PushEvent(event="agent.event", payload={"session_id": "B", "x": 2}, seq=2))
        backend._emit(PushEvent(event="agent.event", payload={"session_id": "A", "x": 3}, seq=3))

        received: list[PushEvent] = []

        async def reader():
            async for evt in only_a:
                received.append(evt)
                if len(received) == 2:
                    break

        await asyncio.wait_for(reader(), timeout=1.0)
        await backend.aclose()
        return received

    received = asyncio.run(run())
    assert [e.payload["x"] for e in received] == [1, 3]


def test_subscribe_filters_by_event_kind(tmp_path):
    async def run():
        backend = EmbeddedBackend(_fake_runtime(tmp_path))
        only_approvals = backend.subscribe(events=["approval.request"])

        backend._emit(PushEvent(event="agent.event", payload={}, seq=1))
        backend._emit(PushEvent(event="approval.request", payload={"request_id": "r1"}, seq=2))
        backend._emit(PushEvent(event="agent.event", payload={}, seq=3))

        received: list[PushEvent] = []

        async def reader():
            async for evt in only_approvals:
                received.append(evt)
                break

        await asyncio.wait_for(reader(), timeout=1.0)
        await backend.aclose()
        return received

    received = asyncio.run(run())
    assert len(received) == 1
    assert received[0].event == "approval.request"


def test_busy_error_when_session_locked(tmp_path):
    async def run():
        backend = EmbeddedBackend(_fake_runtime(tmp_path))
        session = backend.manager.create()
        await session.lock.acquire()
        session.active_turn_id = "fake-turn"
        try:
            with pytest.raises(BusyError) as exc_info:
                await backend.chat_send(session_key=session.session_id, text="hi")
            assert exc_info.value.retry_after_ms == 500
            assert exc_info.value.details["session_id"] == session.session_id
        finally:
            session.lock.release()
            session.active_turn_id = None
            await backend.aclose()

    asyncio.run(run())


def test_chat_abort_unknown_turn_is_noop(tmp_path):
    async def run():
        backend = EmbeddedBackend(_fake_runtime(tmp_path))
        await backend.chat_abort(turn_id="does-not-exist")
        await backend.aclose()

    asyncio.run(run())


def test_chat_abort_interrupts_task_and_releases_session(tmp_path):
    async def run():
        runtime = _fake_runtime(tmp_path)
        backend = EmbeddedBackend(runtime)
        session = backend.manager.create()
        started = asyncio.Event()
        events = backend.subscribe(session_key=session.session_id, events=["agent.event"])

        async def blocked_turn(_self, _text, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        try:
            with patch(
                "nano_openclaw.services.backend_embedded.AgentSession.run_turn",
                new=blocked_turn,
            ):
                turn_id = await backend.chat_send(session_key=session.session_id, text="hi")
                await asyncio.wait_for(started.wait(), timeout=0.2)
                await asyncio.wait_for(backend.chat_abort(turn_id=turn_id), timeout=0.2)
                await backend.await_turn(turn_id)

            details = await backend.sessions_get(session.session_id)
            assert details.active_turn_id is None
            assert runtime.run_registry.get(turn_id) is None

            async def cancelled_payload():
                async for event in events:
                    if event.payload.get("type") == "turn.cancelled":
                        return event.payload
                raise AssertionError("event stream closed before turn.cancelled")

            payload = await asyncio.wait_for(cancelled_payload(), timeout=0.2)
            assert payload["turn_id"] == turn_id
        finally:
            await events.aclose()
            await backend.aclose()

    asyncio.run(run())


def test_subscribe_drop_oldest_on_overflow(tmp_path):
    async def run():
        backend = EmbeddedBackend(_fake_runtime(tmp_path))
        iterator = backend.subscribe()

        for i in range(SUBSCRIBER_QUEUE_MAX + 20):
            backend._emit(PushEvent(event="agent.event", payload={"i": i}, seq=i))

        received: list[PushEvent] = []

        async def reader():
            async for evt in iterator:
                received.append(evt)
                if len(received) >= SUBSCRIBER_QUEUE_MAX:
                    break

        await asyncio.wait_for(reader(), timeout=1.0)
        await backend.aclose()
        return received

    received = asyncio.run(run())
    assert any(e.event == "gap" for e in received), "expected synthetic gap event after overflow"


def test_approvals_respond_unknown_request_raises(tmp_path):
    async def run():
        backend = EmbeddedBackend(_fake_runtime(tmp_path))
        with pytest.raises(NotFoundError):
            await backend.approvals_respond("unknown-id", allow=True)
        await backend.aclose()

    asyncio.run(run())


def test_runtime_get_returns_snapshot(tmp_path):
    async def run():
        backend = EmbeddedBackend(_fake_runtime(tmp_path))
        snap = await backend.runtime_get()
        await backend.aclose()
        return snap

    snap = asyncio.run(run())
    assert snap.agent_id == "default"
    assert snap.model_id == "test-model"
    assert snap.thinking_level == "off"


def test_health_returns_summary(tmp_path):
    async def run():
        backend = EmbeddedBackend(_fake_runtime(tmp_path))
        h = await backend.health()
        await backend.aclose()
        return h

    h = asyncio.run(run())
    assert h.runtime_ready is True
    assert h.in_flight_turns == 0


def test_channels_use_injected_manager(tmp_path):
    async def run():
        manager = ChannelManager()
        manager.register(_RecordingChannel)
        backend = EmbeddedBackend(_fake_runtime(tmp_path), channel_manager=manager)

        started = await backend.channels_start("recording", "work")
        statuses = await backend.channels_status()
        health = await backend.health()
        await backend.channels_stop("recording", "work")
        stopped_statuses = await backend.channels_status()
        await backend.aclose()

        return started, statuses, health, stopped_statuses

    started, statuses, health, stopped_statuses = asyncio.run(run())
    assert started.channel_id == "recording"
    assert started.account_id == "work"
    assert started.state == "running"
    assert [(s.channel_id, s.account_id, s.state) for s in statuses] == [("recording", "work", "running")]
    assert health.channels_running == 1
    assert stopped_statuses == []


def test_channels_stop_reports_actual_state_when_adapter_stop_fails(tmp_path):
    async def run():
        manager = ChannelManager()
        manager.register(_FailingStopChannel)
        backend = EmbeddedBackend(_fake_runtime(tmp_path), channel_manager=manager)

        await backend.channels_start("failing-stop")
        stopped = await backend.channels_stop("failing-stop")
        statuses = await backend.channels_status()
        await backend.aclose()

        return stopped, statuses

    stopped, statuses = asyncio.run(run())
    assert stopped.channel_id == "failing-stop"
    assert stopped.state == "running"
    assert stopped.error == "stop failed"
    assert [(s.channel_id, s.state, s.error) for s in statuses] == [
        ("failing-stop", "running", "stop failed")
    ]
