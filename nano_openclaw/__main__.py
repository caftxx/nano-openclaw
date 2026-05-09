"""Entry point.

Mirrors the chain openclaw.mjs -> src/entry.ts -> src/run-main.ts,
collapsed into one file because nano skips auth-profile resolution and telemetry
init while keeping a lightweight plugin loader.

Configuration is loaded using openclaw-aligned path resolution:
1. NANO_OPENCLAW_CONFIG_PATH environment variable
2. {stateDir}/nano-openclaw.json5
3. {cwd}/workspace/nano-openclaw.json5
4. ~/.nano-openclaw/nano-openclaw.json5

Session storage aligns with openclaw:
- {stateDir}/agents/{agentId}/sessions/
- Supports multi-agent session isolation

Model reference format: provider/model-id (e.g., anthropic/claude-sonnet-4-5)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from nano_openclaw.cli import repl
from nano_openclaw.runtime import build_agent_runtime
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
from nano_openclaw.config import resolve_state_dir
from rich.console import Console


def main() -> None:
    _common = argparse.ArgumentParser(add_help=False)
    _common.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Path to config file (or use NANO_OPENCLAW_CONFIG_PATH)",
    )
    _common.add_argument(
        "--agent",
        metavar="AGENT_ID",
        default="default",
        help="Agent ID for session isolation (default: default)",
    )

    parser = argparse.ArgumentParser(
        prog="nano-openclaw",
        description="Minimal educational reimplementation of OpenClaw's agent loop.",
        parents=[_common],
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
    subparsers = parser.add_subparsers(dest="command")

    web_parser = subparsers.add_parser("web", help="Start the WebUI server", parents=[_common])
    web_parser.add_argument("--host", default="127.0.0.1", metavar="HOST",
                            help="Host to bind to (default: 127.0.0.1)")
    web_parser.add_argument("--port", type=int, default=8765, metavar="PORT",
                            help="Port to listen on (default: 8765)")
    web_parser.add_argument("--token", metavar="TOKEN", default=None,
                            help="Bearer token for API authentication")

    args = parser.parse_args()

    if args.command == "web":
        from nano_openclaw.webui.server import main as web_main
        web_argv = ["--host", args.host, "--port", str(args.port), "--agent", args.agent]
        if args.config:
            web_argv += ["--config", args.config]
        if args.token:
            web_argv += ["--token", args.token]
        web_main(web_argv)
        return

    state_dir = resolve_state_dir()
    session_dir = resolve_agent_sessions_dir(state_dir, args.agent)
    store_path = resolve_session_store_path(session_dir)

    if args.sessions:
        _print_sessions_list(store_path)
        return

    asyncio.run(_async_main(args=args, session_dir=session_dir, store_path=store_path))


async def _async_main(*, args, session_dir: Path, store_path: Path) -> None:
    # Resolve session before building runtime so we have the real session_id
    transcript_writer: TranscriptWriter | None = None
    session_id = ""
    history = []

    if args.resume:
        store = load_session_store(store_path)
        last = get_last_session(store)
        if last:
            session_id = last.session_id
            transcript_path = session_dir / f"{session_id}.jsonl"
            if transcript_path.exists():
                reader = TranscriptReader(transcript_path)
                history, _, msg_count, comp_count, last_msg_id = reader.load_history()
                transcript_writer = TranscriptWriter.resume(
                    transcript_path, session_id, msg_count, comp_count, last_msg_id
                )
                print(
                    f"resumed session {session_id[:8]}… ({msg_count} messages, {comp_count} compactions)",
                    file=sys.stderr,
                )
            else:
                print("last session has no transcript — starting fresh", file=sys.stderr)
        else:
            print("no previous session to resume — starting fresh", file=sys.stderr)

    if not session_id:
        session_id = new_session_id()

    console = Console()
    runtime = await build_agent_runtime(
        config_path=args.config,
        agent_id=args.agent,
        session_id=session_id,
        console=console,
    )

    for var_name, config_path in runtime.warnings:
        print(
            f"warning: missing env var \"{var_name}\" at {config_path} - "
            f"feature using this value will be unavailable",
            file=sys.stderr,
        )

    # Finalise transcript writer (needs model_id and workspace_dir from runtime)
    if not transcript_writer:
        transcript_path = runtime.session_dir / f"{session_id}.jsonl"
        transcript_writer = TranscriptWriter(transcript_path)
        transcript_writer.start(model=runtime.model_id, cwd=str(runtime.workspace_dir))
        # Defer sessions.json entry to first actual message so empty sessions leave
        # neither a file nor a store entry behind.
        _sid, _mid, _sp, _tw = session_id, runtime.model_id, runtime.store_path, transcript_writer
        def _persist_new_session() -> None:
            _store = load_session_store(_sp)
            update_session(
                _store,
                _sid,
                model=_mid,
                message_count=_tw.message_count,
                compaction_count=_tw.compaction_count,
            )
            save_session_store(_sp, _store)
        transcript_writer._on_first_write = _persist_new_session

    try:
        await repl(
            runtime.registry,
            client=runtime.client,
            cfg=runtime.cfg,
            session_dir=runtime.session_dir,
            transcript_writer=transcript_writer,
            session_id=session_id,
            store_path=runtime.store_path,
            initial_history=history if history else None,
        )
    finally:
        await runtime.close()


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


if __name__ == "__main__":
    main()
