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

Subcommands:
- ``tui`` (default if none given): interactive REPL — embedded mode for now;
  ``--connect`` will attach to a remote gateway in Phase 5.
- ``gateway {status|start|stop|run}``: daemon supervisor + foreground runner.
  Hosts WebUI + WeChat channels + (Phase 4) the WebSocket /rpc endpoint.

Phase 3 dropped the standalone ``web`` and ``wechat`` subcommands — both now
run inside the daemon. To get a webui, ``nano-openclaw gateway start`` and
open ``http://127.0.0.1:5000``. To run wechat, configure ``wechat.accounts``
in nano-openclaw.json5 and start the gateway.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from nano_openclaw.cli import repl
from nano_openclaw.config import resolve_state_dir
from nano_openclaw.gateway.cli import add_gateway_subparser, run_gateway_cli
from nano_openclaw.logger import setup_logging
from nano_openclaw.runtime import build_agent_runtime
from nano_openclaw.session import (
    TranscriptReader,
    TranscriptWriter,
    get_last_session,
    list_sessions,
    load_session_store,
    new_session_id,
    resolve_agent_sessions_dir,
    resolve_session_store_path,
    save_session_store,
    update_session,
)
from rich.console import Console


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nano-openclaw",
        description="Minimal educational reimplementation of OpenClaw's agent loop.",
    )
    # Top-level convenience flags forwarded to ``tui`` (back-compat). Were the
    # default invocation in pre-Phase 3 builds; phased out gradually.
    parser.add_argument(
        "--resume",
        action="store_true",
        help="(legacy) Equivalent to `tui --resume`",
    )
    parser.add_argument(
        "--sessions",
        action="store_true",
        help="(legacy) Equivalent to `tui --list-sessions`",
    )
    subparsers = parser.add_subparsers(dest="command")

    tui_parser = subparsers.add_parser(
        "tui",
        help="Start the interactive REPL (auto-detects local gateway, falls back to embedded)",
    )
    tui_parser.add_argument(
        "--connect",
        metavar="URL",
        default=None,
        help="Connect to a remote gateway over WebSocket (Phase 5; not yet implemented)",
    )
    tui_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume the last session from transcript file",
    )
    tui_parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List saved sessions and exit",
    )

    add_gateway_subparser(subparsers)

    # WeChat (iLink) management subcommand. Currently exposes ``login`` for
    # the QR-code login flow; runs in-process and exits, separate from the
    # daemon long-poll loop so the QR can be shown interactively in the
    # terminal.
    wechat_parser = subparsers.add_parser(
        "wechat",
        help="WeChat (iLink) management commands (login, …)",
    )
    wechat_sub = wechat_parser.add_subparsers(dest="wechat_command")
    wechat_login_p = wechat_sub.add_parser(
        "login",
        help="Run the QR login flow and persist the resulting bot token",
    )
    wechat_login_p.add_argument(
        "--account",
        default="default",
        help="Account id to log in (matches wechat.accounts[*].id; default: 'default')",
    )

    args = parser.parse_args()

    state_dir = resolve_state_dir()
    setup_logging(state_dir)

    # Config path resolution: ``$NANO_OPENCLAW_CONFIG_PATH`` env var still
    # works (handled inside ``load_config``); the CLI flag was removed in
    # favor of the env var + the standard search-path priority chain.
    # Agent isolation: only the default agent's session dir is used by the
    # CLI now; multi-agent setups go through ``config.agents.list``.
    config_path: str | None = None
    agent_id: str = "default"

    # ── Subcommand dispatch ─────────────────────────────────────────────────

    if args.command == "gateway":
        sys.exit(run_gateway_cli(args))

    if args.command == "wechat":
        if args.wechat_command == "login":
            from nano_openclaw.wechat.login_cli import run_wechat_login
            sys.exit(asyncio.run(run_wechat_login(
                config_path=config_path,
                account_id=args.account,
            )))
        wechat_parser.error("missing wechat subcommand (try `wechat login`)")

    # ── tui (explicit or default) ───────────────────────────────────────────
    # Back-compat: top-level --resume / --sessions still work; they get
    # routed to tui as if the user had typed `tui --resume` etc.

    session_dir = resolve_agent_sessions_dir(state_dir, agent_id)
    store_path = resolve_session_store_path(session_dir)

    is_tui = args.command == "tui" or args.command is None

    if is_tui:
        # Pull list-sessions / resume / connect from the right namespace.
        # (Top-level `--sessions` mirrors `tui --list-sessions`.)
        list_only = (args.command == "tui" and getattr(args, "list_sessions", False)) or (
            args.command is None and args.sessions
        )
        if list_only:
            _print_sessions_list(store_path)
            return

        explicit_connect = bool(args.command == "tui" and getattr(args, "connect", None))
        connect_url = getattr(args, "connect", None) if args.command == "tui" else None

        # Resolve the daemon URL in three steps, highest priority first:
        #
        # 1. ``--connect`` CLI flag (explicit user request)
        # 2. ``NANO_OPENCLAW_GATEWAY_URL`` env var (cross-container / explicit
        #    deployment override; needed when pid + port discovery fails —
        #    e.g. Docker: TUI container can't see gateway container's PID and
        #    can't reach its bind host directly, so this env var points to
        #    the service-name DNS, ``ws://gateway:5000/rpc``)
        # 3. Pidfile-based auto-detect (single-host default — works when TUI
        #    and daemon share the same machine + state_dir)
        #
        # Falls through to embedded mode when none of the three resolves.
        import os as _os_for_env
        if not connect_url:
            connect_url = (_os_for_env.environ.get("NANO_OPENCLAW_GATEWAY_URL") or "").strip() or None

        if not connect_url:
            from nano_openclaw.gateway.pidfile import gateway_status as _gw_status
            status = _gw_status(state_dir)
            if status.running and status.entry is not None:
                connect_url = f"ws://{status.entry.host}:{status.entry.port}/rpc"

        if connect_url:
            connected = asyncio.run(_run_ws_tui(connect_url))
            if connected:
                return
            # Connect failed. Honor explicit user intent by exiting; for
            # auto-detected URLs (env var / pidfile), gracefully fall back
            # to embedded mode in this process.
            if explicit_connect:
                sys.exit(1)
            from rich.console import Console as _Console
            _Console().print("[dim]falling back to embedded mode[/]")

        # Top-level --resume or `tui --resume` both surface the same flag here.
        resume_flag = getattr(args, "resume", False)

        # tui (default + explicit) goes through EmbeddedBackend so the Backend
        # path is exercised end-to-end. The legacy direct-AgentSession path is
        # gone in Phase 3 — tests still cover it via the `backend=None` branch
        # of `cli.repl()`, but the user-facing default is Backend-mediated.
        asyncio.run(
            _async_main(
                config=config_path,
                agent=agent_id,
                resume=resume_flag,
                session_dir=session_dir,
                store_path=store_path,
                use_backend=True,
            )
        )
        return

    # Unrecognized — argparse should have already errored, but be defensive.
    parser.error(f"unknown command: {args.command!r}")


