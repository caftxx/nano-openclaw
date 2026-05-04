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

import asyncio
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from nano_openclaw.compact import compact_if_needed, estimate_tokens
from nano_openclaw.images import describe_image, load_image, parse_image_refs, to_anthropic_image_block
from nano_openclaw.memory.active import ActiveMemoryConfig, ActiveMemoryManager, ActiveMemoryResult
from nano_openclaw.memory.dreaming import DreamingConfig
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
from nano_openclaw.skills import (
    Skill,
    SkillEntry,
    build_skill_registry_from_entries,
    build_slash_command_context,
    filter_eligible_skills,
    filter_visible_skills,
    get_or_load_skills,
    parse_slash_command,
    SlashCommand,
)
from nano_openclaw.tools import ToolRegistry
from nano_openclaw.workspace import WorkspaceBootstrapFile, get_or_load_bootstrap_files

if TYPE_CHECKING:
    from nano_openclaw.plugins.registry import HookRegistry
    from nano_openclaw.session import TranscriptWriter

EventCallback = Callable[[Any], None]


class TurnCancelled(Exception):
    """Raised when the current user turn is cancelled by the operator."""


@dataclass
class CancellationToken:
    _cancelled: Event = field(default_factory=Event)
    _input_pause_requested: Event = field(default_factory=Event)
    _input_pause_ack: Event = field(default_factory=Event)

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @contextmanager
    def pause_input_capture(self):
        """Temporarily pause background key capture so foreground prompts can read stdin."""
        self._input_pause_requested.set()
        self._input_pause_ack.wait(timeout=0.2)
        try:
            yield
        finally:
            self._input_pause_requested.clear()
            self._input_pause_ack.clear()


def _check_cancelled(token: "CancellationToken | None") -> None:
    if token and token.is_cancelled:
        raise TurnCancelled()


@dataclass
class ToolResult:
    tool_use_id: str
    name: str
    args: dict[str, Any]
    result: dict[str, Any]


@dataclass
class Compaction:
    summary: str


@dataclass
class ActiveMemoryRecall:
    result: ActiveMemoryResult


@dataclass
class ImageDescribe:
    ref: str


@dataclass
class ImageAttached:
    refs: list[str]
    via_model: bool


@dataclass
class ImageError:
    ref: str
    error: str


@dataclass
class ImageSkip:
    ref: str
    reason: str


@dataclass
class SkillInvoked:
    skill_name: str
    skill_path: str


@dataclass
class SubagentSpawned:
    """Event when a subagent is spawned."""
    run_id: str
    task: str
    label: Optional[str] = None
    model: Optional[str] = None


@dataclass
class SubagentAnnounced:
    """Event when a subagent completes and announces result."""
    run_id: str
    status: str
    task: str
    result_text: Optional[str] = None
    elapsed_ms: Optional[int] = None
    error_message: Optional[str] = None


@dataclass
class SubagentKilled:
    """Event when a subagent is killed."""
    run_id: str
    task: str


@dataclass
class SubagentProgress:
    """Event emitted during subagent execution with live progress."""
    run_id: str
    label: str
    tool_uses: int
    input_tokens: int
    output_tokens: int
    current_activity: str


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
    # Skills configuration (mirrors openclaw skills.*)
    skill_filter: list[str] | None = None  # Agent skill allowlist
    extra_skill_dirs: list[str] | None = None  # Extra skill directories
    max_skill_file_bytes: int = 256_000  # Max bytes per SKILL.md
    max_skills_in_prompt: int = 150  # Max skills in prompt
    max_skills_prompt_chars: int = 18_000  # Max chars for skills section
    # Active Memory configuration (mirrors openclaw active-memory plugin)
    active_memory_config: ActiveMemoryConfig | None = None  # None = disabled
    # Dreaming configuration (mirrors openclaw memory-core dreaming)
    dreaming_config: DreamingConfig | None = None  # None = disabled
    # If set, bypasses build_system_prompt() entirely (used by subagent runner)
    system_prompt_override: str | None = None
    # Lightweight plugin hooks, installed by the plugin loader.
    hook_registry: "HookRegistry | None" = None

    @property
    def model_has_vision(self) -> bool:
        return "image" in self.model_input
    
    @property
    def thinking_budget_tokens(self) -> int | None:
        """Convert thinking level to budget tokens.
        Returns 0 to explicitly disable thinking, >0 to enable, None if level unknown."""
        return THINKING_BUDGETS.get(self.thinking_level)


