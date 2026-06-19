import asyncio

from rich.console import Console

from nano_openclaw.cli import _build_tool_tree, _make_event_handler
from nano_openclaw.core.loop import (
    Compaction,
    ImageAttached,
    SkillInvoked,
    SubagentAnnounced,
    SubagentKilled,
    SubagentProgress,
    SubagentSpawned,
    ToolResult,
    _consume_one_assistant_turn,
)
from nano_openclaw.core.provider import MessageEnd, ToolUseDelta, ToolUseEnd, ToolUseStart


def test_event_handler_keeps_tool_tree_without_result_content():
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
    assert "● 1 tool call done" in output
    assert 'web_search() · ✓ 1 line' in output
    assert "first result" not in output
    assert "Tools" not in output


def test_event_handler_keeps_parallel_tool_tree_without_result_content():
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
    assert "● 2 tool calls done" in output
    assert "web_search() · ✓ 1 line" in output
    assert "web_search #2() · ✓ 1 line" in output
    assert "first result" not in output
    assert "second result" not in output


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
    assert output.count("● 1 tool call done") == 2
    assert output.count("web_search() · ✓ 1 line") == 2
    assert "first result" not in output
    assert "second result" not in output
    assert "web_search #2" not in output


def test_build_tool_tree_shows_running_and_done_items():
    console = Console(record=True, width=120)
    tool_slots = {
        "tool-1": {
            "display_name": "Glob",
            "args_buf": '{"pattern": "**/*.py"}',
            "done": True,
            "is_error": False,
            "result_preview": "23 lines",
        },
        "tool-2": {
            "display_name": "Read",
            "args_buf": '{"file": "cli.py"}',
            "done": False,
            "is_error": False,
            "result_preview": None,
        },
    }

    console.print(_build_tool_tree(tool_slots, 0.0))

    output = console.export_text()
    assert "● 2 tool calls..." in output
    assert 'Glob({"pattern": "**/*.py"}) · ✓ 23 lines' in output
    assert 'Read({"file": "cli.py"}) · running...' in output


def test_event_handler_renders_tool_errors_as_failures():
    console = Console(record=True, width=120)
    handler = _make_event_handler(console)

    handler(ToolUseStart(id="tool-1", name="Read"))
    handler(ToolUseEnd(id="tool-1"))
    handler(ToolResult(
        tool_use_id="tool-1",
        name="Read",
        args={"file": "missing.py"},
        result={
            "is_error": True,
            "content": [{"type": "text", "text": "File not found: missing.py"}],
        },
    ))

    output = console.export_text()
    assert "Read() · ✗ File not found: missing.py" in output
    assert "Read() · ✓ error" not in output


def test_event_handler_prints_tracked_subagent_killed_notice():
    console = Console(record=True, width=120)
    handler = _make_event_handler(console)

    handler(SubagentSpawned(run_id="run-1", task="inspect child state", label="child"))
    handler(SubagentKilled(run_id="run-1", task="inspect child state"))

    output = console.export_text()
    assert "● 1 agent done" in output
    assert "child · 0 tool uses" in output
    assert "✗ killed" in output


def test_event_handler_renders_untracked_subagent_summary_as_tree():
    console = Console(record=True, width=120)
    handler = _make_event_handler(console)

    handler(SubagentAnnounced(
        run_id="run-1",
        status="completed",
        task="inspect child state",
        result_text="child result",
        elapsed_ms=1200,
    ))

    output = console.export_text()
    assert "● Subagent" in output
    assert "inspect child state · ✓ completed · 1.2s · child result" in output
    assert "run · run-1" in output


def test_sessions_spawn_tool_does_not_hide_subagent_live_tree():
    console = Console(record=True, width=120)
    handler = _make_event_handler(console)

    handler(ToolUseStart(id="tool-1", name="sessions_spawn"))
    handler(ToolUseDelta(id="tool-1", partial_json='{"label": "research movies"}'))
    handler(ToolUseEnd(id="tool-1"))
    handler(SubagentSpawned(run_id="run-1", task="research movies", label="research movies"))
    handler(ToolResult(
        tool_use_id="tool-1",
        name="sessions_spawn",
        args={"label": "research movies", "task": "research movies"},
        result={"content": [{"type": "text", "text": "Subagent spawned successfully"}]},
    ))
    handler(SubagentProgress(
        run_id="run-1",
        label="research movies",
        tool_uses=3,
        input_tokens=1200,
        output_tokens=300,
        current_activity="WebSearch",
    ))
    handler(SubagentAnnounced(
        run_id="run-1",
        status="completed",
        task="research movies",
        result_text="done",
        elapsed_ms=1500,
    ))

    output = console.export_text()
    assert "● 1 agent done" in output
    assert "research movies · 3 tool uses · 1.5k tokens" in output
    assert "⎿  ✓ completed · 1.5s · done" in output
    assert "● 1 tool call done" not in output
    assert "sessions_spawn(" not in output


def test_event_handler_renders_status_events_as_trees():
    console = Console(record=True, width=120)
    handler = _make_event_handler(console)

    handler(SkillInvoked(skill_name="review", skill_path="C:/skills/review/SKILL.md"))
    handler(ImageAttached(refs=["diagram.png"], via_model=False))

    output = console.export_text()
    assert "● Skill" in output
    assert "review · C:/skills/review/SKILL.md" in output
    assert "● Image" in output
    assert "diagram.png · attached" in output


def test_event_handler_renders_compaction_summary_panel():
    console = Console(record=True, width=120)
    handler = _make_event_handler(console)

    handler(Compaction(summary="Conversation summary\nmore detail\nimportant retained fact"))

    output = console.export_text()
    assert "Context Compacted" in output
    assert "Conversation summary" in output
    assert "more detail" in output
    assert "important retained fact" in output


def test_consume_assistant_turn_keeps_interleaved_tool_calls(monkeypatch):
    async def fake_stream_response(**_kwargs):
        yield ToolUseStart(id="tool-1", name="web_search")
        yield ToolUseStart(id="tool-2", name="web_search")
        yield ToolUseDelta(id="tool-1", partial_json='{"query": "rust"}')
        yield ToolUseDelta(id="tool-2", partial_json='{"query": "cpp"}')
        yield ToolUseEnd(id="tool-1")
        yield ToolUseEnd(id="tool-2")
        yield MessageEnd(stop_reason="tool_use", usage={})

    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

    blocks, stop_reason, _usage = asyncio.run(
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
