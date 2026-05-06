from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from nano_openclaw.loop import LoopConfig, Message, agent_loop
from nano_openclaw.provider import MessageEnd, TextDelta, ToolUseDelta, ToolUseEnd, ToolUseStart
from nano_openclaw.tools import ToolRegistry


async def _tool_use_batch_stream(tool_names: list[str]):
    for index, name in enumerate(tool_names, start=1):
        tool_id = f"tool-{index}"
        yield ToolUseStart(id=tool_id, name=name)
        yield ToolUseDelta(id=tool_id, partial_json=json.dumps({"value": index}))
        yield ToolUseEnd(id=tool_id)
    yield MessageEnd(stop_reason="tool_use", usage={})


async def _conclusion_stream():
    yield TextDelta(text="I cannot proceed as the tool call was denied.")
    yield MessageEnd(stop_reason="end_turn", usage={})


def test_denied_approval_skips_later_tools_in_same_batch(monkeypatch):
    registry = ToolRegistry()
    dispatches: list[str] = []

    class ApprovalManager:
        def check_request(self, _name, _args):
            return SimpleNamespace(requires_approval=True)

    registry.approval_manager = ApprovalManager()  # type: ignore[assignment]

    call_streams = [
        _tool_use_batch_stream(["needs_approval", "side_effect"]),
        _conclusion_stream(),
    ]
    stream_iter = iter(call_streams)

    async def fake_stream_response(**_kwargs):
        async for event in next(stream_iter):
            yield event

    async def fake_dispatch(self, tool_use_id, name, args, cancellation_token=None):
        dispatches.append(name)
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": True,
            "_denied": True,
            "content": [{"type": "text", "text": f"approval denied for {name}"}],
        }

    monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)
    monkeypatch.setattr(ToolRegistry, "dispatch", fake_dispatch)

    history: list[Message] = []
    asyncio.run(agent_loop(
        user_input="run tools",
        history=history,
        registry=registry,
        on_event=lambda _event: None,
        client=object(),
        cfg=LoopConfig(),
    ))

    assert dispatches == ["needs_approval"]
    # Tool results are now the second-to-last message; last is the conclusion.
    tool_results = history[-2].content
    assert len(tool_results) == 2
    assert "approval denied for needs_approval" in tool_results[0]["content"][0]["text"]
    assert "skipped because approval was denied for needs_approval" in tool_results[1]["content"][0]["text"]
    assert all("_denied" not in result for result in tool_results)
    # Final message is the model's conclusion after denial.
    conclusion = history[-1]
    assert conclusion.role == "assistant"
    assert any(b.get("type") == "text" for b in conclusion.content)


def test_denial_markers_are_stripped_from_all_tool_results(monkeypatch):
    registry = ToolRegistry()

    call_streams = [
        _tool_use_batch_stream(["first", "second"]),
        _conclusion_stream(),
    ]
    stream_iter = iter(call_streams)

    async def fake_stream_response(**_kwargs):
        async for event in next(stream_iter):
            yield event

    async def fake_dispatch(self, tool_use_id, name, args, cancellation_token=None):
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "is_error": True,
            "_denied": True,
            "content": [{"type": "text", "text": f"approval denied for {name}"}],
        }

    monkeypatch.setattr("nano_openclaw.loop.stream_response", fake_stream_response)
    monkeypatch.setattr(ToolRegistry, "dispatch", fake_dispatch)

    history: list[Message] = []
    asyncio.run(agent_loop(
        user_input="run tools",
        history=history,
        registry=registry,
        on_event=lambda _event: None,
        client=object(),
        cfg=LoopConfig(),
    ))

    # Tool results are the second-to-last message; last is the conclusion.
    tool_results = history[-2].content
    assert len(tool_results) == 2
    assert all("_denied" not in result for result in tool_results)
    # Final message is the model's conclusion after denial.
    assert history[-1].role == "assistant"
