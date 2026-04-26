"""Anthropic streaming provider, normalized to a tiny event vocabulary.

Mirrors `src/agents/anthropic-transport-stream.ts` (around line 742): take
the SDK's raw SSE events and translate them into our 5 dataclasses so the
rest of nano-openclaw never touches the SDK directly. This is the only
file in the project that knows about Anthropic specifically — swap it
out and you've ported nano-openclaw to a different provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Union

from anthropic import Anthropic


# ---- Normalized stream events -------------------------------------------------


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUseStart:
    id: str
    name: str


@dataclass
class ToolUseDelta:
    partial_json: str


@dataclass
class ToolUseEnd:
    pass


@dataclass
class MessageEnd:
    stop_reason: str
    usage: dict[str, Any]


StreamEvent = Union[TextDelta, ToolUseStart, ToolUseDelta, ToolUseEnd, MessageEnd]


# ---- Stream wrapper -----------------------------------------------------------


def stream_response(
    *,
    client: Anthropic,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int = 4096,
) -> Iterator[StreamEvent]:
    """Yield normalized events from an Anthropic streaming completion.

    Event mapping (Anthropic SDK -> nano):

    - ``content_block_start`` with type=tool_use   -> ToolUseStart
    - ``content_block_delta`` with type=text_delta -> TextDelta
    - ``content_block_delta`` with type=input_json_delta -> ToolUseDelta
    - ``content_block_stop`` of a tool_use block   -> ToolUseEnd
    - ``message_delta`` (carries stop_reason)      -> buffered
    - ``message_stop``                              -> MessageEnd(stop_reason, usage)
    """
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
