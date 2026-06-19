from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from nano_openclaw.core.loop import AgentSession, LoopConfig, Message, SubagentEvent, SubagentProgress, ToolResult
from nano_openclaw.core.provider import MessageEnd, TextDelta, ToolUseDelta, ToolUseEnd, ToolUseStart
from nano_openclaw.subagent.registry import reset_registry
from nano_openclaw.subagent.runner import SubagentRunner, get_runner, reset_runner
from nano_openclaw.subagent.types import SpawnParams
from nano_openclaw.core.tools import Tool, ToolRegistry


def test_agent_session_waits_for_spawned_subagent_before_next_model_turn(monkeypatch):
    reset_registry()
    reset_runner()

    requester_session_key = "agent:default:main"
    registry = ToolRegistry()
    registry.set_spawn_tool_context(SimpleNamespace(requester_session_key=requester_session_key))
    sent_messages: list[list[dict]] = []

    def fake_spawn(_args, *, context):
        runner = get_runner()
        record = runner.registry.register(
            requester_session_key=context.requester_session_key,
            task="do child work",
            label="child",
        )
        runner.registry.mark_started(record.run_id)

        async def finish():
            await asyncio.sleep(0.02)
            runner.registry.mark_completed(record.run_id, result_text="child result", elapsed_ms=20)
            runner._pending_announcements.setdefault(context.requester_session_key, []).append(
                Message("user", [{"type": "text", "text": "subagent finished: child result"}])
            )

        runner._running_tasks[record.run_id] = asyncio.create_task(finish())
        return f"Subagent spawned successfully.\nRun ID: {record.run_id}"

    registry.register(
        Tool(
            name="sessions_spawn",
            description="spawn",
            input_schema={"type": "object", "properties": {"task": {"type": "string"}}},
            run=fake_spawn,
        )
    )

    async def fake_stream_response(**kwargs):
        sent_messages.append(kwargs["messages"])
        if len(sent_messages) == 1:
            yield ToolUseStart(id="tool-1", name="sessions_spawn")
            yield ToolUseDelta(id="tool-1", partial_json=json.dumps({"task": "do child work"}))
            yield ToolUseEnd(id="tool-1")
            yield MessageEnd(stop_reason="tool_use", usage={})
        else:
            assert any(
                "subagent finished: child result" in block.get("text", "")
                for msg in kwargs["messages"]
                for block in msg["content"]
            )
            yield TextDelta(text="done")
            yield MessageEnd(stop_reason="end_turn", usage={})

    async def async_dispatch(self, tool_use_id, name, args, cancellation_token=None):
        tool = self.get(name)
        output = tool.run(args, context=self._spawn_tool_context)
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": [{"type": "text", "text": output}],
        }

    monkeypatch.setattr(ToolRegistry, "dispatch", async_dispatch)
    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

    history: list[Message] = []
    session = AgentSession(
        history=history,
        registry=registry,
        on_event=lambda _event: None,
        client=object(),
        cfg=LoopConfig(session_key=requester_session_key),
    )
    asyncio.run(session.run_turn("spawn child"))

    assert len(sent_messages) == 2
    assert any(
        "subagent finished: child result" in block.get("text", "")
        for msg in sent_messages[1]
        for block in msg["content"]
    )


def test_subagent_runner_emits_progress_events(monkeypatch, tmp_path):
    reset_registry()

    async def fake_run_turn(self, _user_input, **_kwargs):
        self.on_event(ToolUseStart(id="tool-1", name="Read"))
        self.on_event(ToolResult(
            tool_use_id="tool-1",
            name="Read",
            args={"path": "demo.md"},
            result={"content": [{"type": "text", "text": "ok"}]},
        ))
        self.on_event(MessageEnd(
            stop_reason="end_turn",
            usage={"input_tokens": 1200, "output_tokens": 300},
        ))
        self.history.append(Message("assistant", [{"type": "text", "text": "child done"}]))

    monkeypatch.setattr("nano_openclaw.subagent.runner.AgentSession.run_turn", fake_run_turn)

    runner = SubagentRunner()
    record = runner.registry.register(
        requester_session_key="agent:default:main",
        task="inspect child state",
        label="child",
    )
    runner.registry.mark_started(record.run_id)

    events = []
    result = asyncio.run(runner._run_subagent(
        record=record,
        params=SpawnParams(task=record.task, label=record.label),
        cfg=LoopConfig(session_key=record.child_session_key),
        registry=ToolRegistry(),
        client=object(),
        transcript_writer=SimpleNamespace(path=tmp_path / "child.jsonl"),
        cancellation_token=runner._cancellation_tokens.setdefault(record.run_id, SimpleNamespace()),
        parent_on_event=events.append,
    ))

    progress_events = [event for event in events if isinstance(event, SubagentProgress)]
    activity_events = [event for event in events if isinstance(event, SubagentEvent)]
    assert len(progress_events) == 2
    assert progress_events[0].tool_uses == 1
    assert progress_events[0].current_activity == "Read"
    assert progress_events[-1].input_tokens == 1200
    assert progress_events[-1].output_tokens == 300
    assert len(activity_events) == 1
    assert activity_events[0].run_id == record.run_id
    assert activity_events[0].label == "child"
    assert isinstance(activity_events[0].event, ToolResult)
    assert result.input_tokens == 1200
    assert result.output_tokens == 300
