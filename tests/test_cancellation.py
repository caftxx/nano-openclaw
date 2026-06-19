from __future__ import annotations

import asyncio
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from nano_openclaw.adapters.cli.repl import _manual_compact, repl
from nano_openclaw.config.types import MemoryFlushConfig
from nano_openclaw.core.loop import (
    AgentSession,
    CancellationToken,
    LoopConfig,
    Message,
    TurnCancelled,
    _build_memory_flush_prompt,
)
from nano_openclaw.core.provider import MessageEnd, TextDelta, ToolUseDelta, ToolUseEnd, ToolUseStart
from nano_openclaw.session import TranscriptReader, load_session_store
from nano_openclaw.session.transcript import TranscriptWriter
from nano_openclaw.core.tools import Tool, ToolRegistry


def test_agent_session_cancellation_during_stream_discards_turn(monkeypatch):
    history = [Message("user", [{"type": "text", "text": "earlier"}])]
    registry = ToolRegistry()
    token = CancellationToken()
    token.cancel()

    async def fake_stream_response(**_kwargs):
        yield TextDelta(text="partial")
        yield MessageEnd(stop_reason="end_turn")

    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

    with pytest.raises(TurnCancelled):
        session = AgentSession(
            history=history,
            registry=registry,
            on_event=lambda _event: None,
            client=object(),
            cfg=LoopConfig(),
            cancellation_token=token,
        )
        asyncio.run(session.run_turn("hello"))

    assert history == [Message("user", [{"type": "text", "text": "earlier"}])]