async def _async_main(
    *,
    config: str | None,
    agent: str,
    resume: bool,
    session_dir: Path,
    store_path: Path,
    use_backend: bool = False,
) -> None:
    # Resolve session before building runtime so we have the real session_id
    transcript_writer: TranscriptWriter | None = None
    session_id = ""
    history = []

    if resume:
        store = load_session_store(store_path)
        last = get_last_session(store)
        if last:
            session_id = last.session_id
            transcript_path = session_dir / f"{session_id}.jsonl"
            if transcript_path.exists():
                if not use_backend:
                    # Legacy path reads history into a list; backend path lets
                    # the BackendSessionManager load it on demand.
                    reader = TranscriptReader(transcript_path)
                    history, _, msg_count, comp_count, last_msg_id = reader.load_history()
                    transcript_writer = TranscriptWriter.resume(
                        transcript_path, session_id, msg_count, comp_count, last_msg_id
                    )
                print(
                    f"resumed session {session_id[:8]}…",
                    file=sys.stderr,
                )
            else:
                print("last session has no transcript — starting fresh", file=sys.stderr)
                session_id = ""
        else:
            print("no previous session to resume — starting fresh", file=sys.stderr)

    console = Console()
    runtime = await build_agent_runtime(
        config_path=config,
        agent_id=agent,
        session_id=session_id or new_session_id(),  # runtime needs a key for cron / cfg.session_key
        console=console,
    )

    for var_name, cfg_path in runtime.warnings:
        print(
            f"warning: missing env var \"{var_name}\" at {cfg_path} - "
            f"feature using this value will be unavailable",
            file=sys.stderr,
        )

    backend = None
    if use_backend:
        # Backend mode: BackendSessionManager owns transcript_writer + history.
        # Don't pre-create either here — repl() asks the manager for the session
        # entity, and the manager either loads ``session_id`` from disk or
        # creates a fresh one.
        from nano_openclaw.gateway.backend_embedded import EmbeddedBackend
        backend = EmbeddedBackend(runtime)
    else:
        # Legacy mode: pre-create writer so repl() inherits it.
        if not session_id:
            session_id = new_session_id()
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
            backend=backend,
        )
    finally:
        if backend is not None:
            await backend.aclose()
        await runtime.close()


async def _run_ws_tui(connect_url: str) -> bool:
    """Connect to a remote gateway and run the thin remote REPL.

    Returns ``True`` once the REPL completes normally; ``False`` if the
    initial WebSocket connect failed (caller decides whether to fall back
    to embedded mode or surface the error).

    Imports are lazy because the embedded path doesn't need the websockets
    library at runtime when no daemon is targeted.
    """
    from rich.console import Console

    from nano_openclaw.gateway.backend_websocket import WebSocketBackend
    from nano_openclaw.gateway.ws_repl import ws_repl

    console = Console()
    backend = WebSocketBackend(connect_url)
    try:
        await backend.aopen()
    except Exception as exc:  # noqa: BLE001 — surface connect failures clearly
        console.print(f"[red]connect failed:[/] {connect_url} — {type(exc).__name__}: {exc}")
        return False

    try:
        await ws_repl(backend, console=console)
    finally:
        await backend.aclose()
    return True


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
