from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from nano_openclaw.cli import repl
from nano_openclaw.loop import CancellationToken, LoopConfig, Message, TurnCancelled, agent_loop
from nano_openclaw.provider import MessageEnd, TextDelta, ToolUseEnd, ToolUseStart
from nano_openclaw.session.transcript import TranscriptWriter
from nano_openclaw.tools import Tool, ToolRegistry


def test_agent_loop_cancellation_during_stream_discards_turn(monkeypatch):
    history = [Message("user", [{"type": "text", "text": "earlier"}])]
    registry = ToolRegistry()
    token = CancellationToken()
    token.cancel()

    async def fake_stream_response(**_kwargs):
        yield TextDelta(text="partial")
        yield MessageEnd(stop_reason="end_turn")

    monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)

    with pytest.raises(TurnCancelled):
        asyncio.run(agent_loop(
            user_input="hello",
            history=history,
            registry=registry,
            on_event=lambda _event: None,
            client=object(),
            cfg=LoopConfig(),
            cancellation_token=token,
        ))

    assert history == [Message("user", [{"type": "text", "text": "earlier"}])]


def test_agent_loop_cancellation_before_tool_dispatch_discards_turn(monkeypatch):
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
            yield ToolUseEnd()
            yield MessageEnd(stop_reason="tool_use")

        monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)

        with pytest.raises(TurnCancelled):
            asyncio.run(agent_loop(
                user_input="run tool",
                history=history,
                registry=registry,
                on_event=on_event,
                client=object(),
                cfg=LoopConfig(),
                transcript_writer=writer,
                cancellation_token=token,
            ))

        assert tool_called is False
        assert history == []
        assert writer.message_count == 0
        lines = (tmp_dir / "session.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_repl_swallow_turn_cancelled(monkeypatch):
    registry = ToolRegistry()
    cfg = LoopConfig()
    console = Console(record=True)

    inputs = iter(["hello", "/quit"])

    async def mock_repl_input(_console):
        return next(inputs)

    monkeypatch.setattr("nano_openclaw.cli.Console", lambda: console)
    monkeypatch.setattr("nano_openclaw.cli._repl_input", mock_repl_input)
    monkeypatch.setattr("nano_openclaw.cli._print_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("nano_openclaw.cli.agent_loop", AsyncMock(side_effect=TurnCancelled()))

    asyncio.run(repl(registry, client=MagicMock(), cfg=cfg))

    output = console.export_text()
    assert "turn cancelled" in output.lower()
    assert "error:" not in output.lower()