def test_agent_session_cancellation_before_tool_dispatch_discards_turn(monkeypatch):
    history: list[Message] = []
    registry = ToolRegistry()
    tool_called = False
    tmp_dir = Path("tests") / f".tmp-cancel-{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    def run_tool(_args):
        nonlocal tool_called
        tool_called = True
        return "ran"

    registry.register(
        Tool(
            name="demo",
            description="demo tool",
            input_schema={"type": "object", "properties": {}},
            run=run_tool,
        )
    )

    try:
        writer = TranscriptWriter(tmp_dir / "session.jsonl")
        writer.start(model="test-model")

        token = CancellationToken()

        def on_event(event):
            if isinstance(event, ToolUseStart):
                token.cancel()

        async def fake_stream_response(**_kwargs):
            yield ToolUseStart(id="tool-1", name="demo")
            yield ToolUseEnd(id="tool-1")
            yield MessageEnd(stop_reason="tool_use")

        monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

        with pytest.raises(TurnCancelled):
            session = AgentSession(
                history=history,
                registry=registry,
                on_event=on_event,
                client=object(),
                cfg=LoopConfig(),
                transcript_writer=writer,
                cancellation_token=token,
            )
            asyncio.run(session.run_turn("run tool"))

        assert tool_called is False
        assert len(history) == 1
        assert history[0].role == "user"
        assert history[0].content[0]["text"] == "run tool"
        assert writer.message_count == 1
        assert (tmp_dir / "session.jsonl").exists()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_agent_session_success_does_not_duplicate_immediate_user_message(monkeypatch):
    history: list[Message] = []
    registry = ToolRegistry()
    tmp_dir = Path("tests") / f".tmp-success-{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    async def fake_stream_response(**_kwargs):
        yield TextDelta(text="reply")
        yield MessageEnd(stop_reason="end_turn", usage={})

    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

    try:
        writer = TranscriptWriter(tmp_dir / "session.jsonl")
        writer.start(model="test-model")
        session = AgentSession(
            history=history,
            registry=registry,
            on_event=lambda _event: None,
            client=object(),
            cfg=LoopConfig(),
            transcript_writer=writer,
        )

        asyncio.run(session.run_turn("hello"))

        assert [message.role for message in history] == ["user", "assistant"]
        assert history[0].content[0]["text"] == "hello"
        assert writer.message_count == 2
        from nano_openclaw.session.transcript import TranscriptReader

        loaded, _, msg_count, _, _ = TranscriptReader(tmp_dir / "session.jsonl").load_history()
        assert msg_count == 2
        assert [message.role for message in loaded] == ["user", "assistant"]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_agent_session_memory_flush_is_silent_and_dispatches_tools(monkeypatch):
    history: list[Message] = []
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="write_file",
            description="write file",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            run=lambda _args: "should not overwrite",
        )
    )
    visible_events: list[object] = []
    tmp_dir = Path("tests") / f".tmp-memory-flush-{uuid.uuid4().hex}"
    target = tmp_dir / "memory" / "2026-05-04.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("existing note\n", encoding="utf-8")

    stream_calls = 0

    async def fake_stream_response(**kwargs):
        nonlocal stream_calls
        stream_calls += 1
        if stream_calls == 1:
            assert [tool["name"] for tool in kwargs["tools"]] == ["write_file"]
            assert "memory/2026-05-04.md" in str(kwargs["messages"])
            yield ToolUseStart(id="tool-1", name="write_file")
            yield ToolUseDelta(
                id="tool-1",
                partial_json='{"path":"memory/2026-05-04.md","content":"new note"}',
            )
            yield ToolUseEnd(id="tool-1")
            yield MessageEnd(stop_reason="tool_use", usage={})
        elif stream_calls == 2:
            yield MessageEnd(stop_reason="end_turn", usage={})
        else:
            yield TextDelta(text="main reply")
            yield MessageEnd(stop_reason="end_turn", usage={})

    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)
    monkeypatch.setattr("nano_openclaw.core.loop.datetime", _fixed_datetime())

    try:
        session = AgentSession(
            history=history,
            registry=registry,
            on_event=visible_events.append,
            client=object(),
            cfg=LoopConfig(
                context_budget=1000,
                context_threshold=1.0,
                context_window=150,
                workspace_dir=tmp_dir,
                memory_flush_config=MemoryFlushConfig(
                    reserveTokensFloor=20,
                    softThresholdTokens=10,
                    prompt="flush memory/YYYY-MM-DD.md now",
                ),
            ),
        )
        asyncio.run(session.run_turn("x" * 500))

        assert target.read_text(encoding="utf-8") == "existing note\nnew note"
        assert stream_calls == 3
        assert len(history) == 2
        assert history[0].role == "user"
        assert history[1].content == [{"type": "text", "text": "main reply"}]
        assert "flush memory" not in str(history)
        assert [type(event).__name__ for event in visible_events] == ["TextDelta", "MessageEnd"]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_agent_session_skips_memory_flush_without_write_tool(monkeypatch):
    history: list[Message] = []
    registry = ToolRegistry()
    stream_calls = 0

    async def fake_stream_response(**kwargs):
        nonlocal stream_calls
        stream_calls += 1
        assert kwargs["tools"] == []
        yield TextDelta(text="main reply")
        yield MessageEnd(stop_reason="end_turn", usage={})

    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

    session = AgentSession(
        history=history,
        registry=registry,
        on_event=lambda _event: None,
        client=object(),
        cfg=LoopConfig(
            context_budget=1000,
            context_threshold=1.0,
            context_window=150,
            workspace_dir=Path("tests"),
            memory_flush_config=MemoryFlushConfig(
                reserveTokensFloor=20,
                softThresholdTokens=10,
            ),
        ),
    )
    asyncio.run(session.run_turn("x" * 500))

    assert stream_calls == 1
    assert history[1].content == [{"type": "text", "text": "main reply"}]


def test_manual_compact_runs_silent_memory_flush(monkeypatch):
    history = [
        Message("user", [{"type": "text", "text": "older"}]),
        Message("assistant", [{"type": "text", "text": "reply"}]),
    ]
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="write_file",
            description="write file",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            run=lambda _args: "should not overwrite",
        )
    )
    tmp_dir = Path("tests") / f".tmp-manual-memory-flush-{uuid.uuid4().hex}"
    target = tmp_dir / "memory" / "2026-05-04.md"
    stream_calls = 0

    async def fake_stream_response(**kwargs):
        nonlocal stream_calls
        stream_calls += 1
        assert [tool["name"] for tool in kwargs["tools"]] == ["write_file"]
        if stream_calls == 1:
            yield ToolUseStart(id="tool-1", name="write_file")
            yield ToolUseDelta(
                id="tool-1",
                partial_json='{"path":"memory/2026-05-04.md","content":"manual note"}',
            )
            yield ToolUseEnd(id="tool-1")
            yield MessageEnd(stop_reason="tool_use", usage={})
        else:
            yield MessageEnd(stop_reason="end_turn", usage={})

    async def fake_compact_if_needed(history_arg, **_kwargs):
        return history_arg, "manual summary"

    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)
    monkeypatch.setattr("nano_openclaw.core.loop.datetime", _fixed_datetime())
    monkeypatch.setattr("nano_openclaw.adapters.cli.repl.compact_if_needed", fake_compact_if_needed)

    try:
        console = Console(record=True)
        asyncio.run(_manual_compact(
            console,
            history,
            LoopConfig(
                context_recent_turns=1,
                workspace_dir=tmp_dir,
                memory_flush_config=MemoryFlushConfig(prompt="flush memory/YYYY-MM-DD.md now"),
            ),
            object(),
            registry,
        ))

        assert stream_calls == 2
        assert target.read_text(encoding="utf-8") == "manual note"
        assert "flush memory" not in str(history)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_build_memory_flush_prompt_replaces_date_and_adds_time(monkeypatch):
    monkeypatch.setattr("nano_openclaw.core.loop.datetime", _fixed_datetime())

    prompt = _build_memory_flush_prompt("Store memory/YYYY-MM-DD.md")

    assert "memory/2026-05-04.md" in prompt
    assert "YYYY-MM-DD" not in prompt
    assert "Current time:" in prompt


