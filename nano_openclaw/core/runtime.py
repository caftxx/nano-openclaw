"""Shared agent runtime builder used by both CLI and WebUI."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from nano_openclaw.services.runs import RunRegistry
    from nano_openclaw.services.runtime_update import RuntimeUpdateGuard


def _build_run_registry() -> "RunRegistry":
    """Lazy import to avoid a circular dependency between runtime and gateway."""
    from nano_openclaw.services.runs import RunRegistry
    return RunRegistry()


def _build_runtime_guard() -> "RuntimeUpdateGuard":
    """Lazy import — keeps runtime.py free of gateway imports at module load."""
    from nano_openclaw.services.runtime_update import RuntimeUpdateGuard
    return RuntimeUpdateGuard()

from nano_openclaw.approvals.exec_approvals import load_exec_approvals
from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.config import (
    load_config,
    resolve_agent_workspace_dir,
    resolve_model_config,
    resolve_state_dir,
)
from nano_openclaw.logger import get_logger, resolve_log_level
from nano_openclaw.core.loop import LoopConfig

log = get_logger(__name__)
from nano_openclaw.memory.active import ActiveMemoryConfig, PromptStyle, QueryMode
from nano_openclaw.memory.dreaming import DreamingConfig, start_dreaming_scheduler
from nano_openclaw.plugins.loader import load_plugins
from nano_openclaw.plugins.registry import HookRegistry
from nano_openclaw.session import resolve_agent_sessions_dir, resolve_session_store_path
from nano_openclaw.core.tools import ToolRegistry, build_core_registry


@dataclass
class AgentRuntime:
    agent_id: str
    session_id: str
    config: Any
    warnings: list[tuple[str, str]]
    client: Any
    registry: ToolRegistry
    cfg: LoopConfig
    hook_registry: HookRegistry
    state_dir: Path
    session_dir: Path
    store_path: Path
    workspace_dir: Path
    model_ref: str
    model_id: str
    image_model_ref: str | None
    dreaming_stop: threading.Event
    # The config path used to build this runtime — needed by Backend's
    # ``runtime_update`` so a hot-reload can re-invoke ``build_agent_runtime``
    # with the same source. ``None`` means "use default discovery".
    config_path: str | None = None
    # ``run_registry`` is the single source of truth for in-flight turn_ids
    # across chat, cron, channels — see gateway/run_registry.py. Created
    # eagerly in ``build_agent_runtime`` so cron can register against it
    # before any Backend exists.
    run_registry: "RunRegistry" = field(default_factory=lambda: _build_run_registry())
    # ``runtime_guard`` coordinates ``runtime.update`` against in-flight turns
    # — see gateway/runtime_lock.py. Same lifetime as ``run_registry``.
    runtime_guard: "RuntimeUpdateGuard" = field(default_factory=lambda: _build_runtime_guard())
    dreaming_task: Any | None = None
    cron_stop: threading.Event | None = None
    cron_task: Any | None = None
    # Process restart is supplied by the outer daemon layer. Core can schedule
    # the intent, but it must not import daemon process-control modules.
    restart_callback: Callable[[str], Any] | None = None
    # Flipped to True by the ``restart`` tool. The restart watcher waits for
    # ``run_registry`` to drain, then invokes ``restart_callback``. The slash
    # ``/restart`` path bypasses this with an immediate backend call.
    pending_restart: bool = False

    async def close(self) -> None:
        await self.hook_registry.run("session_end", {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "workspace_dir": str(self.workspace_dir),
        })
        self.dreaming_stop.set()
        if self.dreaming_task and not self.dreaming_task.done():
            self.dreaming_task.cancel()
            try:
                await self.dreaming_task
            except BaseException as e:
                log.debug("runtime.close.dreaming", f"Dreaming task cancelled: {type(e).__name__}")
                pass
        if self.cron_stop is not None:
            self.cron_stop.set()
        if self.cron_task and not self.cron_task.done():
            self.cron_task.cancel()
            try:
                await self.cron_task
            except BaseException as e:
                log.debug("runtime.close.cron", f"Cron task cancelled: {type(e).__name__}")
                pass
        if hasattr(self.client, "aclose"):
            await self.client.aclose()
        elif hasattr(self.client, "close"):
            await self.client.close()


async def build_agent_runtime(
    *,
    config_path: str | None,
    agent_id: str,
    session_id: str = "",
    model_ref_override: str | None = None,
    image_model_ref_override: str | None = None,
    console: Console | None = None,
    restart_callback: Callable[[str], Any] | None = None,
) -> AgentRuntime:
    config, warnings = load_config(config_path)
    # Apply log level from config (env var NANO_LOG_LEVEL takes precedence)
    level = resolve_log_level(config.logging.level)
    logging.getLogger().setLevel(level)
    if agent_id == "default":
        agent_id = _resolve_default_agent_id(config)
    model_ref = model_ref_override or config.resolve_primary_model(agent_id)
    resolved = resolve_model_config(model_ref, config)
    api_type = resolved["api_type"]
    api = "anthropic" if api_type == "anthropic-messages" else "openai"
    model_id = resolved["model_id"]
    base_url = resolved["base_url"]
    api_key = resolved["api_key"]
    model_input = resolved["model_input"]
    model_max_tokens = resolved["max_tokens"]
    model_context_window = resolved["context_window"]

    if model_context_window > 0 and model_max_tokens > model_context_window:
        model_max_tokens = model_context_window

    client = _build_client(api, api_key, base_url)
    state_dir = resolve_state_dir()
    session_dir = resolve_agent_sessions_dir(state_dir, agent_id)
    store_path = resolve_session_store_path(session_dir)

    no_tools = config.noTools or config.tools.noTools
    registry = ToolRegistry() if no_tools else build_core_registry()
    registry.console = console
    approval_manager = build_approval_manager(state_dir, agent_id)
    registry.approval_manager = approval_manager

    workspace_dir = resolve_agent_workspace_dir(config, agent_id)
    registry.set_workspace_dir(workspace_dir)
    registry.set_state_dir(state_dir)
    registry.set_allow_global_pip(config.skills.install.allowGlobalPip)
    config.state_dir = str(state_dir)
    hook_registry = (
        load_plugins(config.plugins, registry, config, base_dir=workspace_dir)
        if not no_tools
        else HookRegistry()
    )

    image_model_ref = (
        image_model_ref_override
        if image_model_ref_override is not None
        else config.resolve_image_model(agent_id)
    )
    image_model_id = image_model_id_from_ref(image_model_ref)

    active_mem_cfg: ActiveMemoryConfig | None = None
    if config.activeMemory and config.activeMemory.enabled:
        am = config.activeMemory
        active_mem_cfg = ActiveMemoryConfig(
            enabled=am.enabled,
            query_mode=QueryMode(am.queryMode),
            prompt_style=PromptStyle(am.promptStyle),
            model=am.model,
            thinking=am.thinking,
            timeout_ms=am.timeoutMs,
            max_summary_chars=am.maxSummaryChars,
            recent_user_turns=am.recentUserTurns,
            recent_assistant_turns=am.recentAssistantTurns,
            recent_user_chars=am.recentUserChars,
            recent_assistant_chars=am.recentAssistantChars,
            prompt_override=am.promptOverride,
            prompt_append=am.promptAppend,
            cache_ttl_ms=am.cacheTtlMs,
            logging=am.logging,
        )

    d = config.dreaming
    dreaming_cfg = DreamingConfig(
        enabled=d.enabled,
        frequency=d.frequency,
        min_score=d.minScore,
        min_recall_count=d.minRecallCount,
        min_unique_queries=d.minUniqueQueries,
        max_promotions=d.maxPromotions,
        diary=d.diary,
        model=d.model,
    )

    default_context_budget = 256000
    if config.context.budget is None:
        context_budget = model_context_window if model_context_window > 0 else default_context_budget
    else:
        context_budget = config.context.budget
        if model_context_window > 0 and context_budget > model_context_window:
            context_budget = model_context_window

    # Resolve prompt caching: only meaningful for Anthropic; OpenAI provider
    # ignores cache_ttl entirely. Disabled when api != "anthropic" or when
    # the user turned it off in config (default is on with 5m TTL).
    cache_ttl: str | None
    if api == "anthropic" and config.promptCaching.enabled:
        cache_ttl = config.promptCaching.cache_ttl
    else:
        cache_ttl = None

    cfg = LoopConfig(
        model=model_id,
        api=api,
        base_url=base_url,
        model_input=tuple(model_input),
        max_iterations=config.maxIterations,
        max_tokens=model_max_tokens,
        context_budget=context_budget,
        context_window=model_context_window,
        context_threshold=config.context.threshold,
        context_recent_turns=config.context.recent_turns,
        truncate_after_compaction=config.context.truncate_after_compaction,
        cache_ttl=cache_ttl,
        image_model=image_model_id,
        thinking_level=config.resolve_thinking_level(model_ref),
        workspace_dir=workspace_dir,
        state_dir=state_dir,
        session_key=session_id or agent_id,
        bootstrap_max_chars=config.agents.defaults.bootstrapMaxChars,
        bootstrap_total_max_chars=config.agents.defaults.bootstrapTotalMaxChars,
        skill_filter=config.resolve_skill_filter(agent_id),
        extra_skill_dirs=config.skills.load.extraDirs,
        max_skill_file_bytes=config.skills.load.maxSkillFileBytes,
        max_skills_in_prompt=config.skills.load.maxSkillsInPrompt,
        max_skills_prompt_chars=config.skills.load.maxSkillsPromptChars,
        active_memory_config=active_mem_cfg,
        memory_flush_config=config.memoryFlush,
        dreaming_config=dreaming_cfg,
        extract_memories_config=config.extractMemories,
        hook_registry=hook_registry,
    )

    if not no_tools:
        from nano_openclaw.subagent.runner import get_runner
        from nano_openclaw.subagent.types import SubagentConfig as _SubagentConfig
        get_runner(_SubagentConfig(
            max_concurrent=config.subagents.maxConcurrent,
            max_spawn_depth=config.subagents.maxSpawnDepth,
            run_timeout_seconds=config.subagents.runTimeoutSeconds,
            archive_after_minutes=config.subagents.archiveAfterMinutes,
            model=config.subagents.model,
            thinking=config.subagents.thinking.value if config.subagents.thinking else None,
        ))

    dreaming_stop = threading.Event()
    dreaming_task = None
    if dreaming_cfg.enabled and workspace_dir:
        dreaming_task = start_dreaming_scheduler(str(workspace_dir), dreaming_cfg, model_id, client, dreaming_stop)

    # Build the RunRegistry + RuntimeUpdateGuard up front so the scheduler
    # (started below) shares the same instances the EmbeddedBackend will use
    # — see gateway/run_registry.py + gateway/runtime_lock.py.
    run_registry = _build_run_registry()
    runtime_guard = _build_runtime_guard()

    cron_stop = threading.Event()
    cron_task = None
    if config.schedule.enabled and not no_tools:
        from nano_openclaw.schedule.scheduler import start_cron_scheduler
        cron_dir = state_dir / "cron"
        cron_task = start_cron_scheduler(
            cron_dir=cron_dir,
            state_dir=state_dir,
            session_dir=session_dir,
            workspace_dir=workspace_dir,
            client=client,
            base_cfg=cfg,
            max_concurrent=config.schedule.maxConcurrentRuns,
            missed_jobs_limit=config.schedule.missedJobsLimit,
            stop_event=cron_stop,
            run_registry=run_registry,
            approval_manager=registry.approval_manager,
            runtime_guard=runtime_guard,
        )

    runtime = AgentRuntime(
        agent_id=agent_id,
        session_id=session_id,
        config=config,
        warnings=warnings,
        client=client,
        registry=registry,
        cfg=cfg,
        hook_registry=hook_registry,
        state_dir=state_dir,
        session_dir=session_dir,
        store_path=store_path,
        workspace_dir=workspace_dir,
        model_ref=model_ref,
        model_id=model_id,
        image_model_ref=image_model_ref,
        dreaming_stop=dreaming_stop,
        run_registry=run_registry,
        runtime_guard=runtime_guard,
        dreaming_task=dreaming_task,
        cron_stop=cron_stop,
        cron_task=cron_task,
        config_path=config_path,
        restart_callback=restart_callback,
    )
    if not no_tools:
        _register_restart_tool(runtime)

    await hook_registry.run("session_start", {
        "session_id": session_id,
        "agent_id": agent_id,
        "workspace_dir": str(workspace_dir),
    })
    return runtime


def _register_restart_tool(runtime: AgentRuntime) -> None:
    """Wire the LLM-facing ``restart`` tool into the runtime's ToolRegistry.

    Lives here (rather than ``tools.py``) so the tool's closure can hold
    ``runtime`` directly — the tool needs to flip ``runtime.pending_restart``
    and spawn a watcher task that fires the injected restart callback only after the
    ``run_registry`` drains. Approval gating is handled by the registry's
    dispatch path: ``ApprovalPolicy`` ships ``restart`` in ``dangerous_tools``
    + a ``tool_configs`` entry with ``requires_approval=True``, so cron /
    channel auto-runs go through ``NonInteractiveApprovalHandler`` and are
    denied unless the user explicitly allowlists it.
    """
    import asyncio

    from nano_openclaw.core.tools import Tool

    async def _wait_and_restart(rt: AgentRuntime, strategy: str) -> None:
        # Wait until the calling turn (and any other in-flight turns) finish.
        # Polling is fine here — the loop fires once the registry drains.
        while len(rt.run_registry) > 0:
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.2)  # final flush window
        if rt.restart_callback is None:
            log.warning("runtime.restart.unavailable", "restart requested without daemon callback")
            return
        rt.restart_callback(strategy)

    # Re-entrancy guard: multiple ``restart`` calls in one process should not
    # stack watcher tasks. The first one wins; subsequent calls just confirm.
    state: dict[str, Any] = {"watcher_started": False}

    def _restart_tool(_args: dict[str, Any]) -> str:
        runtime.pending_restart = True
        if state["watcher_started"]:
            return "restart already pending — will fire after current turn(s) complete"

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return "restart cannot be scheduled: no running event loop"

        strategy = runtime.config.gateway.restart_strategy
        loop.create_task(_wait_and_restart(runtime, strategy))
        state["watcher_started"] = True
        return f"restart scheduled (strategy={strategy}); will fire once the registry drains"

    runtime.registry.register(Tool(
        name="restart",
        description=(
            "Restart the gateway daemon process. Defers until the current "
            "turn (and any other in-flight turns) finish — the response you "
            "produce after calling this will be delivered before the swap. "
            "Use sparingly: clients lose their WebSocket connection and have "
            "to reconnect; cron / channel jobs in flight are interrupted."
        ),
        input_schema={"type": "object", "properties": {}},
        run=_restart_tool,
    ))


def image_model_id_from_ref(image_model_ref: str | None) -> str | None:
    if not image_model_ref:
        return None
    return image_model_ref.split("/", 1)[1] if "/" in image_model_ref else image_model_ref


def _resolve_default_agent_id(config: Any) -> str:
    for agent in config.agents.list:
        if agent.default:
            return agent.id
    if config.agents.list:
        return config.agents.list[0].id
    return "default"


def build_approval_manager(state_dir: Path, agent_id: str) -> ApprovalManager | None:
    policy = load_exec_approvals(state_dir, agent_id)
    if policy.ask_mode == "off":
        return None
    return ApprovalManager(policy)


def _build_client(api: str, api_key: str, base_url: str | None) -> Any:
    if api == "anthropic":
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(api_key=api_key, base_url=base_url)
    if api == "openai":
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=api_key, base_url=base_url)
    raise ValueError(f"unsupported api: {api!r}")
