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

from nano_openclaw.core.compact import (
    CompactionState,
    compact_if_needed,
    estimate_tokens,
    should_run_memory_flush,
)
from nano_openclaw.config.types import ExtractMemoriesConfig, MemoryFlushConfig
from nano_openclaw.logger import get_logger
from nano_openclaw.core.attachments import (
    AttachmentAttached,
    AttachmentError,
    PromptAttachment,
    attachment_prompt_block,
    is_image_mime,
    save_non_image_attachment,
)
from nano_openclaw.core.images import (
    describe_image,
    load_image,
    load_image_bytes,
    parse_image_refs,
    to_anthropic_image_block,
)
from nano_openclaw.memory.active import ActiveMemoryConfig, ActiveMemoryManager, ActiveMemoryResult
from nano_openclaw.memory.dreaming import DreamingConfig
from nano_openclaw.core.prompt import VOICE_STYLE_PROMPT, build_system_prompt
from nano_openclaw.core.provider import (
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
from nano_openclaw.todo import TodoStore
from nano_openclaw.core.tools import ToolRegistry
from nano_openclaw.workspace import (
    WorkspaceBootstrapFile,
    get_or_load_bootstrap_files,
    load_workspace_memory_index,
)

if TYPE_CHECKING:
    from nano_openclaw.plugins.registry import HookRegistry
    from nano_openclaw.session import TranscriptWriter

EventCallback = Callable[[Any], None]
MEMORY_FLUSH_ALLOWED_TOOLS = frozenset({"read_file", "write_file"})

logger = get_logger(__name__)


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


def append_active_todo_reminder(
    history: list[Message],
    todo_store: TodoStore | None,
) -> Message | None:
    """Append the compact-time active todo reminder, if there is one."""
    if todo_store is None:
        return None
    snapshot = todo_store.format_for_injection()
    if not snapshot:
        return None
    reminder = Message(
        role="user",
        content=[{"type": "text", "text": snapshot}],
    )
    history.append(reminder)
    return reminder


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
class MaxIterationsReached:
    """Event emitted when the loop exhausts max_iterations and forces a final conclusion."""
    max_iterations: int


@dataclass
class StopReasonWarning:
    """Event emitted when the model stops for an unusual reason (max_tokens, stop_sequence)."""
    stop_reason: str
    iteration: int


@dataclass
class RetryAttempt:
    """Event emitted before each retry of a failed API call."""
    attempt: int       # 1-indexed
    max_attempts: int
    error: str


@dataclass
class SubagentProgress:
    """Event emitted during subagent execution with live progress."""
    run_id: str
    label: str
    tool_uses: int
    input_tokens: int
    output_tokens: int
    current_activity: str


@dataclass
class SubagentEvent:
    """Event emitted from inside a subagent, tagged for parent activity views."""
    run_id: str
    label: str
    task: str
    event: Any


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
    # When True, after a turn that triggered compaction the transcript file is
    # rewritten to header + compaction marker + kept messages, so a daemon
    # restart loads the post-compaction history instead of replaying the full
    # pre-compaction transcript and re-paying the compaction cost.
    truncate_after_compaction: bool = True
    # Prompt caching TTL (Stage 4). When set to "5m" or "1h" and api=="anthropic",
    # the provider transport applies ``cache_control`` markers on the system
    # prompt + last 3 messages. None disables caching entirely.
    cache_ttl: str | None = None
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
    state_dir: Path | None = None  # State directory for telemetry/checkpoints
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
    # Stop-hook memory extractor configuration (mirrors claude-code
    # extractMemories.ts). None = disabled. Hook reads this on after_turn.
    extract_memories_config: "ExtractMemoriesConfig | None" = None
    # Turn provenance — used by the stop-hook extractor's triggerSources
    # filter and any plugin that needs to know who initiated the turn.
    # Enum: "tui" / "webui" / "wechat" / "cron" / "channel_auto".
    turn_source: str = "tui"
    # Per-turn response style hint, orthogonal to turn_source. "voice" appends
    # a spoken-conversation directive (concise, no markdown/emoji) for the web
    # voice hands-free mode. Empty = normal. Does not affect memory/triggerSources.
    response_style: str = ""
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
class SessionUsageStats:
    """Per-conversation token / cache counters surfaced by ``/usage``.

    Lives on the long-lived holder (``AgentBackendSession`` for the gateway,
    a CLI outer-scope variable for the standalone CLI) and is shared by
    reference into the per-turn ``AgentSession``. Every ``MessageEnd``
    in the loop calls :meth:`update_from_usage` so counters survive across
    turns.

    Naming note: ``*_prompt_tokens`` is the **total prompt size** the model
    saw — i.e. ``input_tokens + cache_read_input_tokens +
    cache_creation_input_tokens`` from Anthropic's ``usage`` object. This
    is intentionally NOT the same as Anthropic's ``input_tokens`` field
    (which counts only the billable, non-cached portion). We track the
    total because:

      * ``compact_if_needed`` measures against ``context_window`` — the
        model's hard limit applies to the total prompt, not the billable
        slice. Using billable would never trigger compaction on cached
        sessions even as the actual context grows.
      * ``/usage``'s "% of budget" should reflect what the model actually
        sees, not the cost line item.

    For cost / billable analysis, derive billable = prompt_total -
    cache_read - cache_creation from the cache fields.
    """

    last_prompt_tokens: int = 0   # input + cache_read + cache_creation, last turn
    last_output_tokens: int = 0
    last_cache_read_tokens: int = 0
    last_cache_creation_tokens: int = 0
    total_prompt_tokens: int = 0  # cumulative input + cache_read + cache_creation
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    compactions_fired: int = 0
    turns_recorded: int = 0

    def update_from_usage(self, usage: dict[str, Any]) -> None:
        """Fold a ``MessageEnd.usage`` dict into the running counters.

        Empty dict is a no-op (some OpenAI-compatible providers don't
        surface usage on every chunk). Cache-related fields default to 0
        when not present (e.g. when prompt caching is disabled or for
        OpenAI which doesn't carry the field at all).

        ``last_prompt_tokens`` / ``total_prompt_tokens`` are the SUM of
        ``input + cache_read + cache_creation`` — the real prompt size
        the model saw. See the class docstring for why we track total
        rather than just billable input.
        """
        if not usage:
            return
        in_tok = usage.get("input_tokens", 0) or 0
        out_tok = usage.get("output_tokens", 0) or 0
        cr_tok = usage.get("cache_read_input_tokens", 0) or 0
        cc_tok = usage.get("cache_creation_input_tokens", 0) or 0
        prompt_total = in_tok + cr_tok + cc_tok
        self.last_prompt_tokens = prompt_total or self.last_prompt_tokens
        self.last_output_tokens = out_tok or self.last_output_tokens
        self.last_cache_read_tokens = cr_tok
        self.last_cache_creation_tokens = cc_tok
        self.total_prompt_tokens += prompt_total
        self.total_output_tokens += out_tok
        self.total_cache_read_tokens += cr_tok
        self.total_cache_creation_tokens += cc_tok
        self.turns_recorded += 1

    def cache_hit_ratio(self) -> float | None:
        """Return cache hit ratio as a fraction in [0, 1], or None when
        there's no cached prompt traffic to score (denominator zero).

        Hit = cache_read; miss is approximated as cache_creation (the
        tokens that just became cacheable but weren't a hit yet).
        Note: providers that silently cache without reporting
        ``cache_creation_input_tokens`` (some Anthropic-compatible
        proxies / aggregators) will inflate this ratio because the first-
        turn miss is hidden from the denominator.
        """
        denom = self.total_cache_read_tokens + self.total_cache_creation_tokens
        if denom <= 0:
            return None
        return self.total_cache_read_tokens / denom


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
    # Per-conversation usage / cache counters. Long-lived holders
    # (e.g. AgentBackendSession) inject the same instance every turn so
    # ``/usage`` sees cumulative totals + the previous turn's
    # ``last_prompt_tokens`` as a real-token compaction trigger across
    # turns (Stage 2 fix).
    usage_stats: SessionUsageStats = field(default_factory=SessionUsageStats)
    # Per-session compaction tracking (previous_summary for iterative
    # updates + cooldown bookkeeping). Same long-lived-instance pattern as
    # usage_stats so iterative summary updates work across turns (Stage 3 fix).
    compaction_state: CompactionState = field(default_factory=CompactionState)
    # Per-session todo list. AgentBackendSession 注入 shared 实例做持久化；
    # 直接构造 AgentSession 的 caller（CLI 旧路径 / cron）给个 throwaway store
    # 也能让模型用，turn 结束就丢。
    todo_store: TodoStore = field(default_factory=TodoStore)

    @property
    def last_prompt_tokens(self) -> int:
        """Total prompt tokens last turn — input + cache_read + cache_creation.
        See ``SessionUsageStats`` for why we track total rather than billable."""
        return self.usage_stats.last_prompt_tokens

    @property
    def last_output_tokens(self) -> int:
        return self.usage_stats.last_output_tokens

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

        from nano_openclaw.session.transcript import is_synthetic_summary

        latest_summary: str | None = None
        for op, payload in pending_ops:
            if op == "compaction":
                latest_summary = payload  # type: ignore[assignment]

        should_rotate = (
            latest_summary is not None
            and self.cfg.truncate_after_compaction
        )

        if not should_rotate:
            for op, payload in pending_ops:
                if op == "message":
                    self.transcript_writer.append_message(payload)  # type: ignore[arg-type]
                else:
                    self.transcript_writer.append_compaction(payload)  # type: ignore[arg-type]
            return

        # Compaction happened this turn. Drop the synthetic summary that
        # compact_if_needed prepended to scratch_history — it's about to be
        # represented on disk by a TranscriptCompaction entry instead.
        kept = list(scratch_history)
        if kept and is_synthetic_summary(kept[0]):
            kept = kept[1:]
        self.transcript_writer.rotate(latest_summary or "", kept)

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
        try:
            from nano_openclaw.skills.usage import record_event
            for skill_name, skill in model_registry.items():
                record_event(
                    self.cfg.state_dir,
                    skill_name,
                    "load",
                    source=skill.source,
                    path=skill.filePath,
                )
        except Exception:
            pass
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
            try:
                from nano_openclaw.skills.usage import record_event
                record_event(
                    self.cfg.state_dir,
                    command.name,
                    "use",
                    source=command.skill.source,
                    path=command.skill.filePath,
                )
            except Exception:
                pass
            on_event(SkillInvoked(skill_name=command.name, skill_path=command.skill.filePath))

        cleaned_text, image_refs = parse_image_refs(remaining_text, workspace_dir=self.cfg.workspace_dir)
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
                logger.warning("loop.image.load.error", f"Failed to load image {ref}: {exc}")
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
                        content.append({
                            "type": "text",
                            "text": (
                                f"[Image: {attachment.name}]\n"
                                f"type: {attachment.mime}\n"
                                f"size: {attachment.size} bytes\n\n"
                                "The user sent an image but the current model cannot view it. "
                                "Let the user know you received it and ask them to describe it if needed."
                            ),
                        })
                    uploaded_image_refs.append(attachment.name)
                except Exception as exc:
                    logger.warning("loop.attachment.image.error", f"Failed to process image attachment {attachment.name}: {exc}")
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
                logger.warning("loop.attachment.save.error", f"Failed to save attachment {attachment.name}: {exc}")
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

    async def _recall_active_memory_context(
        self,
        scratch_history: list[Message],
        on_event: EventCallback,
    ) -> str | None:
        """Run Active Memory recall and return text to prepend to the user message.

        Mirrors openclaw's active-memory plugin (extensions/active-memory/index.ts)
        which returns `prependContext` — appended to the current user prompt, NOT
        the system prompt, so the cacheable system prefix stays stable.
        """
        if not (
            self.cfg.workspace_dir
            and self.cfg.active_memory_config
            and self.cfg.active_memory_config.enabled
        ):
            return None
        manager = ActiveMemoryManager(
            client=self.client,
            model=self.cfg.model,
            workspace_dir=str(self.cfg.workspace_dir),
            config=self.cfg.active_memory_config,
        )
        wire_messages = [{"role": m.role, "content": m.content} for m in scratch_history]
        recall_result = await manager.run(wire_messages)
        if not recall_result:
            return None
        on_event(ActiveMemoryRecall(result=recall_result))
        return recall_result.context or None

    async def _build_system_for_turn(
        self,
        user_input: str,
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

        if self.cfg.system_prompt_override is not None:
            system = self.cfg.system_prompt_override
        else:
            auto_memory_index: str | None = None
            if self.cfg.workspace_dir:
                auto_memory_index = load_workspace_memory_index(self.cfg.workspace_dir)
            system = build_system_prompt(
                self.registry,
                self.cfg.workspace_dir,
                bootstrap_files,
                visible_skills,
                max_skills_in_prompt=self.cfg.max_skills_in_prompt,
                max_skills_prompt_chars=self.cfg.max_skills_prompt_chars,
                auto_memory_index=auto_memory_index,
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

        # Voice hands-free mode: append the spoken-style directive last so it
        # carries the most recency. Stable across pure-voice turns, so the
        # prompt prefix still caches; only a voice<->text switch misses.
        if self.cfg.response_style == "voice":
            system = f"{system}\n\n{VOICE_STYLE_PROMPT}"

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
        tool_context = self.registry.execution_context(
            todo_store=self.todo_store,
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

        async def dispatch_with_context(block: dict[str, Any]) -> dict[str, Any]:
            try:
                return await self.registry.dispatch(
                    block["id"],
                    block["name"],
                    block.get("input") or {},
                    cancellation_token=cancellation_token,
                    context=tool_context,
                )
            except TypeError as exc:
                # Some tests monkey-patch ToolRegistry.dispatch with the legacy
                # signature. Keep that narrow compatibility while production
                # code receives the explicit execution context above.
                if "context" not in str(exc):
                    raise
                return await self.registry.dispatch(
                    block["id"],
                    block["name"],
                    block.get("input") or {},
                    cancellation_token=cancellation_token,
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

                result = await dispatch_with_context(block)
                if result.get("_denied"):
                    denied_tool_name = block["name"]
                tool_results.append(result)
        else:
            tool_results = list(
                await asyncio.gather(*[
                    dispatch_with_context(b)
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

    # User input is committed before the model call starts so a cancelled or
    # failed turn still leaves a resumable session anchored by the submitted
    # prompt. Assistant/tool messages are committed only after successful turns.
    scratch_history = list(history)
    pending_transcript_ops: list[tuple[str, Message | str]] = []
    loop_event_tasks: list[asyncio.Task] = []
    original_on_event = on_event

    def hooked_on_event(event: Any) -> None:
        event_type = type(event).__name__
        logger.debug("event.received", "", event_type=event_type)
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

    # 3b. Active Memory recall — prepend to user message (NOT system prompt) so
    #     the cacheable system prefix stays stable across turns. Mirrors openclaw's
    #     active-memory plugin which returns `prependContext` (per-turn user-msg
    #     prefix), not `prependSystemContext` (cacheable system prefix).
    active_memory_context = await session._recall_active_memory_context(
        scratch_history + [Message("user", content)],
        on_event,
    )
    if active_memory_context:
        content = [{"type": "text", "text": active_memory_context}, *content]

    user_message = Message("user", content)
    history.append(user_message)
    if session.transcript_writer:
        session.transcript_writer.append_message(user_message)
    scratch_history.append(user_message)

    system = await session._build_system_for_turn(
        user_input,
        visible_skills,
        on_event,
    )

    tools_schema = registry.schemas()
    already_flushed_for_compaction = False
    tools_used_set: set[str] = set()

    async def fire_after_turn_hook(stop_reason_label: str, iteration: int) -> None:
        """Fire `after_turn` hook for plugins (e.g. ReviewFork)."""
        if not cfg.hook_registry:
            return
        try:
            transcript_path = (
                str(session.transcript_writer.path)
                if session.transcript_writer
                else None
            )
            session_dir = (
                str(session.transcript_writer.path.parent)
                if session.transcript_writer
                else ""
            )
            agent_id = "default"
            try:
                from nano_openclaw.subagent.types import parse_session_key
                parsed = parse_session_key(cfg.session_key)
                agent_id = parsed.get("agentId", "default")
            except Exception:
                pass
            payload = {
                "session_id": session.session_id,
                "agent_id": agent_id,
                "session_key": cfg.session_key,
                "session_dir": session_dir,
                "transcript_path": transcript_path,
                "workspace_dir": str(cfg.workspace_dir) if cfg.workspace_dir else "",
                "stop_reason": stop_reason_label,
                "iteration_count": iteration,
                "turn_source": cfg.turn_source,
                "tools_used": sorted(tools_used_set),
                "messages_snapshot": [
                    {"role": m.role, "content": m.content} for m in scratch_history
                ],
                "user_input": user_input,
                "client": client,
                "loop_config": cfg,
                "tool_registry": registry,
                # Extractor / other after_turn hooks can push status events
                # back into the same per-turn event stream the UI is reading.
                # ``original_on_event`` is the unhooked callback captured
                # before ``hooked_on_event`` wrapped it — we deliberately
                # bypass the loop_event hook fanout because hooks that drive
                # this callback are themselves running as part of after_turn.
                "on_event": original_on_event,
            }
            await cfg.hook_registry.run("after_turn", payload)
        except Exception as exc:
            logger.warning("loop.after_turn.hook_error", f"after_turn hook failed: {exc}")

    for i in range(cfg.max_iterations):
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

        # Check context budget and compact if needed (mirrors OpenClaw's compaction).
        # Pass last_prompt_tokens (= input + cache_read + cache_creation) so the
        # trigger watches what the model actually saw last turn — not just the
        # billable input (which under-counts for cached sessions).
        _, summary = await compact_if_needed(
            scratch_history,
            budget=cfg.context_budget,
            client=client,
            model=cfg.model,
            api=cfg.api,
            threshold_ratio=cfg.context_threshold,
            recent_turns=cfg.context_recent_turns,
            last_prompt_tokens=session.last_prompt_tokens,
            state=session.compaction_state,
        )
        if summary:
            logger.info("loop.compaction", f"Context compacted: {summary[:100]}...")
            on_event(Compaction(summary=summary))
            pending_transcript_ops.append(("compaction", summary))
            session.usage_stats.compactions_fired += 1

            reminder = append_active_todo_reminder(scratch_history, session.todo_store)
            if reminder is not None:
                pending_transcript_ops.append(("message", reminder))
        wire_messages = [{"role": m.role, "content": m.content} for m in scratch_history]

        try:
            assistant_blocks, stop_reason, usage = await _retry_assistant_turn(
                client=client,
                api=cfg.api,
                model=cfg.model,
                system=system,
                messages=wire_messages,
                tools=tools_schema,
                max_tokens=cfg.max_tokens,
                thinking_budget_tokens=cfg.thinking_budget_tokens,
                cache_ttl=cfg.cache_ttl,
                on_event=on_event,
                cancellation_token=cancellation_token,
            )
        except TurnCancelled:
            await drain_loop_event_hooks()
            raise

        # Update per-conversation usage counters from real provider usage
        # (Stage 2.1, refined for cross-turn persistence). Empty dict is a
        # no-op so providers that don't surface usage don't zero us out.
        session.usage_stats.update_from_usage(usage)

        await check_cancelled()

        # max_tokens means the model's output was truncated. Force compact and retry once
        # so the model can generate a complete response with a smaller context window.
        if stop_reason == "max_tokens":
            on_event(StopReasonWarning(stop_reason="max_tokens", iteration=i + 1))
            _, summary = await compact_if_needed(
                scratch_history,
                budget=1,
                client=client,
                model=cfg.model,
                api=cfg.api,
                threshold_ratio=1.0,
                recent_turns=cfg.context_recent_turns,
                last_prompt_tokens=session.last_prompt_tokens,
                state=session.compaction_state,
            )
            if summary:
                logger.info("loop.compaction.max_tokens", f"Context compacted due to max_tokens: {summary[:100]}...")
                on_event(Compaction(summary=summary))
                pending_transcript_ops.append(("compaction", summary))
                session.usage_stats.compactions_fired += 1

                reminder = append_active_todo_reminder(scratch_history, session.todo_store)
                if reminder is not None:
                    pending_transcript_ops.append(("message", reminder))
            wire_messages = [{"role": m.role, "content": m.content} for m in scratch_history]
            try:
                assistant_blocks, stop_reason, usage = await _retry_assistant_turn(
                    client=client,
                    api=cfg.api,
                    model=cfg.model,
                    system=system,
                    messages=wire_messages,
                    tools=tools_schema,
                    max_tokens=cfg.max_tokens,
                    thinking_budget_tokens=cfg.thinking_budget_tokens,
                    cache_ttl=cfg.cache_ttl,
                    on_event=on_event,
                    cancellation_token=cancellation_token,
                )
            except TurnCancelled:
                await drain_loop_event_hooks()
                raise
            session.usage_stats.update_from_usage(usage)
            await check_cancelled()

        scratch_history.append(Message("assistant", assistant_blocks))
        pending_transcript_ops.append(("message", scratch_history[-1]))

        if stop_reason != "tool_use":
            session._commit_turn(scratch_history, pending_transcript_ops)
            await fire_after_turn_hook(stop_reason or "end_turn", i + 1)
            await drain_loop_event_hooks()
            return history  # end_turn / stop_sequence — terminal

        tool_use_blocks = [b for b in assistant_blocks if b.get("type") == "tool_use"]
        for _b in tool_use_blocks:
            _name = _b.get("name")
            if isinstance(_name, str):
                tools_used_set.add(_name)

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

        # Force a final text-only conclusion when the tool budget is exhausted or a tool
        # was denied. Pass tools=[] so the model cannot make more tool calls.
        is_last_iteration = (i == cfg.max_iterations - 1)
        if has_denial or is_last_iteration:
            if is_last_iteration and not has_denial:
                on_event(MaxIterationsReached(max_iterations=cfg.max_iterations))
            await check_cancelled()
            wire_messages = [{"role": m.role, "content": m.content} for m in scratch_history]
            try:
                final_blocks, _, final_usage = await _retry_assistant_turn(
                    client=client,
                    api=cfg.api,
                    model=cfg.model,
                    system=system,
                    messages=wire_messages,
                    tools=[],
                    max_tokens=cfg.max_tokens,
                    thinking_budget_tokens=cfg.thinking_budget_tokens,
                    cache_ttl=cfg.cache_ttl,
                    on_event=on_event,
                    cancellation_token=cancellation_token,
                )
            except TurnCancelled:
                await drain_loop_event_hooks()
                raise
            session.usage_stats.update_from_usage(final_usage)
            scratch_history.append(Message("assistant", final_blocks))
            pending_transcript_ops.append(("message", scratch_history[-1]))
            session._commit_turn(scratch_history, pending_transcript_ops)
            await fire_after_turn_hook("denial" if has_denial else "max_iter", i + 1)
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

    # Unreachable: the is_last_iteration branch inside the loop always returns.
    raise AssertionError("loop exited without returning — this is a bug")


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
        # Memory flush is a "silent" sub-conversation — we don't propagate its
        # usage onto the parent session because it's measured against an
        # ephemeral temp_history, not the user-visible one.
        assistant_blocks, stop_reason, _ = await _consume_one_assistant_turn(
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
    auto_memory_index: str | None = None
    if cfg.workspace_dir:
        auto_memory_index = load_workspace_memory_index(cfg.workspace_dir)
    return build_system_prompt(
        registry,
        cfg.workspace_dir,
        bootstrap_files,
        visible_skills,
        max_skills_in_prompt=cfg.max_skills_in_prompt,
        max_skills_prompt_chars=cfg.max_skills_prompt_chars,
        auto_memory_index=auto_memory_index,
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


async def _retry_assistant_turn(
    *,
    on_event: EventCallback,
    cancellation_token: "CancellationToken | None" = None,
    max_attempts: int = 3,
    **kwargs: Any,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """Wrap _consume_one_assistant_turn with up to max_attempts retries on API errors.

    TurnCancelled is never retried. Other exceptions are retried with 1s / 2s backoff.

    Returns ``(blocks, stop_reason, usage)`` — usage is the dict carried by
    the final ``MessageEnd`` event (Anthropic populates input_tokens /
    output_tokens; OpenAI fills it via ``stream_options.include_usage``).
    Empty dict if the provider didn't surface usage (e.g. mid-stream
    cancellation or older OpenAI-compatible backends).
    """
    for attempt in range(max_attempts):
        try:
            return await _consume_one_assistant_turn(
                **kwargs, on_event=on_event, cancellation_token=cancellation_token
            )
        except TurnCancelled:
            raise
        except Exception as exc:
            logger.warning("loop.turn.retry", f"Turn failed (attempt {attempt + 1}/{max_attempts}): {exc}")
            if attempt == max_attempts - 1:
                raise
            on_event(RetryAttempt(attempt=attempt + 1, max_attempts=max_attempts, error=str(exc)))
            await asyncio.sleep(2 ** attempt)
    raise AssertionError("unreachable")


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
    cache_ttl: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    """Stream one model response, accumulating mixed text + tool_use blocks.

    Returns ``(blocks, stop_reason, usage)``. ``usage`` carries the
    provider-reported input_tokens / output_tokens from the final
    ``MessageEnd`` (empty dict if the provider didn't surface them).
    """
    blocks: list[dict[str, Any]] = []
    text_buf = ""
    tool_bufs: dict[str, dict[str, Any]] = {}
    tool_order: list[str] = []
    stop_reason: str | None = None
    usage: dict[str, Any] = {}

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
        cache_ttl=cache_ttl,
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
            usage = ev.usage or {}

    return blocks, stop_reason, usage