def _fixed_datetime():
    class FixedDatetime:
        @classmethod
        def now(cls):
            from datetime import datetime

            return datetime(2026, 5, 4, 10, 30)

    return FixedDatetime


def test_repl_swallow_turn_cancelled(monkeypatch):
    registry = ToolRegistry()
    cfg = LoopConfig()
    console = Console(record=True)

    inputs = iter(["hello", "/quit"])

    async def fake_prompt_async():
        return next(inputs)

    fake_session = SimpleNamespace(prompt_async=fake_prompt_async)

    monkeypatch.setattr("nano_openclaw.adapters.cli.repl.Console", lambda: console)
    monkeypatch.setattr("nano_openclaw.adapters.cli.repl._get_pt_session", lambda: fake_session)
    monkeypatch.setattr("nano_openclaw.adapters.cli.repl._print_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "nano_openclaw.adapters.cli.repl.AgentSession.run_turn",
        AsyncMock(side_effect=TurnCancelled()),
    )

    asyncio.run(repl(registry, client=MagicMock(), cfg=cfg))

    output = console.export_text()
    assert "turn cancelled" in output.lower()
    assert "error:" not in output.lower()


def test_ws_repl_escape_aborts_turn(monkeypatch):
    """Esc during a remote-mode turn must reach the daemon as ``chat.abort``.

    Regression: the WebSocket path drops ``cancellation_token`` on the wire,
    so ws_repl wires its own Esc watcher → ``chat.abort`` RPC. The fake
    backend simulates Esc by flipping the token after the first delta, then
    blocks the stream until the abort lands — if ws_repl never sends the
    abort, the test hangs (caught by the asyncio timeout) instead of passing.
    """
    from contextlib import contextmanager

    from nano_openclaw.services.backend import PushEvent
    from nano_openclaw.adapters.cli.ws_repl import ws_repl

    console = Console(record=True)
    inputs = iter(["hello", "/quit"])

    async def fake_prompt_async():
        return next(inputs)

    fake_session = SimpleNamespace(prompt_async=fake_prompt_async)
    monkeypatch.setattr("nano_openclaw.adapters.cli.repl._get_pt_session", lambda: fake_session)

    # Real token, fake key source — the watcher thread reads the tty, which
    # doesn't exist under pytest. The fake backend flips the token instead.
    token = CancellationToken()

    @contextmanager
    def fake_escape_token():
        yield token

    monkeypatch.setattr(
        "nano_openclaw.adapters.cli.ws_repl._escape_cancellation_token", fake_escape_token,
    )

    class FakeBackend:
        def __init__(self):
            self.abort_calls: list[str] = []
            self._abort_landed: asyncio.Event | None = None

        async def runtime_get(self):
            return SimpleNamespace(agent_id="agent", model_id="model")

        async def chat_send(self, **_kwargs):
            return "turn-1"

        async def chat_abort(self, *, turn_id: str) -> None:
            self.abort_calls.append(turn_id)
            assert self._abort_landed is not None
            self._abort_landed.set()

        def subscribe(self, session_key=None):
            self._abort_landed = asyncio.Event()

            async def gen():
                yield PushEvent(
                    event="agent.event",
                    payload={"type": "text.delta", "text": "hi", "turn_id": "turn-1"},
                    seq=1,
                )
                token.cancel()  # simulate the Esc keypress mid-stream
                await self._abort_landed.wait()
                yield PushEvent(
                    event="agent.event",
                    payload={"type": "turn.cancelled", "turn_id": "turn-1"},
                    seq=2,
                )

            return gen()

        async def aclose(self):
            pass

    backend = FakeBackend()

    async def run_with_timeout():
        await asyncio.wait_for(
            ws_repl(backend, session_key="s1", console=console), timeout=10,
        )

    asyncio.run(run_with_timeout())

    assert backend.abort_calls == ["turn-1"]
    output = console.export_text()
    assert "turn cancelled" in output.lower()
    assert "error:" not in output.lower()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX watcher branch only")
