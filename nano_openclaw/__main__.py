"""Entry point.

Mirrors the chain openclaw.mjs -> src/entry.ts -> src/run-main.ts,
collapsed into one file because nano skips auth-profile resolution and telemetry
init while keeping a lightweight plugin loader.

Configuration is loaded using openclaw-aligned path resolution:
1. OPENCLAW_CONFIG_PATH environment variable
2. {stateDir}/nano-openclaw.json5
3. {cwd}/workspace/nano-openclaw.json5
4. ~/.openclaw/nano-openclaw.json5

Session storage aligns with openclaw:
- {stateDir}/agents/{agentId}/sessions/
- Supports multi-agent session isolation

Model reference format: provider/model-id (e.g., anthropic/claude-sonnet-4-5)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from pathlib import Path

from nano_openclaw.cli import repl
from nano_openclaw.loop import LoopConfig, Message, agent_loop
from nano_openclaw.memory.active import ActiveMemoryConfig, QueryMode, PromptStyle
from nano_openclaw.memory.dreaming import DreamingConfig, start_dreaming_scheduler
from nano_openclaw.config import (
    load_config,
    resolve_model_config,
    resolve_state_dir,
    resolve_agent_workspace_dir,
)
from nano_openclaw.session import (
    TranscriptWriter,
    TranscriptReader,
    load_session_store,
    save_session_store,
    update_session,
    get_last_session,
    list_sessions,
    new_session_id,
    resolve_agent_sessions_dir,
    resolve_session_store_path,
)
from nano_openclaw.plugins.loader import load_plugins
from nano_openclaw.plugins.registry import HookRegistry
from nano_openclaw.tools import ToolRegistry, build_core_registry
from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.approvals.exec_approvals import load_exec_approvals
from rich.console import Console


def build_approval_manager(state_dir: Path, agent_id: str) -> ApprovalManager | None:
    """Build ApprovalManager from exec-approvals.json."""
    policy = load_exec_approvals(state_dir, agent_id)
    if policy.ask_mode == "off":
        return None
    return ApprovalManager(policy)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nano-openclaw",
        description="Minimal educational reimplementation of OpenClaw's agent loop.",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to config file (or use OPENCLAW_CONFIG_PATH)",
    )
    parser.add_argument(
        "--agent",
        metavar="AGENT_ID",
        default="default",
        help="Agent ID for session isolation (default: default)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume the last session from transcript file",
    )
    parser.add_argument(
        "--sessions",
        action="store_true",
        help="List all saved sessions and exit",
    )
    args = parser.parse_args()

    # Load configuration
    config, warnings = load_config(args.config)

    for var_name, config_path in warnings:
        print(
            f"warning: missing env var \"{var_name}\" at {config_path} - "
            f"feature using this value will be unavailable",
            file=sys.stderr,
        )

    # Resolve model for agent
    model_ref = config.resolve_primary_model(args.agent)

    try:
        resolved = resolve_model_config(model_ref, config)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    api_type = resolved["api_type"]
    model_id = resolved["model_id"]
    base_url = resolved["base_url"]
    api_key = resolved["api_key"]
    model_input = resolved["model_input"]
    model_max_tokens = resolved["max_tokens"]
    model_context_window = resolved["context_window"]

    if model_context_window > 0 and model_max_tokens > model_context_window:
        print(
            f"warning: maxTokens ({model_max_tokens}) exceeds contextWindow ({model_context_window}), clamping",
            file=sys.stderr,
        )
        model_max_tokens = model_context_window

    api = "anthropic" if api_type == "anthropic-messages" else "openai"

    # --sessions: list and exit (no async needed)
    state_dir = resolve_state_dir()
    session_dir = resolve_agent_sessions_dir(state_dir, args.agent)
    store_path = resolve_session_store_path(session_dir)

    if args.sessions:
        _print_sessions_list(store_path)
        return

    asyncio.run(_async_main(
        args=args,
        config=config,
        api=api,
        api_key=api_key,
        base_url=base_url,
        model_id=model_id,
        model_ref=model_ref,
        model_input=model_input,
        model_max_tokens=model_max_tokens,
        model_context_window=model_context_window,
        resolved=resolved,
        state_dir=state_dir,
        session_dir=session_dir,
        store_path=store_path,
    ))


async def _async_main(
    *,
    args,
    config,
    api: str,
    api_key: str,
    base_url: str | None,
    model_id: str,
    model_ref: str,
    model_input,
    model_max_tokens: int,
    model_context_window: int,
    resolved: dict,
    state_dir: Path,
    session_dir: Path,
    store_path: Path,
) -> None:
    client = _build_client(api, api_key, base_url)
    no_tools = config.noTools or config.tools.noTools
    registry = ToolRegistry() if no_tools else build_core_registry()

    console = Console()
    approval_manager = build_approval_manager(state_dir, args.agent)
    registry.approval_manager = approval_manager
    registry.console = console

    workspace_dir = resolve_agent_workspace_dir(config, args.agent)
    registry.set_workspace_dir(workspace_dir)
    hook_registry = (
        load_plugins(config.plugins, registry, config)
        if not no_tools
        else HookRegistry()
    )

    # Build session: new or resumed
    transcript_writer: TranscriptWriter | None = None
    session_id = ""
    history = []

    if args.resume:
        store = load_session_store(store_path)
        last = get_last_session(store)
        if last:
            session_id = last.session_id
            transcript_path = session_dir / f"{session_id}.jsonl"
            reader = TranscriptReader(transcript_path)
            history, loaded_id, msg_count, comp_count, last_msg_id = reader.load_history()
            transcript_writer = TranscriptWriter.resume(
                transcript_path, session_id, msg_count, comp_count, last_msg_id
            )
            print(f"resumed session {session_id[:8]}… ({msg_count} messages, {comp_count} compactions)", file=sys.stderr)
        else:
            print("no previous session to resume — starting fresh", file=sys.stderr)

    if not transcript_writer:
        session_id = new_session_id()
        transcript_path = session_dir / f"{session_id}.jsonl"
        transcript_writer = TranscriptWriter(transcript_path)
        transcript_writer.start(model=model_id, cwd=str(workspace_dir))
        store = load_session_store(store_path)
        update_session(store, session_id, model=model_id, message_count=0, compaction_count=0)
        save_session_store(store_path, store)

    # Resolve image model
    image_model_ref = config.resolve_image_model(args.agent)
    image_model_id: str | None = None
    if image_model_ref:
        if "/" in image_model_ref:
            image_provider_id, image_model_id = image_model_ref.split("/", 1)
            if image_provider_id != resolved["provider_id"]:
                print(
                    f"warning: image model provider '{image_provider_id}' differs from main "
                    f"provider '{resolved['provider_id']}'; using model id '{image_model_id}' "
                    f"with the main provider endpoint",
                    file=sys.stderr,
                )
        else:
            image_model_id = image_model_ref

    # Build Active Memory config
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

    # Build Dreaming config
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

    _DEFAULT_CONTEXT_BUDGET = 256000
    if config.context.budget is None:
        context_budget = model_context_window if model_context_window > 0 else _DEFAULT_CONTEXT_BUDGET
    else:
        context_budget = config.context.budget
        if model_context_window > 0 and context_budget > model_context_window:
            print(
                f"warning: context.budget ({context_budget}) exceeds contextWindow ({model_context_window}), clamping",
                file=sys.stderr,
            )
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
        session_key=session_id if session_id else args.agent,
        bootstrap_max_chars=config.agents.defaults.bootstrapMaxChars,
        bootstrap_total_max_chars=config.agents.defaults.bootstrapTotalMaxChars,
        skill_filter=config.resolve_skill_filter(args.agent),
        extra_skill_dirs=config.skills.load.extraDirs,
        max_skill_file_bytes=config.skills.load.maxSkillFileBytes,
        max_skills_in_prompt=config.skills.load.maxSkillsInPrompt,
        max_skills_prompt_chars=config.skills.load.maxSkillsPromptChars,
        active_memory_config=active_mem_cfg,
        memory_flush_config=config.memoryFlush,
        dreaming_config=dreaming_cfg,
        hook_registry=hook_registry,
    )

    # Start dreaming scheduler as asyncio task (replaces background thread)
    _dreaming_stop = threading.Event()
    dreaming_task = None
    if dreaming_cfg.enabled and workspace_dir:
        dreaming_task = start_dreaming_scheduler(str(workspace_dir), dreaming_cfg, model_id, client, _dreaming_stop)

    # Wire config.subagents to the global SubagentRunner before the REPL starts.
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

    await hook_registry.run("session_start", {
        "session_id": session_id,
        "agent_id": args.agent,
        "workspace_dir": str(workspace_dir),
    })

    try:
        await repl(
            registry,
            client=client,
            cfg=cfg,
            session_dir=session_dir,
            transcript_writer=transcript_writer,
            session_id=session_id,
            store_path=store_path,
            initial_history=history if history else None,
        )
    finally:
        await hook_registry.run("session_end", {
            "session_id": session_id,
            "agent_id": args.agent,
            "workspace_dir": str(workspace_dir),
        })
        _dreaming_stop.set()
        if dreaming_task and not dreaming_task.done():
            dreaming_task.cancel()
            try:
                await dreaming_task
            except (asyncio.CancelledError, Exception):
                pass

def _print_sessions_list(store_path: Path) -> None:
    """Print saved sessions to stdout."""
    store = load_session_store(store_path)
    sessions = list_sessions(store)
    if not sessions:
        print("no saved sessions")
        return
    from datetime import datetime, timezone
    print(f"{'ID':<38} {'Model':<25} {'Messages':>8} {'Compactions':>11} {'Last Active'}")
    print("-" * 100)
    for s in sessions:
        marker = " ← current" if s.session_id == store.get("lastSessionId") else ""
        last_active = datetime.fromtimestamp(s.updated_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"{s.session_id:<36}{marker} {s.model or '(unknown)':<25} {s.message_count:>8} {s.compaction_count:>11} {last_active}"
        )


def _build_client(api: str, api_key: str, base_url: str | None):
    if api == "anthropic":
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(api_key=api_key, base_url=base_url)
    if api == "openai":
        from openai import AsyncOpenAI
        return AsyncOpenAI(api_key=api_key, base_url=base_url)
    raise ValueError(f"unsupported api: {api!r}")


if __name__ == "__main__":
    main()