async def agent_loop(
    user_input: str,
    history: list[Message],
    registry: ToolRegistry,
    on_event: EventCallback,
    *,
    client: Any,  # anthropic.AsyncAnthropic | openai.AsyncOpenAI
    cfg: LoopConfig,
    transcript_writer: "TranscriptWriter | None" = None,
    cancellation_token: "CancellationToken | None" = None,
) -> list[Message]:
    """Drive one user turn to completion (possibly through many tool rounds).

    ``history`` is mutated in place AND returned for convenience. The caller
    keeps the same list across turns to maintain conversation state.
    ``on_event`` receives every streaming event and every loop event (``ToolResult``,
    ``Compaction``, ``ImageAttached``, etc.); the CLI uses it for live rendering.
    Skills are loaded from cfg each turn (cached per session) and used for
    both slash command dispatch and system prompt injection.
    """
    # The turn is built against a scratch history and committed only on success.
    scratch_history = list(history)
    pending_transcript_ops: list[tuple[str, Message | str]] = []
    loop_event_tasks: list[asyncio.Task] = []
    original_on_event = on_event

    def hooked_on_event(event: Any) -> None:
        original_on_event(event)
        if cfg.hook_registry:
            loop_event_tasks.append(
                asyncio.create_task(cfg.hook_registry.run("on_loop_event", {"event": event}))
            )

    async def drain_loop_event_hooks() -> None:
        if loop_event_tasks:
            await asyncio.gather(*loop_event_tasks, return_exceptions=True)
            loop_event_tasks.clear()

    async def check_cancelled() -> None:
        try:
            _check_cancelled(cancellation_token)
        except TurnCancelled:
            await drain_loop_event_hooks()
            raise

    on_event = hooked_on_event

    await check_cancelled()

    # 1. Load skills early (needed for slash commands + prompt injection)
    eligible_entries: list[SkillEntry] = []
    visible_skills: list[Skill] | None = None
    if cfg.workspace_dir:
        skill_entries = get_or_load_skills(
            cfg.workspace_dir,
            cfg.session_key,
            extra_dirs=cfg.extra_skill_dirs,
            max_bytes=cfg.max_skill_file_bytes,
        )
        if skill_entries:
            eligible_entries = filter_eligible_skills(skill_entries, skill_filter=cfg.skill_filter)
            visible_skills = filter_visible_skills(eligible_entries)

    # 2. Parse slash command (mirrors openclaw slash-commands.md)
    command: SlashCommand | None = None
    remaining_text = user_input
    skill_registry: dict[str, Skill] = {}
    if eligible_entries:
        # Slash commands: user-invocable skills only
        runtime_registry = build_skill_registry_from_entries(eligible_entries)
        if runtime_registry:
            skill_registry = runtime_registry
        command, remaining_text = parse_slash_command(user_input, skill_registry)
        # Model Skill tool: all eligible skills, not just user-invocable ones.
        # user-invocable controls slash command access; disable-model-invocation
        # controls model access. These are separate gates.
        model_registry = build_skill_registry_from_entries(eligible_entries, user_invocable_only=False)
        registry.set_eligible_skills(model_registry)

    # 3. Build message content
    content: list[dict[str, Any]] = []
    
    # Slash command invocation: inject skill content
    if command:
        skill_context = build_slash_command_context(command)
        content.append({"type": "text", "text": skill_context})
        on_event(SkillInvoked(skill_name=command.name, skill_path=command.skill.filePath))
    
    # 3. Parse image references from remaining text (mirrors openclaw attempt.ts detectAndLoadPromptImages)
    cleaned_text, image_refs = parse_image_refs(remaining_text)
    loaded_refs: list[str] = []
    for ref in image_refs:
        try:
            b64, mime = load_image(ref)
            if cfg.image_model:
                # Media Understanding path (openclaw: imageModel configured → apply.ts)
                # Image model describes the image; main model receives text, not pixels.
                on_event(ImageDescribe(ref=ref))
                desc = await describe_image(b64, mime, client=client, model=cfg.image_model, api=cfg.api)
                content.append({"type": "text", "text": f"[Image: {desc}]"})
            elif cfg.model_has_vision:
                # Native Vision path (openclaw: main model supports vision → attempt.ts:2648-2654)
                # Image sent as base64 block directly to the main model.
                content.append(to_anthropic_image_block(b64, mime))
            else:
                # Main model has no vision AND no image_model configured → skip image.
                on_event(ImageSkip(ref=ref, reason="model has no vision capability and no image_model configured"))
            loaded_refs.append(ref)
        except Exception as exc:
            on_event(ImageError(ref=ref, error=str(exc)))

    if loaded_refs:
        on_event(ImageAttached(refs=loaded_refs, via_model=bool(cfg.image_model)))

    # Mirror openclaw convertContentBlocks: guarantee at least one text block.
    if cleaned_text:
        content.append({"type": "text", "text": cleaned_text})
    if not content:
        content.append({"type": "text", "text": user_input})
    elif not any(b.get("type") == "text" for b in content):
        content.append({"type": "text", "text": "(see attached image)"})

    scratch_history.append(Message("user", content))
    pending_transcript_ops.append(("message", scratch_history[-1]))

    # Load workspace bootstrap files (AGENTS.md, SOUL.md, etc.) for prompt injection
    bootstrap_files: list[WorkspaceBootstrapFile] | None = None
    if cfg.workspace_dir:
        bootstrap_files = get_or_load_bootstrap_files(
            cfg.workspace_dir,
            cfg.session_key,
            cfg.bootstrap_max_chars,
            cfg.bootstrap_total_max_chars,
        )

    # Run Active Memory recall before building system prompt
    active_memory_context: str | None = None
    if cfg.workspace_dir and cfg.active_memory_config and cfg.active_memory_config.enabled:
        manager = ActiveMemoryManager(
            client=client,
            model=cfg.model,
            workspace_dir=str(cfg.workspace_dir),
            config=cfg.active_memory_config,
        )
        wire_messages = [{"role": m.role, "content": m.content} for m in scratch_history]
        recall_result = await manager.run(wire_messages)
        if recall_result:
            on_event(ActiveMemoryRecall(result=recall_result))
            if recall_result.context:
                active_memory_context = recall_result.context

    if cfg.system_prompt_override is not None:
        system = cfg.system_prompt_override
    else:
        system = build_system_prompt(
            registry,
            cfg.workspace_dir,
            bootstrap_files,
            visible_skills,
            max_skills_in_prompt=cfg.max_skills_in_prompt,
            max_skills_prompt_chars=cfg.max_skills_prompt_chars,
        )

    if cfg.hook_registry:
        hook_result = await cfg.hook_registry.run("before_prompt_build", {
            "system": system,
            "user_input": user_input,
            "workspace_dir": str(cfg.workspace_dir) if cfg.workspace_dir else "",
        })
        system = hook_result.get("system", system)
        if prepend := hook_result.get("prepend"):
            system = f"{prepend}\n\n{system}"
        if append := hook_result.get("append"):
            system = f"{system}\n\n{append}"

    # Inject Active Memory after prompt hooks so custom system replacements do
    # not accidentally discard recalled context.
    if active_memory_context:
        system = f"{active_memory_context}\n\n{system}"

    tools_schema = registry.schemas()

    for _ in range(cfg.max_iterations):
        await check_cancelled()
        # Check context budget and compact if needed (mirrors OpenClaw's compaction)
        _, summary = await compact_if_needed(
            scratch_history,
            budget=cfg.context_budget,
            client=client,
            model=cfg.model,
            api=cfg.api,
            threshold_ratio=cfg.context_threshold,
            recent_turns=cfg.context_recent_turns,
        )
        if summary:
            on_event(Compaction(summary=summary))
            pending_transcript_ops.append(("compaction", summary))
        wire_messages = [{"role": m.role, "content": m.content} for m in scratch_history]

        try:
            assistant_blocks, stop_reason = await _consume_one_assistant_turn(
                client=client,
                api=cfg.api,
                model=cfg.model,
                system=system,
                messages=wire_messages,
                tools=tools_schema,
                max_tokens=cfg.max_tokens,
                thinking_budget_tokens=cfg.thinking_budget_tokens,
                on_event=on_event,
                cancellation_token=cancellation_token,
            )
        except TurnCancelled:
            await drain_loop_event_hooks()
            raise

        await check_cancelled()
        scratch_history.append(Message("assistant", assistant_blocks))
        pending_transcript_ops.append(("message", scratch_history[-1]))

        if stop_reason != "tool_use":
            history[:] = scratch_history
            if transcript_writer:
                for op, payload in pending_transcript_ops:
                    if op == "message":
                        transcript_writer.append_message(payload)  # type: ignore[arg-type]
                    else:
                        transcript_writer.append_compaction(payload)  # type: ignore[arg-type]
            await drain_loop_event_hooks()
            return history  # end_turn / max_tokens / stop_sequence — terminal

        # Dispatch every tool_use; package all results into ONE user message.
        registry.set_session_status_context(
            model=cfg.model,
            session_id=transcript_writer.session_id if transcript_writer else "",
            context_budget=cfg.context_budget,
            current_tokens=estimate_tokens(scratch_history),
            compaction_count=transcript_writer.compaction_count if transcript_writer else 0,
            message_count=len(scratch_history),
        )
        tool_use_blocks = [b for b in assistant_blocks if b.get("type") == "tool_use"]

        # Emit SkillInvoked events before dispatch (synchronous, order matters for UX)
        for block in tool_use_blocks:
            tool_name = block["name"]
            tool_args = block.get("input") or {}
            if tool_name == "Skill" and "skill" in tool_args:
                skill_name = tool_args["skill"]
                if skill_registry and skill_name in skill_registry:
                    skill = skill_registry[skill_name]
                    on_event(SkillInvoked(skill_name=skill_name, skill_path=skill.filePath))

        await check_cancelled()

        # Dispatch all tool calls in parallel — core benefit of async model.
        tool_results: list[dict[str, Any]] = list(
            await asyncio.gather(*[
                registry.dispatch(
                    b["id"],
                    b["name"],
                    b.get("input") or {},
                    cancellation_token=cancellation_token,
                )
                for b in tool_use_blocks
            ])
        )

        await check_cancelled()
        # Strip denial markers before persisting (keeps history clean for the API).
        has_denial = any(r.pop("_denied", False) for r in tool_results)
        for block, result in zip(tool_use_blocks, tool_results):
            on_event(ToolResult(
                tool_use_id=block["id"],
                name=block["name"],
                args=block.get("input") or {},
                result=result,
            ))

        scratch_history.append(Message("user", tool_results))
        pending_transcript_ops.append(("message", scratch_history[-1]))

        if has_denial:
            # User denied at least one tool — stop immediately so the model
            # cannot retry with a different approach in the same turn.
            history[:] = scratch_history
            if transcript_writer:
                for op, payload in pending_transcript_ops:
                    if op == "message":
                        transcript_writer.append_message(payload)  # type: ignore[arg-type]
                    else:
                        transcript_writer.append_compaction(payload)  # type: ignore[arg-type]
            await drain_loop_event_hooks()
            return history

        if any(block["name"] == "sessions_spawn" for block in tool_use_blocks):
            try:
                announced = await _wait_for_subagent_announcements(
                    registry,
                    cfg,
                    cancellation_token=cancellation_token,
                )
            except TurnCancelled:
                await drain_loop_event_hooks()
                raise
            if announced:
                scratch_history.extend(announced)
                pending_transcript_ops.extend(("message", msg) for msg in announced)
        # next iteration sends history (now including tool_results) back to the model

    scratch_history.append(
        Message("assistant", [{"type": "text", "text": "[max_iterations reached]"}])
    )
    pending_transcript_ops.append(("message", scratch_history[-1]))
    history[:] = scratch_history
    if transcript_writer:
        for op, payload in pending_transcript_ops:
            if op == "message":
                transcript_writer.append_message(payload)  # type: ignore[arg-type]
            else:
                transcript_writer.append_compaction(payload)  # type: ignore[arg-type]
    await drain_loop_event_hooks()
    return history