def test_escape_token_posix_watcher_exits_on_external_cancel(monkeypatch):
    """External ``token.cancel()`` releases the watcher within the join
    timeout, and ``raw_mode`` is entered exactly once for the whole read
    window — proves the prior per-iteration ISIG-toggle anti-pattern (and
    its accompanying CPU spin) is gone.
    """
    from nano_openclaw.adapters.cli.repl import _escape_cancellation_token

    class _FakeInput:
        def __init__(self) -> None:
            self.raw_entries = 0
            self.read_calls = 0
            self.closed = False

        def raw_mode(self):
            @contextmanager
            def _ctx():
                self.raw_entries += 1
                yield
            return _ctx()

        def read_keys(self):
            self.read_calls += 1
            return []

        def close(self):
            self.closed = True

    fake = _FakeInput()
    monkeypatch.setattr("prompt_toolkit.input.create_input", lambda: fake)

    start = time.monotonic()
    with _escape_cancellation_token() as token:
        # Let the watcher run a few iterations of the empty-read sleep loop.
        time.sleep(0.05)
        assert fake.read_calls >= 1, "watcher thread did not run"
        assert fake.raw_entries == 1, (
            f"raw_mode entered {fake.raw_entries}× — should be held once "
            "across the whole read window to keep ISIG off."
        )
        token.cancel()
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"context teardown took {elapsed:.2f}s (watcher hung?)"
    assert fake.closed, "input_handle.close() was not called on teardown"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX watcher branch only")
def test_escape_token_posix_watcher_catches_ctrl_c_key(monkeypatch):
    """POSIX raw_mode means SIGINT is masked; the watcher must translate
    a ``Keys.ControlC`` keypress into ``token.cancel()`` so embedded mode
    can soft-cancel the in-flight turn.
    """
    from prompt_toolkit.keys import Keys

    from nano_openclaw.adapters.cli.repl import _escape_cancellation_token

    class _CtrlCInput:
        def __init__(self) -> None:
            self._delivered = False

        def raw_mode(self):
            @contextmanager
            def _ctx():
                yield
            return _ctx()

        def read_keys(self):
            if self._delivered:
                return []
            self._delivered = True
            return [SimpleNamespace(key=Keys.ControlC)]

        def close(self):
            pass

    monkeypatch.setattr("prompt_toolkit.input.create_input", lambda: _CtrlCInput())

    with _escape_cancellation_token() as token:
        for _ in range(50):
            if token.is_cancelled:
                break
            time.sleep(0.02)
    assert token.is_cancelled, "watcher did not translate Ctrl+C key into token.cancel()"


def test_repl_keyboard_interrupt_during_turn_soft_cancels(monkeypatch):
    """Mid-turn ``KeyboardInterrupt`` is recovered as a soft ``(turn
    cancelled)`` so the REPL doesn't crash. Covers Windows (no raw mode →
    SIGINT fires) and the brief POSIX windows where ISIG is still on.
    """
    registry = ToolRegistry()
    cfg = LoopConfig(model="test-model")
    console = Console(record=True)

    inputs = iter(["hello", "/quit"])

    async def fake_prompt_async():
        return next(inputs)

    fake_session = SimpleNamespace(prompt_async=fake_prompt_async)
    monkeypatch.setattr("nano_openclaw.adapters.cli.repl.Console", lambda: console)
    monkeypatch.setattr("nano_openclaw.adapters.cli.repl._get_pt_session", lambda: fake_session)
    monkeypatch.setattr("nano_openclaw.adapters.cli.repl._print_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "nano_openclaw.adapters.cli.repl.AgentSession.run_turn",
        AsyncMock(side_effect=KeyboardInterrupt()),
    )

    asyncio.run(repl(registry, client=MagicMock(), cfg=cfg))

    output = console.export_text()
    assert "turn cancelled" in output.lower()
    assert "traceback" not in output.lower()
    assert "error:" not in output.lower()


