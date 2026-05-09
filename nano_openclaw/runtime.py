"""Shared agent runtime builder used by both CLI and WebUI."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from nano_openclaw.approvals.exec_approvals import load_exec_approvals
from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.config import (
    load_config,
    resolve_agent_workspace_dir,
    resolve_model_config,
    resolve_state_dir,
)
from nano_openclaw.loop import LoopConfig
from nano_openclaw.memory.active import ActiveMemoryConfig, PromptStyle, QueryMode
from nano_openclaw.memory.dreaming import DreamingConfig, start_dreaming_scheduler
from nano_openclaw.plugins.loader import load_plugins
from nano_openclaw.plugins.registry import HookRegistry
from nano_openclaw.session import resolve_agent_sessions_dir, resolve_session_store_path
from nano_openclaw.tools import ToolRegistry, build_core_registry


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
    dreaming_task: Any | None = None
    cron_stop: threading.Event | None = None
    cron_task: Any | None = None

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
            except BaseException:
                pass
        if self.cron_stop is not None:
            self.cron_stop.set()
        if self.cron_task and not self.cron_task.done():
            self.cron_task.cancel()
            try:
                await self.cron_task
            except BaseException:
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
) -> AgentRuntime:
    config, warnings = load_config(config_path)
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
        image_model=image_model_id,
        thinking_level=config.resolve_thinking_level(model_ref),
        workspace_dir=workspace_dir,
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

    cron_stop = threading.Event()
    cron_task = None
    if config.schedule.enabled and not no_tools:
        from nano_openclaw.schedule.scheduler import start_cron_scheduler
        cron_dir = state_dir / "cron"
        cron_task = start_cron_scheduler(
            cron_dir=cron_dir,
            session_dir=session_dir,
            workspace_dir=workspace_dir,
            client=client,
            base_cfg=cfg,
            max_concurrent=config.schedule.maxConcurrentRuns,
            missed_jobs_limit=config.schedule.missedJobsLimit,
            stop_event=cron_stop,
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
        dreaming_task=dreaming_task,
        cron_stop=cron_stop,
        cron_task=cron_task,
    )
    await hook_registry.run("session_start", {
        "session_id": session_id,
        "agent_id": agent_id,
        "workspace_dir": str(workspace_dir),
    })
    return runtime


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
