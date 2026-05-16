from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from nano_openclaw.cli import _manual_compact, repl
from nano_openclaw.config.types import MemoryFlushConfig
from nano_openclaw.loop import (
    AgentSession,
    CancellationToken,
    LoopConfig,
    Message,
    TurnCancelled,
    _build_memory_flush_prompt,
)
from nano_openclaw.provider import MessageEnd, TextDelta, ToolUseDelta, ToolUseEnd, ToolUseStart
from nano_openclaw.session import TranscriptReader, load_session_store
from nano_openclaw.session.transcript import TranscriptWriter
from nano_openclaw.tools import Tool, ToolRegistry


def test_agent_session_cancellation_during_stream_discards_turn(monkeypatch):
    history = [Message("user", [{"type": "text", "text": "earlier"}])]
    registry = ToolRegistry()
    token = CancellationToken()
    token.cancel()

    async def fake_stream_response(**_kwargs):
        yield TextDelta(text="partial")
        yield MessageEnd(stop_reason="end_turn")

    monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)

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

        monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)

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

    monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)

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

    monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)
    monkeypatch.setattr("nano_openclaw.loop.datetime", _fixed_datetime())

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

    monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)

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

    monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)
    monkeypatch.setattr("nano_openclaw.loop.datetime", _fixed_datetime())
    monkeypatch.setattr("nano_openclaw.cli.compact_if_needed", fake_compact_if_needed)

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
    monkeypatch.setattr("nano_openclaw.loop.datetime", _fixed_datetime())

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

    monkeypatch.setattr("nano_openclaw.cli.Console", lambda: console)
    monkeypatch.setattr("nano_openclaw.cli._get_pt_session", lambda: fake_session)
    monkeypatch.setattr("nano_openclaw.cli._print_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "nano_openclaw.cli.AgentSession.run_turn",
        AsyncMock(side_effect=TurnCancelled()),
    )

    asyncio.run(repl(registry, client=MagicMock(), cfg=cfg))

    output = console.export_text()
    assert "turn cancelled" in output.lower()
    assert "error:" not in output.lower()


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
        monkeypatch.setattr("nano_openclaw.cli.Console", lambda: console)
        monkeypatch.setattr("nano_openclaw.cli._get_pt_session", lambda: fake_session)
        monkeypatch.setattr("nano_openclaw.cli._print_banner", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)

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
