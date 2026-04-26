"""Anthropic Messages API transport.

Mirrors `src/agents/anthropic-transport-stream.ts` — take the SDK's raw
SSE events and translate them into our 5 shared StreamEvent types.

Event mapping:
  content_block_start  (type=tool_use)        -> ToolUseStart
  content_block_delta  (type=text_delta)       -> TextDelta
  content_block_delta  (type=input_json_delta) -> ToolUseDelta
  content_block_stop   (of a tool_use block)   -> ToolUseEnd
  message_delta        (carries stop_reason)   -> buffered
  message_stop                                 -> MessageEnd
"""

from __future__ import annotations

from typing import Any, Iterator

from anthropic import Anthropic

from ._stream_events import MessageEnd, StreamEvent, TextDelta, ToolUseDelta, ToolUseEnd, ToolUseStart


def stream_response(
    *,
    client: Anthropic,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int = 4096,
) -> Iterator[StreamEvent]:
    request: dict[str, Any] = {
        "model": model,
        "system": system,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        request["tools"] = tools

    pending_stop_reason = "end_turn"
    pending_usage: dict[str, Any] = {}
    block_kinds: dict[int, str] = {}

    with client.messages.stream(**request) as stream:
        for event in stream:
            etype = event.type

            if etype == "content_block_start":
                block = event.content_block
                block_kinds[event.index] = block.type
                if block.type == "tool_use":
                    yield ToolUseStart(id=block.id, name=block.name)

            elif etype == "content_block_delta":
                delta = event.delta
                dtype = delta.type
                if dtype == "text_delta":
                    yield TextDelta(text=delta.text)
                elif dtype == "input_json_delta":
                    yield ToolUseDelta(partial_json=delta.partial_json)

            elif etype == "content_block_stop":
                if block_kinds.get(event.index) == "tool_use":
                    yield ToolUseEnd()

            elif etype == "message_delta":
                if event.delta.stop_reason is not None:
                    pending_stop_reason = event.delta.stop_reason
                if event.usage is not None:
                    pending_usage = {
                        "input_tokens": getattr(event.usage, "input_tokens", 0),
                        "output_tokens": getattr(event.usage, "output_tokens", 0),
                    }

            elif etype == "message_stop":
                yield MessageEnd(stop_reason=pending_stop_reason, usage=pending_usage)
