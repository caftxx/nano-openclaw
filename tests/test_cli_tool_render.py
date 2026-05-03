import asyncio

from rich.console import Console

from nano_openclaw.cli import _make_event_handler
from nano_openclaw.loop import ToolResult, _consume_one_assistant_turn
from nano_openclaw.provider import MessageEnd, ToolUseDelta, ToolUseEnd, ToolUseStart


def test_event_handler_single_tool_stays_simple():
    console = Console(record=True, width=120)
    handler = _make_event_handler(console)

    handler(ToolUseStart(id="tool-1", name="web_search"))
    handler(ToolUseEnd(id="tool-1"))
    handler(ToolResult(
        tool_use_id="tool-1",
        name="web_search",
        args={"query": "rust"},
        result={"content": [{"type": "text", "text": "first result"}]},
    ))

    output = console.export_text()
    assert ">> web_search" in output
    assert "Tools" not in output
    assert "web_search #2" not in output


def test_event_handler_groups_parallel_same_name_tools():
    console = Console(record=True, width=120)
    handler = _make_event_handler(console)

    handler(ToolUseStart(id="tool-1", name="web_search"))
    handler(ToolUseStart(id="tool-2", name="web_search"))
    handler(ToolUseEnd(id="tool-1"))
    handler(ToolUseEnd(id="tool-2"))
    handler(ToolResult(
        tool_use_id="tool-1",
        name="web_search",
        args={"query": "rust"},
        result={"content": [{"type": "text", "text": "first result"}]},
    ))
    handler(ToolResult(
        tool_use_id="tool-2",
        name="web_search",
        args={"query": "cpp"},
        result={"content": [{"type": "text", "text": "second result"}]},
    ))

    output = console.export_text()
    assert ">> web_search" in output
    assert output.count('web_search({"query": "rust"})') == 1
    assert output.count('web_search #2({"query": "cpp"})') == 1


def test_event_handler_resets_name_counts_between_batches():
    console = Console(record=True, width=120)
    handler = _make_event_handler(console)

    handler(ToolUseStart(id="tool-1", name="web_search"))
    handler(ToolUseEnd(id="tool-1"))
    handler(ToolResult(
        tool_use_id="tool-1",
        name="web_search",
        args={"query": "rust"},
        result={"content": [{"type": "text", "text": "first result"}]},
    ))
    handler(ToolUseStart(id="tool-2", name="web_search"))
    handler(ToolUseEnd(id="tool-2"))
    handler(ToolResult(
        tool_use_id="tool-2",
        name="web_search",
        args={"query": "cpp"},
        result={"content": [{"type": "text", "text": "second result"}]},
    ))

    output = console.export_text()
    assert output.count(">> web_search") == 2
    assert "web_search #2" not in output


def test_consume_assistant_turn_keeps_interleaved_tool_calls(monkeypatch):
    async def fake_stream_response(**_kwargs):
        yield ToolUseStart(id="tool-1", name="web_search")
        yield ToolUseStart(id="tool-2", name="web_search")
        yield ToolUseDelta(id="tool-1", partial_json='{"query": "rust"}')
        yield ToolUseDelta(id="tool-2", partial_json='{"query": "cpp"}')
        yield ToolUseEnd(id="tool-1")
        yield ToolUseEnd(id="tool-2")
        yield MessageEnd(stop_reason="tool_use", usage={})

    monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)

    blocks, stop_reason = asyncio.run(
        _consume_one_assistant_turn(
            client=object(),
            api="openai",
            model="test",
            system="system",
            messages=[],
            tools=[],
            max_tokens=128,
            thinking_budget_tokens=None,
            on_event=lambda _event: None,
        )
    )

    assert stop_reason == "tool_use"
    assert blocks == [
        {"type": "tool_use", "id": "tool-1", "name": "web_search", "input": {"query": "rust"}},
        {"type": "tool_use", "id": "tool-2", "name": "web_search", "input": {"query": "cpp"}},
    ]