async def _wait_for_subagent_announcements(
    registry: ToolRegistry,
    cfg: LoopConfig,
    *,
    cancellation_token: "CancellationToken | None" = None,
) -> list[Message]:
    """Wait for spawned subagents from this session and return their messages."""
    spawn_context = getattr(registry, "_spawn_tool_context", None)
    requester_session_key = getattr(spawn_context, "requester_session_key", None) or cfg.session_key
    if not requester_session_key:
        return []

    from nano_openclaw.subagent.runner import get_runner

    runner = get_runner()
    try:
        await runner.wait_for_requester(requester_session_key, cancellation_token=cancellation_token)
    except asyncio.CancelledError as exc:
        raise TurnCancelled() from exc

    _check_cancelled(cancellation_token)
    return runner.drain_announcements(requester_session_key)


def _maybe_dump_payload(
    *,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int,
    thinking_budget_tokens: int | None,
) -> None:
    """Append one JSONL entry to nano-openclaw-debug.jsonl when NANO_DEBUG_PROMPT=1."""
    if os.getenv("NANO_DEBUG_PROMPT") != "1":
        return
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "max_tokens": max_tokens,
        "thinking_budget_tokens": thinking_budget_tokens,
        "system": system,
        "tools": tools,
        "messages": messages,
    }
    path = Path.cwd() / "nano-openclaw-debug.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def _consume_one_assistant_turn(
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
    cancellation_token: "CancellationToken | None" = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Stream one model response, accumulating mixed text + tool_use blocks."""
    blocks: list[dict[str, Any]] = []
    text_buf = ""
    tool_bufs: dict[str, dict[str, Any]] = {}
    tool_order: list[str] = []
    stop_reason: str | None = None

    def _flush_text():
        nonlocal text_buf
        if text_buf:
            blocks.append({"type": "text", "text": text_buf})
            text_buf = ""

    _maybe_dump_payload(
        model=model,
        system=system,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        thinking_budget_tokens=thinking_budget_tokens,
    )
    async for ev in stream_response(
        api=api,
        client=client,
        model=model,
        system=system,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        thinking_budget_tokens=thinking_budget_tokens,
    ):
        _check_cancelled(cancellation_token)
        on_event(ev)
        _check_cancelled(cancellation_token)

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
            tool_id = ev.id or f"tool-call-{len(tool_order)}"
            if tool_id not in tool_bufs:
                tool_bufs[tool_id] = {"id": tool_id, "name": ev.name, "buf": ""}
                tool_order.append(tool_id)

        elif isinstance(ev, ToolUseDelta):
            tool_id = ev.id or (tool_order[-1] if tool_order else "")
            if tool_id in tool_bufs:
                tool_bufs[tool_id]["buf"] += ev.partial_json

        elif isinstance(ev, ToolUseEnd):
            tool_id = ev.id or (tool_order[-1] if tool_order else "")
            cur_tool = tool_bufs.pop(tool_id, None)
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

        elif isinstance(ev, MessageEnd):
            _flush_text()
            stop_reason = ev.stop_reason

    return blocks, stop_reason
