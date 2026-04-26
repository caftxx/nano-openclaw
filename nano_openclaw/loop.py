"""The agent loop — nano-openclaw's spine.

Mirrors `src/agents/pi-embedded-runner/run/attempt.ts:566` (`runEmbeddedAttempt`).
Production OpenClaw drives this loop via a pi-agent-core session subscription;
underneath, the conceptual cycle is identical and just three rules:

  1.  Send the entire history (incl. system prompt + tools) to the model.
  2.  Accumulate one assistant message from the streamed events.
  3.  If stop_reason == "tool_use": dispatch every tool_use block, package
      results as a single user message, loop. Otherwise: done.

That's it. Read this file top-to-bottom and you understand the whole thing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from nano_openclaw.compact import compact_if_needed
from nano_openclaw.prompt import build_system_prompt
from nano_openclaw.provider import (
    MessageEnd,
    StreamEvent,
    TextDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
    stream_response,
)
from nano_openclaw.tools import ToolRegistry

EventCallback = Callable[[Any], None]


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: list[dict[str, Any]]


@dataclass
class LoopConfig:
    model: str = "claude-sonnet-4-5-20250929"
    api: str = "anthropic"   # mirrors OpenClaw's model.api field
    max_iterations: int = 12
    max_tokens: int = 4096
    # Context compaction settings (mirrors OpenClaw's compaction config)
    context_budget: int = 100000  # Maximum token budget for context
    context_threshold: float = 0.8  # Trigger compaction at 80% of budget
    context_recent_turns: int = 3  # Number of recent turns to preserve


def agent_loop(
    user_input: str,
    history: list[Message],
    registry: ToolRegistry,
    on_event: EventCallback,
    *,
    client: Any,  # anthropic.Anthropic | openai.OpenAI
    cfg: LoopConfig,
) -> list[Message]:
    """Drive one user turn to completion (possibly through many tool rounds).

    ``history`` is mutated in place AND returned for convenience. The caller
    keeps the same list across turns to maintain conversation state.
    ``on_event`` receives every streaming event and every ``("ToolResult", ...)``
    notification; the CLI uses it for live rendering.
    """
    history.append(Message("user", [{"type": "text", "text": user_input}]))

    system = build_system_prompt(registry)
    tools_schema = registry.schemas()

    for _ in range(cfg.max_iterations):
        # Check context budget and compact if needed (mirrors OpenClaw's compaction)
        _, summary = compact_if_needed(
            history,
            budget=cfg.context_budget,
            client=client,
            model=cfg.model,
            api=cfg.api,
            threshold_ratio=cfg.context_threshold,
            recent_turns=cfg.context_recent_turns,
        )
        if summary:
            on_event(("Compaction", summary))
        wire_messages = [{"role": m.role, "content": m.content} for m in history]

        assistant_blocks, stop_reason = _consume_one_assistant_turn(
            client=client,
            api=cfg.api,
            model=cfg.model,
            system=system,
            messages=wire_messages,
            tools=tools_schema,
            max_tokens=cfg.max_tokens,
            on_event=on_event,
        )

        history.append(Message("assistant", assistant_blocks))

        if stop_reason != "tool_use":
            return history  # end_turn / max_tokens / stop_sequence — terminal

        # Dispatch every tool_use; package all results into ONE user message.
        tool_results: list[dict[str, Any]] = []
        for block in assistant_blocks:
            if block.get("type") != "tool_use":
                continue
            result = registry.dispatch(block["id"], block["name"], block.get("input") or {})
            tool_results.append(result)
            on_event(("ToolResult", block["name"], block.get("input") or {}, result))

        history.append(Message("user", tool_results))
        # next iteration sends history (now including tool_results) back to the model

    history.append(
        Message("assistant", [{"type": "text", "text": "[max_iterations reached]"}])
    )
    return history


def _consume_one_assistant_turn(
    *,
    client: Any,
    api: str,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
    on_event: EventCallback,
) -> tuple[list[dict[str, Any]], str | None]:
    """Stream one model response, accumulating mixed text + tool_use blocks."""
    blocks: list[dict[str, Any]] = []
    text_buf = ""
    cur_tool: dict[str, Any] | None = None
    stop_reason: str | None = None

    def _flush_text():
        nonlocal text_buf
        if text_buf:
            blocks.append({"type": "text", "text": text_buf})
            text_buf = ""

    for ev in stream_response(
        api=api,
        client=client,
        model=model,
        system=system,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
    ):
        on_event(ev)

        if isinstance(ev, TextDelta):
            text_buf += ev.text

        elif isinstance(ev, ToolUseStart):
            _flush_text()
            cur_tool = {"id": ev.id, "name": ev.name, "buf": ""}

        elif isinstance(ev, ToolUseDelta):
            if cur_tool is not None:
                cur_tool["buf"] += ev.partial_json

        elif isinstance(ev, ToolUseEnd):
            if cur_tool is not None:
                args = json.loads(cur_tool["buf"] or "{}")
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": cur_tool["id"],
                        "name": cur_tool["name"],
                        "input": args,
                    }
                )
                cur_tool = None

        elif isinstance(ev, MessageEnd):
            _flush_text()
            stop_reason = ev.stop_reason

    return blocks, stop_reason
