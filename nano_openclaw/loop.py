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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from nano_openclaw.compact import compact_if_needed
from nano_openclaw.images import describe_image, load_image, parse_image_refs, to_anthropic_image_block
from nano_openclaw.prompt import build_system_prompt
from nano_openclaw.provider import (
    MessageEnd,
    StreamEvent,
    TextDelta,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
    stream_response,
)
from nano_openclaw.tools import ToolRegistry
from nano_openclaw.workspace import WorkspaceBootstrapFile, get_or_load_bootstrap_files

if TYPE_CHECKING:
    from nano_openclaw.session import TranscriptWriter

EventCallback = Callable[[Any], None]

# Thinking level type (mirrors openclaw ThinkLevel)
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"]

# Thinking budget mapping (mirrors openclaw anthropic-transport-stream.ts)
THINKING_BUDGETS: dict[ThinkingLevel, int] = {
    "off": 0,
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
    "adaptive": 8192,  # adaptive uses medium budget as baseline
    "max": 32768,
}


@dataclass
class Message:
    role: Literal["user", "assistant"]
    content: list[dict[str, Any]]


@dataclass
class LoopConfig:
    model: str = "claude-sonnet-4-5-20250929"
    api: str = "anthropic"   # mirrors OpenClaw's model.api field
    base_url: str | None = None  # mirrors OpenClaw's models.providers.*.baseUrl
    model_input: list[str] = ("text",)  # mirrors OpenClaw's model.input field
    max_iterations: int = 12
    max_tokens: int = 4096
    # Context compaction settings (mirrors OpenClaw's compaction config)
    context_budget: int = 100000  # Maximum token budget for context
    context_threshold: float = 0.8  # Trigger compaction at 80% of budget
    context_recent_turns: int = 3  # Number of recent turns to preserve
    # Image model (mirrors openclaw agents.defaults.imageModel)
    # None  → Native Vision: images sent as base64 blocks to main model (runner.ts:819-857)
    # str   → Media Understanding: images described to text by this model (apply.ts)
    image_model: str | None = None
    # Thinking level (mirrors openclaw agents.defaults.thinkingDefault)
    # When "off", no thinking blocks are requested.
    # When non-off, thinking is enabled with budget derived from level.
    thinking_level: ThinkingLevel = "off"
    # Workspace bootstrap (mirrors openclaw workspace bootstrap injection)
    workspace_dir: Path | None = None  # Path to workspace directory
    session_key: str = "default"  # Session identifier for caching
    bootstrap_max_chars: int = 12000  # Per-file character budget
    bootstrap_total_max_chars: int = 60000  # Total character budget

    @property
    def model_has_vision(self) -> bool:
        return "image" in self.model_input
    
    @property
    def thinking_budget_tokens(self) -> int | None:
        """Convert thinking level to budget tokens.
        Returns 0 to explicitly disable thinking, >0 to enable, None if level unknown."""
        return THINKING_BUDGETS.get(self.thinking_level)


def agent_loop(
    user_input: str,
    history: list[Message],
    registry: ToolRegistry,
    on_event: EventCallback,
    *,
    client: Any,  # anthropic.Anthropic | openai.OpenAI
    cfg: LoopConfig,
    transcript_writer: "TranscriptWriter | None" = None,
) -> list[Message]:
    """Drive one user turn to completion (possibly through many tool rounds).

    ``history`` is mutated in place AND returned for convenience. The caller
    keeps the same list across turns to maintain conversation state.
    ``on_event`` receives every streaming event and every ``("ToolResult", ...)``
    notification; the CLI uses it for live rendering.
    """
    # Parse image references from user input (mirrors openclaw attempt.ts detectAndLoadPromptImages)
    cleaned_text, image_refs = parse_image_refs(user_input)

    content: list[dict[str, Any]] = []
    loaded_refs: list[str] = []
    for ref in image_refs:
        try:
            b64, mime = load_image(ref)
            if cfg.image_model:
                # Media Understanding path (openclaw: imageModel configured → apply.ts)
                # Image model describes the image; main model receives text, not pixels.
                on_event(("ImageDescribe", ref))
                desc = describe_image(b64, mime, client=client, model=cfg.image_model, api=cfg.api)
                content.append({"type": "text", "text": f"[Image: {desc}]"})
            elif cfg.model_has_vision:
                # Native Vision path (openclaw: main model supports vision → attempt.ts:2648-2654)
                # Image sent as base64 block directly to the main model.
                content.append(to_anthropic_image_block(b64, mime))
            else:
                # Main model has no vision AND no image_model configured → skip image.
                on_event(("ImageSkip", ref, "model has no vision capability and no image_model configured"))
            loaded_refs.append(ref)
        except Exception as exc:
            on_event(("ImageError", ref, str(exc)))

    if loaded_refs:
        on_event(("ImageAttached", loaded_refs, bool(cfg.image_model)))

    # Mirror openclaw convertContentBlocks: guarantee at least one text block.
    if cleaned_text:
        content.append({"type": "text", "text": cleaned_text})
    if not content:
        content.append({"type": "text", "text": user_input})
    elif not any(b.get("type") == "text" for b in content):
        content.append({"type": "text", "text": "(see attached image)"})

    history.append(Message("user", content))
    if transcript_writer:
        transcript_writer.append_message(history[-1])

    # Load workspace bootstrap files (AGENTS.md, SOUL.md, etc.) for prompt injection
    bootstrap_files: list[WorkspaceBootstrapFile] | None = None
    if cfg.workspace_dir:
        bootstrap_files = get_or_load_bootstrap_files(
            cfg.workspace_dir,
            cfg.session_key,
            cfg.bootstrap_max_chars,
            cfg.bootstrap_total_max_chars,
        )

    system = build_system_prompt(registry, cfg.workspace_dir, bootstrap_files)
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
            if transcript_writer:
                transcript_writer.append_compaction(summary)
        wire_messages = [{"role": m.role, "content": m.content} for m in history]

        assistant_blocks, stop_reason = _consume_one_assistant_turn(
            client=client,
            api=cfg.api,
            model=cfg.model,
            system=system,
            messages=wire_messages,
            tools=tools_schema,
            max_tokens=cfg.max_tokens,
            thinking_budget_tokens=cfg.thinking_budget_tokens,
            on_event=on_event,
        )

        history.append(Message("assistant", assistant_blocks))
        if transcript_writer:
            transcript_writer.append_message(history[-1])

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
        if transcript_writer:
            transcript_writer.append_message(history[-1])
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
    thinking_budget_tokens: int | None,
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
        thinking_budget_tokens=thinking_budget_tokens,
    ):
        on_event(ev)

        if isinstance(ev, ThinkingDelta):
            pass  # display only — ThinkingBlockComplete carries the full content

        elif isinstance(ev, ThinkingBlockComplete):
            _flush_text()
            if ev.redacted:
                blocks.append({"type": "redacted_thinking", "data": ev.signature})
            else:
                blocks.append({"type": "thinking", "thinking": ev.thinking, "signature": ev.signature})

        elif isinstance(ev, TextDelta):
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
