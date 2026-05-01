"""Anthropic Messages API transport.

Mirrors `src/agents/anthropic-transport-stream.ts` — take the SDK's raw
SSE events and translate them into our shared StreamEvent types.

Event mapping:
  content_block_start  (type=tool_use)        -> ToolUseStart
  content_block_start  (type=thinking)        -> (internal: init buf)
  content_block_start  (type=redacted_think.) -> ThinkingBlockComplete(redacted=True)
  content_block_delta  (type=text_delta)       -> TextDelta
  content_block_delta  (type=input_json_delta) -> ToolUseDelta
  content_block_delta  (type=thinking_delta)   -> ThinkingDelta + accumulate
  content_block_delta  (type=signature_delta)  -> (internal: accumulate sig)
  content_block_stop   (of a tool_use block)   -> ToolUseEnd
  content_block_stop   (of a thinking block)   -> ThinkingBlockComplete
  message_delta        (carries stop_reason)   -> buffered
  message_stop                                 -> MessageEnd
"""

from __future__ import annotations

from typing import Any, Iterator

from anthropic import Anthropic

from ._stream_events import (
    MessageEnd,
    StreamEvent,
    TextDelta,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
)


def stream_response(
    *,
    client: Anthropic,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int = 4096,
    thinking_budget_tokens: int | None = None,
) -> Iterator[StreamEvent]:
    request: dict[str, Any] = {
        "model": model,
        "system": system,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools:
        request["tools"] = tools

    if thinking_budget_tokens is not None:
        # Ensure max_tokens is large enough to hold thinking + some output tokens.
        request["max_tokens"] = max(max_tokens, thinking_budget_tokens + 1024)
        request["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget_tokens,
        }

    pending_stop_reason = "end_turn"
    pending_usage: dict[str, Any] = {}
    block_kinds: dict[int, str] = {}
    # Per thinking-block accumulator: index -> {thinking: str, signature: str}
    thinking_bufs: dict[int, dict[str, str]] = {}

    with client.messages.stream(**request) as stream:
        for event in stream:
            etype = event.type

            if etype == "content_block_start":
                block = event.content_block
                block_kinds[event.index] = block.type
                if block.type == "tool_use":
                    yield ToolUseStart(id=block.id, name=block.name)
                elif block.type == "thinking":
                    thinking_bufs[event.index] = {"thinking": "", "signature": ""}
                elif block.type == "redacted_thinking":
                    data = getattr(block, "data", "")
                    yield ThinkingBlockComplete(thinking="", signature=data, redacted=True)

            elif etype == "content_block_delta":
                delta = event.delta
                dtype = delta.type
                if dtype == "text_delta":
                    yield TextDelta(text=delta.text)
                elif dtype == "input_json_delta":
                    yield ToolUseDelta(partial_json=delta.partial_json)
                elif dtype == "thinking_delta":
                    text = getattr(delta, "thinking", "")
                    if event.index in thinking_bufs:
                        thinking_bufs[event.index]["thinking"] += text
                    yield ThinkingDelta(text=text)
                elif dtype == "signature_delta":
                    sig = getattr(delta, "signature", "")
                    if event.index in thinking_bufs:
                        thinking_bufs[event.index]["signature"] += sig

            elif etype == "content_block_stop":
                idx = event.index
                kind = block_kinds.get(idx)
                if kind == "tool_use":
                    yield ToolUseEnd()
                elif kind == "thinking" and idx in thinking_bufs:
                    buf = thinking_bufs.pop(idx)
                    yield ThinkingBlockComplete(
                        thinking=buf["thinking"],
                        signature=buf["signature"],
                    )

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
