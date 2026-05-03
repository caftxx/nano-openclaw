"""Subagent runner - executes subagent runs in background.

Spawns subagent runs using asyncio tasks, reusing the main agent_loop
with filtered tools and isolated session.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from nano_openclaw.loop import (
    LoopConfig,
    Message,
    CancellationToken,
    SubagentSpawned,
    SubagentAnnounced,
    SubagentKilled,
    agent_loop,
)
from nano_openclaw.subagent.registry import SubagentRegistry, get_registry
from nano_openclaw.subagent.types import (
    SubagentConfig,
    SubagentContextMode,
    SubagentRunRecord,
    SubagentStatus,
    SpawnParams,
    build_subagent_session_key,
    parse_session_key,
)
from nano_openclaw.tools import ToolRegistry, build_default_registry
from nano_openclaw.session import TranscriptWriter


SUBAGENT_TOOL_BLACKLIST = frozenset([
    "sessions_spawn",
    "subagents",
    "sessions_list",
    "sessions_history",
    "sessions_send",
])


@dataclass
class SubagentRunnerResult:
    """Result from a subagent run."""
    run_id: str
    status: SubagentStatus
    result_text: Optional[str] = None
    elapsed_ms: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    error_message: Optional[str] = None
    transcript_path: Optional[Path] = None


@dataclass
class SubagentRunner:
    """Manages subagent execution."""
    
    registry: SubagentRegistry = field(default_factory=get_registry)
    config: SubagentConfig = field(default_factory=SubagentConfig)
    _running_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    _cancellation_tokens: dict[str, CancellationToken] = field(default_factory=dict)
    _pending_announcements: dict[str, list[Message]] = field(default_factory=dict)
    
    def can_spawn(self, requester_session_key: str) -> bool:
        """Check if spawning is allowed (concurrency limit)."""
        active_count = self.registry.count_active()
        return active_count < self.config.max_concurrent
    
    def spawn(
        self,
        params: SpawnParams,
        requester_session_key: str,
        *,
        client: Any,
        base_cfg: LoopConfig,
        session_dir: Path,
        workspace_dir: Path,
        on_event: Optional[Callable[[Any], None]] = None,
        parent_registry: Optional[ToolRegistry] = None,
    ) -> SubagentRunRecord:
        """Spawn a subagent run."""
        if not self.can_spawn(requester_session_key):
            raise RuntimeError(f"Max concurrent subagents reached ({self.config.max_concurrent})")
        
        record = self.registry.register(
            requester_session_key=requester_session_key,
            task=params.task,
            label=params.label,
            model=params.model,
            cleanup=params.cleanup.value,
        )
        
        cancellation_token = CancellationToken()
        self._cancellation_tokens[record.run_id] = cancellation_token
        
        parsed = parse_session_key(requester_session_key)
        agent_id = parsed.get("agentId", "default")
        
        model = params.model or self.config.model or base_cfg.model
        thinking = params.thinking or self.config.thinking or base_cfg.thinking_level
        
        # Build filtered registry first so the system prompt lists accurate tool names.
        subagent_registry = self._build_filtered_registry(parent_registry)

        subagent_cfg = LoopConfig(
            model=model,
            api=base_cfg.api,
            base_url=base_cfg.base_url,
            model_input=base_cfg.model_input,
            max_iterations=base_cfg.max_iterations,
            max_tokens=base_cfg.max_tokens,
            context_budget=base_cfg.context_budget,
            context_threshold=base_cfg.context_threshold,
            context_recent_turns=base_cfg.context_recent_turns,
            image_model=base_cfg.image_model,
            thinking_level=thinking,
            workspace_dir=workspace_dir,
            session_key=record.child_session_key,
            bootstrap_max_chars=base_cfg.bootstrap_max_chars,
            bootstrap_total_max_chars=base_cfg.bootstrap_total_max_chars,
            skill_filter=base_cfg.skill_filter,
            extra_skill_dirs=base_cfg.extra_skill_dirs,
            max_skill_file_bytes=base_cfg.max_skill_file_bytes,
            max_skills_in_prompt=base_cfg.max_skills_in_prompt,
            max_skills_prompt_chars=base_cfg.max_skills_prompt_chars,
            active_memory_config=None,
            dreaming_config=None,
            system_prompt_override=self._build_subagent_system_prompt(params.task, subagent_registry),
        )

        transcript_path = session_dir / f"{record.session_id}.jsonl"
        transcript_writer = TranscriptWriter(transcript_path)
        transcript_writer.start(model=model, cwd=str(workspace_dir))

        task = asyncio.create_task(
            self._run_subagent(
                record=record,
                params=params,
                cfg=subagent_cfg,
                registry=subagent_registry,
                client=client,
                transcript_writer=transcript_writer,
                cancellation_token=cancellation_token,
                parent_on_event=on_event,
            )
        )

        self._running_tasks[record.run_id] = task
        self.registry.mark_started(record.run_id)

        if on_event:
            on_event(SubagentSpawned(
                run_id=record.run_id,
                task=params.task,
                label=params.label,
                model=model,
            ))

        return record
    
    async def _run_subagent(
        self,
        record: SubagentRunRecord,
        params: SpawnParams,
        cfg: LoopConfig,
        registry: ToolRegistry,
        client: Any,
        transcript_writer: TranscriptWriter,
        cancellation_token: CancellationToken,
        parent_on_event: Optional[Callable[[Any], None]] = None,
    ) -> SubagentRunnerResult:
        """Execute a subagent run."""
        from nano_openclaw.subagent.announce import build_announce_message, should_announce
        start_time = time.time()
        result_text: Optional[str] = None
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        
        system_prompt = self._build_subagent_system_prompt(params.task)
        
        history: list[Message] = []
        
        if params.context == SubagentContextMode.FORK:
            pass
        
        user_content = [{"type": "text", "text": params.task}]
        history.append(Message("user", user_content))
        
        try:
            timeout_seconds = params.run_timeout_seconds or self.config.run_timeout_seconds
            # Subagent's internal events are suppressed from parent; only lifecycle events reach parent.
            try:
                if timeout_seconds > 0:
                    await asyncio.wait_for(
                        agent_loop(
                            user_input=params.task,
                            history=history,
                            registry=registry,
                            on_event=lambda _: None,
                            client=client,
                            cfg=cfg,
                            transcript_writer=transcript_writer,
                            cancellation_token=cancellation_token,
                        ),
                        timeout=timeout_seconds,
                    )
                else:
                    await agent_loop(
                        user_input=params.task,
                        history=history,
                        registry=registry,
                        on_event=lambda _: None,
                        client=client,
                        cfg=cfg,
                        transcript_writer=transcript_writer,
                        cancellation_token=cancellation_token,
                    )

                result_text = self._extract_result_text(history)
                elapsed_ms = int((time.time() - start_time) * 1000)

                self.registry.mark_completed(
                    record.run_id,
                    result_text=result_text,
                    elapsed_ms=elapsed_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

                runner_result = SubagentRunnerResult(
                    run_id=record.run_id,
                    status=SubagentStatus.COMPLETED,
                    result_text=result_text,
                    elapsed_ms=elapsed_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    transcript_path=transcript_writer.path,
                )
                if parent_on_event:
                    parent_on_event(SubagentAnnounced(
                        run_id=record.run_id,
                        status=SubagentStatus.COMPLETED.value,
                        task=record.task,
                        result_text=result_text,
                        elapsed_ms=elapsed_ms,
                    ))
                if should_announce(runner_result):
                    self._pending_announcements.setdefault(record.requester_session_key, []).append(
                        build_announce_message(runner_result, record)
                    )
                return runner_result

            except asyncio.TimeoutError:
                elapsed_ms = int((time.time() - start_time) * 1000)
                self.registry.mark_timeout(record.run_id)
                runner_result = SubagentRunnerResult(
                    run_id=record.run_id,
                    status=SubagentStatus.TIMEOUT,
                    elapsed_ms=elapsed_ms,
                    error_message=f"Run timed out after {timeout_seconds}s",
                )
                if parent_on_event:
                    parent_on_event(SubagentAnnounced(
                        run_id=record.run_id,
                        status=SubagentStatus.TIMEOUT.value,
                        task=record.task,
                        elapsed_ms=elapsed_ms,
                        error_message=runner_result.error_message,
                    ))
                if should_announce(runner_result):
                    self._pending_announcements.setdefault(record.requester_session_key, []).append(
                        build_announce_message(runner_result, record)
                    )
                return runner_result

            except asyncio.CancelledError:
                elapsed_ms = int((time.time() - start_time) * 1000)
                self.registry.mark_killed(record.run_id)
                runner_result = SubagentRunnerResult(
                    run_id=record.run_id,
                    status=SubagentStatus.KILLED,
                    elapsed_ms=elapsed_ms,
                    error_message="Run cancelled by user",
                )
                if parent_on_event:
                    parent_on_event(SubagentKilled(run_id=record.run_id, task=record.task))
                if should_announce(runner_result):  # → False for KILLED; LLM need not know about user-cancelled runs
                    self._pending_announcements.setdefault(record.requester_session_key, []).append(
                        build_announce_message(runner_result, record)
                    )
                return runner_result

            except Exception as exc:
                elapsed_ms = int((time.time() - start_time) * 1000)
                error_message = f"{type(exc).__name__}: {exc}"
                self.registry.mark_error(record.run_id, error_message, elapsed_ms)
                runner_result = SubagentRunnerResult(
                    run_id=record.run_id,
                    status=SubagentStatus.ERROR,
                    elapsed_ms=elapsed_ms,
                    error_message=error_message,
                )
                if parent_on_event:
                    parent_on_event(SubagentAnnounced(
                        run_id=record.run_id,
                        status=SubagentStatus.ERROR.value,
                        task=record.task,
                        elapsed_ms=elapsed_ms,
                        error_message=error_message,
                    ))
                if should_announce(runner_result):
                    self._pending_announcements.setdefault(record.requester_session_key, []).append(
                        build_announce_message(runner_result, record)
                    )
                return runner_result

        finally:
            # Always remove tracking entries once the run is done, regardless of outcome.
            self._running_tasks.pop(record.run_id, None)
            self._cancellation_tokens.pop(record.run_id, None)
    
    def _build_filtered_registry(self, parent_registry: Optional[ToolRegistry]) -> ToolRegistry:
        """Build a tool registry for subagent by inheriting from parent and removing blacklisted tools."""
        if parent_registry is None:
            # Fallback when no parent registry is provided (e.g. in tests)
            registry = build_default_registry()
            for name in SUBAGENT_TOOL_BLACKLIST:
                registry._tools.pop(name, None)
            return registry

        registry = ToolRegistry()
        for name, tool in parent_registry._tools.items():
            if name not in SUBAGENT_TOOL_BLACKLIST:
                registry.register(tool)

        # Inherit environment from parent so file tools resolve paths correctly
        # and MCP tools are available in the subagent.
        registry.approval_manager = parent_registry.approval_manager
        # Background subagents cannot safely prompt on the foreground TUI.
        registry.console = None
        registry._eligible_skills = dict(parent_registry._eligible_skills)
        if parent_registry._workspace_dir:
            registry.set_workspace_dir(parent_registry._workspace_dir)

        return registry
    
    def _build_subagent_system_prompt(self, task: str, registry: "ToolRegistry | None" = None) -> str:
        """Build system prompt for a subagent.

        Mirrors openclaw src/agents/subagent-system-prompt.ts: defines role,
        rules, and what the subagent must NOT do — keeps the child focused and
        prevents it from drifting into main-agent behaviour.
        """
        task_preview = task[:300] + ("…" if len(task) > 300 else "")

        tools_block = ""
        if registry:
            tool_names = registry.names()
            if tool_names:
                tools_block = (
                    "\n## Available Tools\n"
                    + "\n".join(f"- {n}" for n in tool_names)
                    + "\n"
                )

        return (
            "# Subagent Context\n\n"
            "You are a **sub-agent** spawned for a specific task.\n\n"
            "## Your Role\n"
            f"- You were created to handle: {task_preview}\n"
            "- Complete this task. That is your entire purpose.\n"
            "- You are NOT the main agent orchestrating the conversation.\n"
            f"{tools_block}\n"
            "## Rules\n"
            "1. **Stay focused** — Do your assigned task, nothing else.\n"
            "2. **Complete the task** — Your final message is automatically reported back to the requester.\n"
            "3. **Don't initiate** — No heartbeats, no proactive side actions, no unrequested follow-ups.\n"
            "4. **Be ephemeral** — You may be terminated after task completion. That is fine.\n"
            "5. **Recover from truncated output** — If tool output is cut off, re-read with smaller "
            "chunks (offset/limit) instead of repeating the full call.\n"
            "6. **Approvals default to denied** — If a tool says approval is required or denied, "
            "do not ask the requester to approve it and do not retry that action.\n\n"
            "## What You Do NOT Do\n"
            "- NO user conversations — that is the main agent's job.\n"
            "- NO external messages (email, Slack, etc.) unless explicitly part of your task.\n"
            "- NO expanding scope beyond the assigned task.\n\n"
            "## Finishing\n"
            "When your task is complete, provide a concise summary of what you did and found.\n"
            "Your response is delivered to the requester automatically — do not add meta-commentary.\n"
        )
    
    def _extract_result_text(self, history: list[Message]) -> str:
        """Extract result text from assistant messages."""
        for msg in reversed(history):
            if msg.role == "assistant":
                text_blocks = [
                    b.get("text", "") for b in msg.content
                    if b.get("type") == "text"
                ]
                if text_blocks:
                    return " ".join(text_blocks).strip()
        return "(no output)"
    
    async def kill(self, run_id: str) -> bool:
        """Kill a running subagent. Returns False if not found or already finished."""
        token = self._cancellation_tokens.get(run_id)
        if token is None:
            return False
        task = self._running_tasks.get(run_id)
        if task is not None and task.done():
            # Finished between spawn and kill; tracking entries will be removed by
            # the finally block in _run_subagent, nothing left to cancel.
            return False
        token.cancel()
        return True

    async def kill_all(self) -> list[str]:
        """Kill all actively running subagents (skips already-finished tasks)."""
        killed = []
        for run_id, task in list(self._running_tasks.items()):
            if not task.done():
                token = self._cancellation_tokens.get(run_id)
                if token:
                    token.cancel()
                    killed.append(run_id)
        return killed
    
    async def wait_for(self, run_id: str, timeout: float = 30.0) -> Optional[SubagentRunnerResult]:
        """Wait for a subagent to complete."""
        task = self._running_tasks.get(run_id)
        if not task:
            return None
        
        try:
            result = await asyncio.wait_for(task, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            return None

    async def wait_for_requester(self, requester_session_key: str, cancellation_token: CancellationToken | None = None) -> None:
        """Wait until all active runs for a requester session have finished."""
        while True:
            if cancellation_token and cancellation_token.is_cancelled:
                raise asyncio.CancelledError()

            run_ids = {run.run_id for run in self.registry.list_for_requester(requester_session_key)}
            tasks = [
                task
                for run_id, task in self._running_tasks.items()
                if run_id in run_ids and not task.done()
            ]
            if not tasks:
                return

            await asyncio.wait(tasks, timeout=0.1, return_when=asyncio.ALL_COMPLETED)
    
    def get_status(self, run_id: str) -> Optional[SubagentStatus]:
        """Get current status of a run."""
        record = self.registry.get(run_id)
        return record.status if record else None
    
    def cleanup_completed(self) -> list[str]:
        """Clean up completed runs from internal tracking."""
        completed = []
        for run_id, task in list(self._running_tasks.items()):
            if task.done():
                del self._running_tasks[run_id]
                self._cancellation_tokens.pop(run_id, None)
                completed.append(run_id)
        return completed

    def drain_announcements(self, session_key: str) -> list[Message]:
        """Consume and return pending announcement messages for the given session.

        Only messages whose subagent was spawned by session_key are returned,
        preventing cross-session history pollution when the user switches sessions.
        """
        return self._pending_announcements.pop(session_key, [])


_runner: Optional[SubagentRunner] = None


def get_runner(config: Optional[SubagentConfig] = None) -> SubagentRunner:
    """Get the global runner instance."""
    global _runner
    if _runner is None:
        _runner = SubagentRunner(config=config or SubagentConfig())
    return _runner


def reset_runner() -> None:
    """Reset the global runner (for testing)."""
    global _runner
    _runner = None
