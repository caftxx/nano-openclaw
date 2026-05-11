"""OpenAI Chat Completions API transport.

Mirrors the role of `src/agents/openai-transport-stream.ts` — translate
OpenAI's streaming chunks into the same StreamEvent types that
_provider_anthropic produces, so loop.py stays provider-agnostic.

Message format translation (Anthropic internal → OpenAI wire):
  history is stored in Anthropic format (text/tool_use/tool_result blocks);
  this module converts to OpenAI format before sending.
  thinking/redacted_thinking blocks in history are skipped — OpenAI format
  does not support them and they are not needed for context.

Tool schema translation (Anthropic → OpenAI):
  Anthropic: {"name": ..., "description": ..., "input_schema": {...}}
  OpenAI:    {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}

Tool result mapping (Anthropic → OpenAI roles):
  A single Anthropic user message holding N tool_result blocks becomes N
  separate {"role": "tool", ...} messages — OpenAI requires one per call.

Stop reason mapping:
  finish_reason "stop"       -> stop_reason "end_turn"
  finish_reason "tool_calls" -> stop_reason "tool_use"
  finish_reason "length"     -> stop_reason "max_tokens"

Extended thinking (OpenAI-compatible providers):
  thinking_budget_tokens is passed via extra_body={"thinking": {...}}.
  Streaming thinking text arrives in delta.reasoning_content (non-standard
  field used by many compatible providers); yielded as ThinkingDelta events.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

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


async def stream_response(
    *,
    client: Any,  # openai.AsyncOpenAI — typed as Any to avoid hard import at module level
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int = 4096,
    thinking_budget_tokens: int | None = None,
) -> AsyncIterator[StreamEvent]:
    oai_messages = [{"role": "system", "content": system}] + _to_openai_messages(messages)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": oai_messages,
        "max_tokens": max_tokens,
        "stream": True,
        # Ask the OpenAI-compatible provider to send a final usage chunk so
        # compact_if_needed can use real prompt_tokens for the trigger
        # decision instead of the character-based estimate.
        "stream_options": {"include_usage": True},
    }
    if tools:
        kwargs["tools"] = _to_openai_tools(tools)

    if thinking_budget_tokens is not None:
        kwargs["extra_body"] = {
            "thinking": {"type": "enabled", "budget_tokens": thinking_budget_tokens}
        }

    pending_stop_reason = "end_turn"
    pending_usage: dict[str, Any] = {}
    tool_ids_by_index: dict[int, str] = {}
    started_tool_indices: set[int] = set()
    thinking_buf = ""

    response = await client.chat.completions.create(**kwargs)
    async for chunk in response:
        # The final chunk emitted under stream_options.include_usage carries
        # usage on the chunk itself with empty choices — capture it before
        # the empty-choices guard skips the chunk.
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            pending_usage = {
                "input_tokens": getattr(chunk_usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(chunk_usage, "completion_tokens", 0) or 0,
            }
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta

        # reasoning_content is a non-standard field used by many OpenAI-compatible
        # providers to stream thinking/reasoning text.
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            thinking_buf += rc
            yield ThinkingDelta(text=rc)

        if delta.content:
            yield TextDelta(text=delta.content)

        if delta.tool_calls:
            for tc in delta.tool_calls:
                tool_id = tool_ids_by_index.get(tc.index)
                if not tool_id:
                    tool_id = tc.id or f"tool-call-{tc.index}"
                    tool_ids_by_index[tc.index] = tool_id
                if tc.index not in started_tool_indices:
                    started_tool_indices.add(tc.index)
                    yield ToolUseStart(id=tool_id, name=(tc.function.name or "") if tc.function else "")
                if tc.function and tc.function.arguments:
                    yield ToolUseDelta(id=tool_id, partial_json=tc.function.arguments)

        fr = choice.finish_reason
        if fr is not None:
            if thinking_buf:
                yield ThinkingBlockComplete(thinking=thinking_buf, signature="")
                thinking_buf = ""
            if fr == "tool_calls":
                for idx in sorted(started_tool_indices):
                    yield ToolUseEnd(id=tool_ids_by_index[idx])
                pending_stop_reason = "tool_use"
            elif fr == "stop":
                pending_stop_reason = "end_turn"
            elif fr == "length":
                pending_stop_reason = "max_tokens"

    yield MessageEnd(stop_reason=pending_stop_reason, usage=pending_usage)


# ---------------------------------------------------------------------------
# Format translation helpers
# ---------------------------------------------------------------------------


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic message list (stored in history) to OpenAI format."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        content: list[dict[str, Any]] = msg["content"]

        if role == "user":
            text_parts = [c for c in content if c.get("type") == "text"]
            image_parts = [c for c in content if c.get("type") == "image"]
            tool_results = [c for c in content if c.get("type") == "tool_result"]

            if image_parts:
                # Native Vision path: convert Anthropic image blocks to OpenAI image_url format.
                oai_content: list[dict[str, Any]] = []
                for c in content:
                    if c.get("type") == "text":
                        oai_content.append({"type": "text", "text": c["text"]})
                    elif c.get("type") == "image":
                        src = c["source"]
                        data_url = f"data:{src['media_type']};base64,{src['data']}"
                        oai_content.append({"type": "image_url", "image_url": {"url": data_url}})
                if oai_content:
                    result.append({"role": "user", "content": oai_content})
            elif text_parts:
                text = " ".join(p["text"] for p in text_parts)
                result.append({"role": "user", "content": text})

            # Each tool_result becomes a separate "tool" role message.
            for tr in tool_results:
                text_content = ""
                if tr.get("content"):
                    text_content = tr["content"][0].get("text", "")
                result.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_use_id"],
                    "content": text_content,
                })

        elif role == "assistant":
            # thinking/redacted_thinking blocks are skipped — not valid in OpenAI format.
            text_parts = [c for c in content if c.get("type") == "text"]
            tool_uses = [c for c in content if c.get("type") == "tool_use"]

            oai_msg: dict[str, Any] = {"role": "assistant"}
            text = "".join(p["text"] for p in text_parts).strip()
            oai_msg["content"] = text or None

            if tool_uses:
                oai_msg["tool_calls"] = [
                    {
                        "id": tu["id"],
                        "type": "function",
                        "function": {
                            "name": tu["name"],
                            "arguments": json.dumps(tu["input"], ensure_ascii=False),
                        },
                    }
                    for tu in tool_uses
                ]

            result.append(oai_msg)

    return result


def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool schema list to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]