def test_ws_repl_abort_failure_surfaces_feedback(monkeypatch):
    """When ``chat.abort`` fails (daemon hung / timeout / disconnect), the
    user must see a visible hint instead of staring at a silent frozen
    stream — that silence was the exact failure mode the previous bare
    ``except Exception: pass`` produced.
    """
    from nano_openclaw.services.backend import PushEvent
    from nano_openclaw.adapters.cli.ws_repl import ws_repl

    console = Console(record=True)
    inputs = iter(["hello", "/quit"])

    async def fake_prompt_async():
        return next(inputs)

    fake_session = SimpleNamespace(prompt_async=fake_prompt_async)
    monkeypatch.setattr("nano_openclaw.adapters.cli.repl._get_pt_session", lambda: fake_session)

    token = CancellationToken()

    @contextmanager
    def fake_escape_token():
        yield token

    monkeypatch.setattr(
        "nano_openclaw.adapters.cli.ws_repl._escape_cancellation_token", fake_escape_token,
    )

    class FailingBackend:
        def __init__(self):
            self._abort_attempted: asyncio.Event | None = None

        async def runtime_get(self):
            return SimpleNamespace(agent_id="agent", model_id="model")

        async def chat_send(self, **_kwargs):
            return "turn-1"

        async def chat_abort(self, *, turn_id: str) -> None:
            assert self._abort_attempted is not None
            self._abort_attempted.set()
            raise RuntimeError("daemon busy")

        def subscribe(self, session_key=None):
            self._abort_attempted = asyncio.Event()

            async def gen():
                yield PushEvent(
                    event="agent.event",
                    payload={"type": "text.delta", "text": "hi", "turn_id": "turn-1"},
                    seq=1,
                )
                token.cancel()  # simulate Esc
                # Wait for the abort RPC to have fired and failed, then end
                # the loop so the test can assert on the printed feedback.
                await self._abort_attempted.wait()
                yield PushEvent(
                    event="agent.event",
                    payload={"type": "turn.cancelled", "turn_id": "turn-1"},
                    seq=2,
                )

            return gen()

        async def aclose(self):
            pass

    backend = FailingBackend()

    async def run_with_timeout():
        await asyncio.wait_for(
            ws_repl(backend, session_key="s1", console=console), timeout=10,
        )

    asyncio.run(run_with_timeout())

    output = console.export_text()
    assert "abort failed" in output.lower()
    assert "runtimeerror" in output.lower()


def test_repl_new_session_first_cancelled_input_is_persisted(monkeypatch):
    registry = ToolRegistry()
    cfg = LoopConfig(model="test-model")
    console = Console(record=True)
    tmp_dir = Path("tests") / f".tmp-cli-cancel-{uuid.uuid4().hex}"
    session_dir = tmp_dir / "sessions"
    store_path = session_dir / "sessions.json"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    inputs = iter(["/new", "hello", "/quit"])

    async def fake_prompt_async():
        return next(inputs)

    fake_session = SimpleNamespace(prompt_async=fake_prompt_async)

    async def fake_stream_response(**_kwargs):
        yield TextDelta(text="partial")
        raise TurnCancelled()

    try:
        monkeypatch.setattr("nano_openclaw.adapters.cli.repl.Console", lambda: console)
        monkeypatch.setattr("nano_openclaw.adapters.cli.repl._get_pt_session", lambda: fake_session)
        monkeypatch.setattr("nano_openclaw.adapters.cli.repl._print_banner", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

        asyncio.run(repl(
            registry,
            client=MagicMock(),
            cfg=cfg,
            session_dir=session_dir,
            store_path=store_path,
        ))

        store = load_session_store(store_path)
        assert len(store["sessions"]) == 1
        session_id = next(iter(store["sessions"]))
        assert store["sessions"][session_id]["message_count"] == 1
        transcript_path = session_dir / f"{session_id}.jsonl"
        assert transcript_path.exists()
        history, _, msg_count, _, _ = TranscriptReader(transcript_path).load_history()
        assert msg_count == 1
        assert history[0].role == "user"
        assert history[0].content[0]["text"] == "hello"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
