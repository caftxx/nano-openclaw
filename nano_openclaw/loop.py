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

from nano_openclaw.compact import compact_if_needed, estimate_tokens, should_run_memory_flush
from nano_openclaw.config.types import MemoryFlushConfig
from nano_openclaw.attachments import (
    AttachmentAttached,
    AttachmentError,
    PromptAttachment,
    attachment_prompt_block,
    is_image_mime,
    save_non_image_attachment,
)
from nano_openclaw.images import (
    describe_image,
    load_image,
    load_image_bytes,
    parse_image_refs,
    to_anthropic_image_block,
)
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
MEMORY_FLUSH_ALLOWED_TOOLS = frozenset({"read_file", "write_file"})


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
    context_window: int = 0  # Model physical context window (0 = unknown)
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
    # Pre-compaction memory flush configuration
    memory_flush_config: MemoryFlushConfig = field(default_factory=MemoryFlushConfig)
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


@dataclass
class AgentSession:
    """Runtime state for one agent session.

    Keep one ``AgentSession`` per conversation and call ``run_turn`` for each
    user turn.
    """

    history: list[Message]
    registry: ToolRegistry
    on_event: EventCallback
    client: Any
    cfg: LoopConfig
    transcript_writer: "TranscriptWriter | None" = None
    cancellation_token: "CancellationToken | None" = None

    @property
    def session_id(self) -> str:
        if self.transcript_writer:
            return self.transcript_writer.session_id
        return self.cfg.session_key

    async def run_turn(
        self,
        user_input: str,
        *,
        on_event: EventCallback | None = None,
        cancellation_token: "CancellationToken | None" = None,
        attachments: list[PromptAttachment] | None = None,
        attachment_turn_id: str | None = None,
    ) -> list[Message]:
        """Drive one user turn to completion and mutate ``history`` in place."""
        return await _run_agent_session_turn(
            self,
            user_input,
            on_event=on_event,
            cancellation_token=cancellation_token,
            attachments=attachments,
            attachment_turn_id=attachment_turn_id,
        )

    def _commit_turn(
        self,
        scratch_history: list[Message],
        pending_ops: list[tuple[str, Message | str]],
    ) -> None:
        self.history[:] = scratch_history
        if not self.transcript_writer:
            return
        for op, payload in pending_ops:
            if op == "message":
                self.transcript_writer.append_message(payload)  # type: ignore[arg-type]
            else:
                self.transcript_writer.append_compaction(payload)  # type: ignore[arg-type]

    def _load_turn_skills(self) -> tuple[list[SkillEntry], list[Skill] | None]:
        eligible_entries: list[SkillEntry] = []
        visible_skills: list[Skill] | None = None
        if not self.cfg.workspace_dir:
            return eligible_entries, visible_skills

        skill_entries = get_or_load_skills(
            self.cfg.workspace_dir,
            self.cfg.session_key,
            extra_dirs=self.cfg.extra_skill_dirs,
            max_bytes=self.cfg.max_skill_file_bytes,
        )
        if skill_entries:
            eligible_entries = filter_eligible_skills(
                skill_entries,
                skill_filter=self.cfg.skill_filter,
            )
            visible_skills = filter_visible_skills(eligible_entries)
        return eligible_entries, visible_skills

    def _prepare_skill_command(
        self,
        user_input: str,
        eligible_entries: list[SkillEntry],
    ) -> tuple[SlashCommand | None, str, dict[str, Skill]]:
        command: SlashCommand | None = None
        remaining_text = user_input
        skill_registry: dict[str, Skill] = {}
        if not eligible_entries:
            return command, remaining_text, skill_registry

        # Slash commands: user-invocable skills only.
        runtime_registry = build_skill_registry_from_entries(eligible_entries)
        if runtime_registry:
            skill_registry = runtime_registry
        command, remaining_text = parse_slash_command(user_input, skill_registry)

        # Model Skill tool: all eligible skills, not just user-invocable ones.
        model_registry = build_skill_registry_from_entries(
            eligible_entries,
            user_invocable_only=False,
        )
        self.registry.set_eligible_skills(model_registry)
        return command, remaining_text, skill_registry

    async def _build_user_content(
        self,
        user_input: str,
        remaining_text: str,
        command: SlashCommand | None,
        on_event: EventCallback,
        *,
        attachments: list[PromptAttachment] | None = None,
        attachment_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []

        if command:
            skill_context = build_slash_command_context(command)
            content.append({"type": "text", "text": skill_context})
            on_event(SkillInvoked(skill_name=command.name, skill_path=command.skill.filePath))

        cleaned_text, image_refs = parse_image_refs(remaining_text)
        loaded_refs: list[str] = []
        for ref in image_refs:
            try:
                b64, mime = load_image(ref)
                if self.cfg.image_model:
                    on_event(ImageDescribe(ref=ref))
                    desc = await describe_image(
                        b64,
                        mime,
                        client=self.client,
                        model=self.cfg.image_model,
                        api=self.cfg.api,
                    )
                    content.append({"type": "text", "text": f"[Image: {desc}]"})
                elif self.cfg.model_has_vision:
                    content.append(to_anthropic_image_block(b64, mime))
                else:
                    on_event(ImageSkip(
                        ref=ref,
                        reason="model has no vision capability and no image_model configured",
                    ))
                loaded_refs.append(ref)
            except Exception as exc:
                on_event(ImageError(ref=ref, error=str(exc)))

        if loaded_refs:
            on_event(ImageAttached(refs=loaded_refs, via_model=bool(self.cfg.image_model)))

        attachment_refs: list[str] = []
        uploaded_image_refs: list[str] = []
        attachment_root = self.cfg.workspace_dir or Path(os.getcwd())
        turn_attachment_id = attachment_turn_id
        if attachments and not turn_attachment_id:
            turn_attachment_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
        for attachment in attachments or []:
            if is_image_mime(attachment.mime):
                try:
                    b64, mime = load_image_bytes(attachment.data, attachment.mime)
                    if self.cfg.image_model:
                        on_event(ImageDescribe(ref=attachment.name))
                        desc = await describe_image(
                            b64,
                            mime,
                            client=self.client,
                            model=self.cfg.image_model,
                            api=self.cfg.api,
                        )
                        content.append({
                            "type": "text",
                            "text": f"[Image: {attachment.name} - {desc}]",
                        })
                    elif self.cfg.model_has_vision:
                        content.append(to_anthropic_image_block(b64, mime))
                    else:
                        on_event(ImageSkip(
                            ref=attachment.name,
                            reason="model has no vision capability and no image_model configured",
                        ))
                    uploaded_image_refs.append(attachment.name)
                except Exception as exc:
                    on_event(ImageError(ref=attachment.name, error=str(exc)))
                continue

            try:
                saved = save_non_image_attachment(
                    attachment,
                    root=attachment_root,
                    session_id=self.cfg.session_key,
                    turn_id=turn_attachment_id or "turn",
                )
                content.append({"type": "text", "text": attachment_prompt_block(saved)})
                attachment_refs.append(saved.display_path)
            except Exception as exc:
                on_event(AttachmentError(ref=attachment.name, error=str(exc)))

        if uploaded_image_refs:
            on_event(ImageAttached(refs=uploaded_image_refs, via_model=bool(self.cfg.image_model)))
        if attachment_refs:
            on_event(AttachmentAttached(refs=attachment_refs))

        # Mirror openclaw convertContentBlocks: guarantee at least one text block.
        if cleaned_text:
            content.append({"type": "text", "text": cleaned_text})
        if not content:
            content.append({"type": "text", "text": user_input})
        elif not any(b.get("type") == "text" for b in content):
            content.append({"type": "text", "text": "(see attached image)"})
        return content

    async def _build_system_for_turn(
        self,
        user_input: str,
        scratch_history: list[Message],
        visible_skills: list[Skill] | None,
        on_event: EventCallback,
    ) -> str:
        bootstrap_files: list[WorkspaceBootstrapFile] | None = None
        if self.cfg.workspace_dir:
            bootstrap_files = get_or_load_bootstrap_files(
                self.cfg.workspace_dir,
                self.cfg.session_key,
                self.cfg.bootstrap_max_chars,
                self.cfg.bootstrap_total_max_chars,
            )

        active_memory_context: str | None = None
        if (
            self.cfg.workspace_dir
            and self.cfg.active_memory_config
            and self.cfg.active_memory_config.enabled
        ):
            manager = ActiveMemoryManager(
                client=self.client,
                model=self.cfg.model,
                workspace_dir=str(self.cfg.workspace_dir),
                config=self.cfg.active_memory_config,
            )
            wire_messages = [{"role": m.role, "content": m.content} for m in scratch_history]
            recall_result = await manager.run(wire_messages)
            if recall_result:
                on_event(ActiveMemoryRecall(result=recall_result))
                if recall_result.context:
                    active_memory_context = recall_result.context

        if self.cfg.system_prompt_override is not None:
            system = self.cfg.system_prompt_override
        else:
            system = build_system_prompt(
                self.registry,
                self.cfg.workspace_dir,
                bootstrap_files,
                visible_skills,
                max_skills_in_prompt=self.cfg.max_skills_in_prompt,
                max_skills_prompt_chars=self.cfg.max_skills_prompt_chars,
            )

        if self.cfg.hook_registry:
            hook_result = await self.cfg.hook_registry.run("before_prompt_build", {
                "system": system,
                "user_input": user_input,
                "workspace_dir": str(self.cfg.workspace_dir) if self.cfg.workspace_dir else "",
            })
            system = hook_result.get("system", system)
            if prepend := hook_result.get("prepend"):
                system = f"{prepend}\n\n{system}"
            if append := hook_result.get("append"):
                system = f"{system}\n\n{append}"

        if active_memory_context:
            system = f"{active_memory_context}\n\n{system}"
        return system

    async def _dispatch_tool_batch(
        self,
        scratch_history: list[Message],
        tool_use_blocks: list[dict[str, Any]],
        skill_registry: dict[str, Skill],
        on_event: EventCallback,
        cancellation_token: "CancellationToken | None",
    ) -> tuple[list[dict[str, Any]], bool]:
        self.registry.set_session_status_context(
            model=self.cfg.model,
            session_id=self.transcript_writer.session_id if self.transcript_writer else "",
            context_budget=self.cfg.context_budget,
            context_window=self.cfg.context_window,
            current_tokens=estimate_tokens(scratch_history),
            compaction_count=self.transcript_writer.compaction_count if self.transcript_writer else 0,
            message_count=len(scratch_history),
        )

        # Emit SkillInvoked events before dispatch; order matters for UX.
        for block in tool_use_blocks:
            tool_name = block["name"]
            tool_args = block.get("input") or {}
            if tool_name == "skill" and "skill" in tool_args:
                skill_name = tool_args["skill"]
                if skill_registry and skill_name in skill_registry:
                    skill = skill_registry[skill_name]
                    on_event(SkillInvoked(skill_name=skill_name, skill_path=skill.filePath))

        batch_requires_approval = False
        if self.registry.approval_manager:
            batch_requires_approval = any(
                self.registry.approval_manager.check_request(
                    b["name"],
                    b.get("input") or {},
                ).requires_approval
                for b in tool_use_blocks
            )

        if batch_requires_approval:
            tool_results = []
            denied_tool_name: str | None = None
            for block in tool_use_blocks:
                if denied_tool_name is not None:
                    tool_results.append(_skipped_after_denial_result(
                        block["id"],
                        denied_tool_name,
                    ))
                    continue

                result = await self.registry.dispatch(
                    block["id"],
                    block["name"],
                    block.get("input") or {},
                    cancellation_token=cancellation_token,
                )
                if result.get("_denied"):
                    denied_tool_name = block["name"]
                tool_results.append(result)
        else:
            tool_results = list(
                await asyncio.gather(*[
                    self.registry.dispatch(
                        b["id"],
                        b["name"],
                        b.get("input") or {},
                        cancellation_token=cancellation_token,
                    )
                    for b in tool_use_blocks
                ])
            )

        has_denial = False
        for result in tool_results:
            has_denial = bool(result.pop("_denied", False)) or has_denial
        for block, result in zip(tool_use_blocks, tool_results):
            on_event(ToolResult(
                tool_use_id=block["id"],
                name=block["name"],
                args=block.get("input") or {},
                result=result,
            ))
        return tool_results, has_denial

async def _run_agent_session_turn(
    session: AgentSession,
    user_input: str,
    *,
    on_event: EventCallback | None = None,
    cancellation_token: "CancellationToken | None" = None,
    attachments: list[PromptAttachment] | None = None,
    attachment_turn_id: str | None = None,
) -> list[Message]:
    """Drive one user turn to completion (possibly through many tool rounds).

    ``history`` is mutated in place AND returned for convenience. The caller
    keeps the same list across turns to maintain conversation state.
    ``on_event`` receives every streaming event and every loop event (``ToolResult``,
    ``Compaction``, ``ImageAttached``, etc.); the CLI uses it for live rendering.
    Skills are loaded from cfg each turn (cached per session) and used for
    both slash command dispatch and system prompt injection.
    """
    history = session.history
    registry = session.registry
    on_event = on_event or session.on_event
    client = session.client
    cfg = session.cfg
    cancellation_token = (
        cancellation_token if cancellation_token is not None else session.cancellation_token
    )

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
    eligible_entries, visible_skills = session._load_turn_skills()

    # 2. Parse slash command (mirrors openclaw slash-commands.md)
    command, remaining_text, skill_registry = session._prepare_skill_command(
        user_input,
        eligible_entries,
    )

    # 3. Build message content
    content = await session._build_user_content(
        user_input,
        remaining_text,
        command,
        on_event,
        attachments=attachments,
        attachment_turn_id=attachment_turn_id,
    )

    scratch_history.append(Message("user", content))
    pending_transcript_ops.append(("message", scratch_history[-1]))

    system = await session._build_system_for_turn(
        user_input,
        scratch_history,
        visible_skills,
        on_event,
    )

    tools_schema = registry.schemas()
    already_flushed_for_compaction = False

    for _ in range(cfg.max_iterations):
        await check_cancelled()
        current_tokens = estimate_tokens(scratch_history)
        if should_run_memory_flush(
            current_tokens,
            cfg.context_window or cfg.context_budget,
            cfg.memory_flush_config,
            already_flushed_for_compaction,
        ):
            await run_pre_compaction_memory_flush(
                client=client,
                cfg=cfg,
                history=scratch_history,
                registry=registry,
                system=system,
                tools_schema=tools_schema,
                force=True,
                already_flushed=already_flushed_for_compaction,
                cancellation_token=cancellation_token,
            )
            already_flushed_for_compaction = True

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
            session._commit_turn(scratch_history, pending_transcript_ops)
            await drain_loop_event_hooks()
            return history  # end_turn / max_tokens / stop_sequence — terminal

        tool_use_blocks = [b for b in assistant_blocks if b.get("type") == "tool_use"]

        await check_cancelled()
        tool_results, has_denial = await session._dispatch_tool_batch(
            scratch_history,
            tool_use_blocks,
            skill_registry,
            on_event,
            cancellation_token,
        )
        await check_cancelled()

        scratch_history.append(Message("user", tool_results))
        pending_transcript_ops.append(("message", scratch_history[-1]))

        if has_denial:
            # At least one tool was denied — make one final model call so the
            # model can draw a conclusion from the context collected so far,
            # then end the turn. Pass no tools to force a text-only response.
            await check_cancelled()
            wire_messages = [{"role": m.role, "content": m.content} for m in scratch_history]
            try:
                final_blocks, _ = await _consume_one_assistant_turn(
                    client=client,
                    api=cfg.api,
                    model=cfg.model,
                    system=system,
                    messages=wire_messages,
                    tools=[],
                    max_tokens=cfg.max_tokens,
                    thinking_budget_tokens=cfg.thinking_budget_tokens,
                    on_event=on_event,
                    cancellation_token=cancellation_token,
                )
            except TurnCancelled:
                await drain_loop_event_hooks()
                raise
            scratch_history.append(Message("assistant", final_blocks))
            pending_transcript_ops.append(("message", scratch_history[-1]))
            session._commit_turn(scratch_history, pending_transcript_ops)
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
    session._commit_turn(scratch_history, pending_transcript_ops)
    await drain_loop_event_hooks()
    return history


async def _run_memory_flush_turn(
    *,
    client: Any,
    cfg: LoopConfig,
    history: list[Message],
    registry: ToolRegistry,
    system: str,
    tools_schema: list[dict[str, Any]],
    cancellation_token: "CancellationToken | None" = None,
) -> None:
    """Run a silent model turn that can save context before compaction.

    The temporary conversation is intentionally not committed to user-visible
    history or transcript. Tool calls still execute so memory/file writes can
    persist the important state before the following compaction summarizes it.
    """
    temp_history = list(history)
    target_path = _memory_flush_target_path()
    temp_history.append(Message(
        "user",
        [{"type": "text", "text": _build_memory_flush_prompt(cfg.memory_flush_config.prompt)}],
    ))
    memory_flush_tools = [
        schema for schema in tools_schema if schema.get("name") in MEMORY_FLUSH_ALLOWED_TOOLS
    ]

    def silent_on_event(_event: Any) -> None:
        return None

    for _ in range(cfg.max_iterations):
        _check_cancelled(cancellation_token)
        wire_messages = [{"role": m.role, "content": m.content} for m in temp_history]
        assistant_blocks, stop_reason = await _consume_one_assistant_turn(
            client=client,
            api=cfg.api,
            model=cfg.model,
            system=system,
            messages=wire_messages,
            tools=memory_flush_tools,
            max_tokens=cfg.max_tokens,
            thinking_budget_tokens=cfg.thinking_budget_tokens,
            on_event=silent_on_event,
            cancellation_token=cancellation_token,
        )
        _check_cancelled(cancellation_token)
        temp_history.append(Message("assistant", assistant_blocks))
        if stop_reason != "tool_use":
            return

        tool_use_blocks = [b for b in assistant_blocks if b.get("type") == "tool_use"]
        if not tool_use_blocks:
            return

        tool_results = list(
            await asyncio.gather(*[
                _dispatch_memory_flush_tool(
                    registry,
                    cfg,
                    tool_use_id=b["id"],
                    name=b["name"],
                    args=b.get("input") or {},
                    target_path=target_path,
                    cancellation_token=cancellation_token,
                )
                for b in tool_use_blocks
            ])
        )
        _check_cancelled(cancellation_token)
        for result in tool_results:
            result.pop("_denied", None)
        temp_history.append(Message("user", tool_results))


async def run_pre_compaction_memory_flush(
    *,
    client: Any,
    cfg: LoopConfig,
    history: list[Message],
    registry: ToolRegistry,
    system: str | None = None,
    tools_schema: list[dict[str, Any]] | None = None,
    force: bool = False,
    already_flushed: bool = False,
    cancellation_token: "CancellationToken | None" = None,
) -> bool:
    """Run the silent memory flush used immediately before context compaction.

    ``force=True`` is for explicit manual compaction: it bypasses the token
    threshold check while still respecting config and tool/workspace gates.
    """
    current_tokens = estimate_tokens(history)
    context_window = cfg.context_window or cfg.context_budget
    if not force and not should_run_memory_flush(
        current_tokens,
        context_window,
        cfg.memory_flush_config,
        already_flushed,
    ):
        return False
    if already_flushed or not cfg.memory_flush_config.enabled:
        return False

    if not cfg.workspace_dir:
        return False

    if tools_schema is None:
        tools_schema = registry.schemas()
    if not _has_memory_flush_write_tool(tools_schema):
        return False

    if system is None:
        system = _build_memory_flush_system(registry, cfg)

    await _run_memory_flush_turn(
        client=client,
        cfg=cfg,
        history=history,
        registry=registry,
        system=system,
        tools_schema=tools_schema,
        cancellation_token=cancellation_token,
    )
    return True


def _build_memory_flush_system(registry: ToolRegistry, cfg: LoopConfig) -> str:
    bootstrap_files: list[WorkspaceBootstrapFile] | None = None
    visible_skills: list[Skill] | None = None
    if cfg.workspace_dir:
        bootstrap_files = get_or_load_bootstrap_files(
            cfg.workspace_dir,
            cfg.session_key,
            cfg.bootstrap_max_chars,
            cfg.bootstrap_total_max_chars,
        )
        skill_entries = get_or_load_skills(
            cfg.workspace_dir,
            cfg.session_key,
            extra_dirs=cfg.extra_skill_dirs,
            max_bytes=cfg.max_skill_file_bytes,
        )
        if skill_entries:
            eligible_entries = filter_eligible_skills(skill_entries, skill_filter=cfg.skill_filter)
            visible_skills = filter_visible_skills(eligible_entries)

    if cfg.system_prompt_override is not None:
        return cfg.system_prompt_override
    return build_system_prompt(
        registry,
        cfg.workspace_dir,
        bootstrap_files,
        visible_skills,
        max_skills_in_prompt=cfg.max_skills_in_prompt,
        max_skills_prompt_chars=cfg.max_skills_prompt_chars,
    )


async def _dispatch_memory_flush_tool(
    registry: ToolRegistry,
    cfg: LoopConfig,
    *,
    tool_use_id: str,
    name: str,
    args: dict[str, Any],
    target_path: str,
    cancellation_token: "CancellationToken | None" = None,
) -> dict[str, Any]:
    """Dispatch only the narrow, append-only tool surface used by memory flush."""
    if name not in MEMORY_FLUSH_ALLOWED_TOOLS:
        return _tool_error_result(tool_use_id, f"memory flush cannot use tool: {name!r}")

    if name != "write_file":
        return await registry.dispatch(
            tool_use_id,
            name,
            args,
            cancellation_token=cancellation_token,
        )

    content = args.get("content")
    if not isinstance(content, str):
        return _tool_error_result(tool_use_id, "write_file requires string content")
    if cfg.workspace_dir is None:
        return _tool_error_result(tool_use_id, "memory flush requires a workspace directory")

    rel_path = args.get("path")
    if not isinstance(rel_path, str) or not rel_path.strip():
        return _tool_error_result(tool_use_id, "write_file requires path")

    workspace = cfg.workspace_dir.resolve()
    requested = (workspace / rel_path).resolve()
    target = (workspace / target_path).resolve()
    try:
        requested.relative_to(workspace)
    except ValueError:
        return _tool_error_result(tool_use_id, f"path escapes workspace: {rel_path}")
    if requested != target:
        return _tool_error_result(
            tool_use_id,
            f"memory flush writes are restricted to {target_path}; use that path only",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    separator = "" if not existing or existing.endswith("\n") or not content else "\n"
    target.write_text(f"{existing}{separator}{content}", encoding="utf-8")
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": [{"type": "text", "text": f"Appended content to {target_path}."}],
    }


def _tool_error_result(tool_use_id: str, message: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": True,
        "content": [{"type": "text", "text": message}],
    }


def _memory_flush_target_path() -> str:
    return f"memory/{datetime.now().strftime('%Y-%m-%d')}.md"


def _has_memory_flush_write_tool(tools_schema: list[dict[str, Any]]) -> bool:
    return any(schema.get("name") == "write_file" for schema in tools_schema)


def _build_memory_flush_prompt(prompt: str) -> str:
    """Resolve date placeholders in the memory flush prompt."""
    date_stamp = datetime.now().strftime("%Y-%m-%d")
    resolved = prompt.replace("YYYY-MM-DD", date_stamp)
    if "Current time:" in resolved:
        return resolved
    current_time = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return f"{resolved}\nCurrent time: {current_time}"


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


def _skipped_after_denial_result(tool_use_id: str, denied_tool_name: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "is_error": True,
        "content": [{
            "type": "text",
            "text": f"skipped because approval was denied for {denied_tool_name}",
        }],
    }


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
