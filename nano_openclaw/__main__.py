"""Entry point.

Mirrors the chain openclaw.mjs -> src/entry.ts -> src/run-main.ts,
collapsed into one file because nano has no plugin loader, no auth-profile
resolution, and no telemetry init.

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
import sys
from pathlib import Path

from nano_openclaw.cli import repl
from nano_openclaw.loop import LoopConfig, Message, agent_loop
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
from nano_openclaw.tools import ToolRegistry, build_default_registry
from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.approvals.types import ApprovalPolicy
from rich.console import Console


def build_approval_manager(cfg, state_dir: Path) -> ApprovalManager | None:
    """Build ApprovalManager from config."""
    approvals_cfg = cfg.approvals
    if approvals_cfg.askMode == "off":
        return None
    
    allow_store = approvals_cfg.allowAlwaysStore
    if allow_store is None:
        allow_store = str(state_dir / "approvals.json")
    
    policy = ApprovalPolicy(
        ask_mode=approvals_cfg.askMode,
        security_mode=approvals_cfg.securityMode,
        dangerous_tools=approvals_cfg.dangerousTools,
        allow_always_store=allow_store,
    )
    
    manager = ApprovalManager(policy)
    existing_patterns = manager.load_allow_always_patterns()
    if existing_patterns:
        policy.allow_always_patterns = existing_patterns
    
    return manager


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

    api = "anthropic" if api_type == "anthropic-messages" else "openai"

    client = _build_client(api, api_key, base_url)
    registry = ToolRegistry() if config.noTools else build_default_registry()
    
    # Build approval manager and pass to registry
    state_dir = resolve_state_dir()
    console = Console()
    approval_manager = build_approval_manager(config, state_dir)
    registry.approval_manager = approval_manager
    registry.console = console
    
    # Resolve state directory (openclaw-aligned)
    
    # Resolve workspace directory for agent (openclaw-aligned)
    # Priority: agents.list[].workspace > agents.defaults.workspace > stateDir/workspace-{agentId} > ~/.openclaw/workspace
    workspace_dir = resolve_agent_workspace_dir(config, args.agent)
    
    # Resolve session directory for agent: {stateDir}/agents/{agentId}/sessions/
    session_dir = resolve_agent_sessions_dir(state_dir, args.agent)
    store_path = resolve_session_store_path(session_dir)

    # Handle --sessions: list and exit
    if args.sessions:
        _print_sessions_list(store_path)
        return

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
        # New session — write store immediately (store-first, like OpenClaw)
        session_id = new_session_id()
        transcript_path = session_dir / f"{session_id}.jsonl"
        transcript_writer = TranscriptWriter(transcript_path)
        transcript_writer.start(model=model_id, cwd=str(workspace_dir))
        store = load_session_store(store_path)
        update_session(store, session_id, model=model_id, message_count=0, compaction_count=0)
        save_session_store(store_path, store)

    # Resolve image model reference to extract model_id for API calls
    image_model_ref = config.resolve_image_model(args.agent)
    image_model_id: str | None = None
    if image_model_ref:
        if "/" in image_model_ref:
            image_provider_id, image_model_id = image_model_ref.split("/", 1)
            # If image model uses a different provider, warn and fall back to main provider's model
            if image_provider_id != resolved["provider_id"]:
                print(
                    f"warning: image model provider '{image_provider_id}' differs from main "
                    f"provider '{resolved['provider_id']}'; using model id '{image_model_id}' "
                    f"with the main provider endpoint",
                    file=sys.stderr,
                )
        else:
            image_model_id = image_model_ref
    
    cfg = LoopConfig(
        model=model_id,
        api=api,
        base_url=base_url,
        model_input=tuple(model_input),
        max_iterations=config.maxIterations,
        max_tokens=model_max_tokens,
        context_budget=config.context.budget,
        context_threshold=config.context.threshold,
        context_recent_turns=config.context.recent_turns,
        image_model=image_model_id,
        thinking_level=config.resolve_thinking_level(model_ref),
        # Workspace bootstrap configuration (AGENTS.md, SOUL.md, etc.)
        workspace_dir=workspace_dir,
        session_key=session_id if session_id else args.agent,
        bootstrap_max_chars=config.agents.defaults.bootstrapMaxChars,
        bootstrap_total_max_chars=config.agents.defaults.bootstrapTotalMaxChars,
    )

    repl(
        registry,
        client=client,
        cfg=cfg,
        session_dir=session_dir,
        transcript_writer=transcript_writer,
        session_id=session_id,
        store_path=store_path,
        initial_history=history if history else None,
    )


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
        from anthropic import Anthropic
        return Anthropic(api_key=api_key, base_url=base_url)
    if api == "openai":
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=base_url)
    raise ValueError(f"unsupported api: {api!r}")


if __name__ == "__main__":
    main()
