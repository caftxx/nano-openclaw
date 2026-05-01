"""Entry point.

Mirrors the chain ``openclaw.mjs`` -> ``src/entry.ts`` -> ``src/run-main.ts``,
collapsed into one file because nano has no plugin loader, no auth-profile
resolution, and no telemetry init.

Configuration is loaded from:
1. Default: ./nano-openclaw.json5 (current directory)
2. Custom: --config <path>

Model reference format: provider/model-id (e.g., anthropic/claude-sonnet-4-5)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nano_openclaw.cli import repl
from nano_openclaw.loop import LoopConfig
from nano_openclaw.config import (
    DEFAULT_CONFIG_FILENAME,
    load_config,
    resolve_model_config,
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
)
from nano_openclaw.tools import ToolRegistry, build_default_registry


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nano-openclaw",
        description="Minimal educational reimplementation of OpenClaw's agent loop.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
        f"Configuration is loaded from {DEFAULT_CONFIG_FILENAME} in the current directory.\n"
        "Use --config to specify a custom config file.\n"
        "\n"
        "Model reference format: provider/model-id (e.g., anthropic/claude-sonnet-4-5)\n"
        "Custom providers can be defined in the config file under models.providers.\n"
        "\n"
        "Session persistence:\n"
        "  --resume                             Resume the last session from transcript\n"
        "  --sessions                           List all saved sessions and exit\n"
        "  Session files are stored in .nano-sessions/ next to the config file.\n"
        "\n"
        "Configurable options (with defaults):\n"
        "  agents.model                           Main model (anthropic/claude-sonnet-4-5-20250929)\n"
        "  agents.imageModel                      Image understanding model (None = Native Vision)\n"
        "  models.providers.<id>.baseUrl          Custom API endpoint (None = default)\n"
        "  models.providers.<id>.apiKey           API key, supports ${ENV_VAR} syntax\n"
        "  models.providers.<id>.api              API type (anthropic-messages|openai-completions|openai-responses)\n"
        "  models.providers.<id>.models[]         Model catalog with id, name, input, contextWindow, maxTokens\n"
        "  models.mode                            Provider catalog mode (merge|replace)\n"
        "  noTools                                Run as plain chatbot, no tools (false)\n"
        "  maxIterations                          Max tool-use rounds per user turn (12)\n"
        "  maxTokens                              Max tokens per assistant response (4096)\n"
        "  context.budget                         Maximum token budget for context window (100000)\n"
        "  context.threshold                      Trigger compaction at this fraction (0.8)\n"
        "  context.recent_turns                   Recent turns to preserve during compaction (3)\n"
        "\n"
        'Example config file (JSON5 — supports comments and trailing commas):\n'
        '  {\n'
        '    // Main model (provider/model-id format)\n'
        '    agents: {\n'
        '      model: "openrouter/anthropic/claude-sonnet-4",\n'
        '      imageModel: "openai/gpt-4o-mini",  // for Media Understanding path\n'
        '    },\n'
        '    models: {\n'
        '      providers: {\n'
        '        "openrouter": {\n'
        '          baseUrl: "https://openrouter.ai/api/v1",\n'
        '          apiKey: "${OPENROUTER_API_KEY}",  // env var substitution\n'
        '          models: [\n'
        '            { id: "anthropic/claude-sonnet-4", name: "Claude Sonnet 4" },\n'
        '          ],\n'
        '        },\n'
        '      },\n'
        '    },\n'
        '    // Runtime settings\n'
        '    maxIterations: 12,\n'
        '    context: {\n'
        '      budget: 100000,\n'
        '      threshold: 0.8,\n'
        '    },\n'
        '  }'
    ),
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=f"Path to config file (default: ./{DEFAULT_CONFIG_FILENAME})",
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

    config, warnings = load_config(args.config)
    
    for var_name, config_path in warnings:
        print(
            f"warning: missing env var \"{var_name}\" at {config_path} - "
            f"feature using this value will be unavailable",
            file=sys.stderr,
        )

    model_ref = config.resolve_primary_model()
    
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

    api = "anthropic" if api_type == "anthropic-messages" else "openai"

    client = _build_client(api, api_key, base_url)
    registry = ToolRegistry() if config.no_tools else build_default_registry()
    
    # Resolve session directory (next to config file)
    config_path = Path(args.config) if args.config else Path.cwd() / DEFAULT_CONFIG_FILENAME
    config_dir = config_path.parent.resolve()
    session_dir = config_dir / ".nano-sessions"
    store_path = session_dir / "sessions.json"

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
        transcript_writer.start(model=model_id, cwd=str(config_dir))
        store = load_session_store(store_path)
        update_session(store, session_id, model=model_id, message_count=0, compaction_count=0)
        save_session_store(store_path, store)

    # Resolve image model reference to extract model_id for API calls
    image_model_ref = config.resolve_image_model()
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
        max_iterations=config.max_iterations,
        max_tokens=config.max_tokens,
        context_budget=config.context.budget,
        context_threshold=config.context.threshold,
        context_recent_turns=config.context.recent_turns,
        image_model=image_model_id,
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