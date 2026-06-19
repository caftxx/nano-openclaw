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
open ``http://127.0.0.1:5000``. To run wechat, scan-login first
(``nano-openclaw wechat login [--account=ID]``) and then start the gateway —
the daemon auto-discovers any persisted login under
``state_dir/wechat-tokens.{id}.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from nano_openclaw.bootstrap import ensure_state_dir_initialized
from nano_openclaw.adapters.cli.repl import repl
from nano_openclaw.config import resolve_state_dir_with_source
from nano_openclaw.daemon.cli import add_gateway_subparser, run_gateway_cli
from nano_openclaw.logger import setup_logging
from nano_openclaw.services.runtime_factory import build_agent_runtime
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


def _resolve_version() -> str:
    """Read the installed package version from metadata (single source of
    truth in pyproject.toml). Falls back to ``unknown`` when running from an
    uninstalled checkout without metadata."""
    from nano_openclaw import resolve_version

    return resolve_version()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="nano-openclaw",
        description="Minimal educational reimplementation of OpenClaw's agent loop.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {_resolve_version()}",
        help="Print the version number and exit",
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
        help="Account id (free-form label; default: 'default'). Token will persist as state_dir/wechat-tokens.{id}.json",
    )

    args = parser.parse_args()

    state_dir, state_source = resolve_state_dir_with_source()
    if ensure_state_dir_initialized(state_dir, source=state_source):
        print(
            f"[bootstrap] initialized {state_dir} from template — "
            f"edit nano-openclaw.json5 to add your API key",
            file=sys.stderr,
        )
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
            sys.exit(asyncio.run(run_wechat_login(account_id=args.account)))
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
            from nano_openclaw.daemon.pidfile import gateway_status as _gw_status
            status = _gw_status(state_dir)
            if status.running and status.entry is not None:
                connect_url = _daemon_connect_url(status.entry)

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
        # path is exercised end-to-end.
        asyncio.run(
            _async_main(
                config=config_path,
                agent=agent_id,
                resume=resume_flag,
                session_dir=session_dir,
                store_path=store_path,
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
    from nano_openclaw.services.runs import RunRegistry
    from nano_openclaw.services.runtime_update import RuntimeUpdateGuard

    runtime = await build_agent_runtime(
        config_path=config,
        agent_id=agent,
        session_id=session_id or new_session_id(),  # runtime needs a key for cron / cfg.session_key
        console=console,
        run_registry=RunRegistry(),
        runtime_guard=RuntimeUpdateGuard(),
    )

    for var_name, cfg_path in runtime.warnings:
        print(
            f"warning: missing env var \"{var_name}\" at {cfg_path} - "
            f"feature using this value will be unavailable",
            file=sys.stderr,
        )

    # BackendSessionManager owns transcript_writer + history. Don't pre-create
    # either here; repl() asks the manager for the session entity.
    from nano_openclaw.services.backend_embedded import EmbeddedBackend
    backend = EmbeddedBackend(runtime)

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
        await backend.aclose()
        await runtime.close()


def _daemon_connect_url(entry: "PidfileEntry") -> str:
    """Build the ``/rpc`` dial URL for an auto-detected local daemon.

    Two pidfile facts the naive ``ws://{host}:{port}`` form gets wrong:

    - A wildcard bind host (``0.0.0.0`` / ``::``) isn't dialable, so loop it
      back to ``127.0.0.1``.
    - An https daemon only speaks ``wss``; honour the recorded scheme so the
      TUI doesn't hit a plaintext handshake against a TLS socket.
    """
    ws_host = "127.0.0.1" if entry.host in ("0.0.0.0", "::") else entry.host
    ws_scheme = "wss" if entry.scheme == "https" else "ws"
    return f"{ws_scheme}://{ws_host}:{entry.port}/rpc"


async def _run_ws_tui(connect_url: str) -> bool:
    """Connect to a remote gateway and run the thin remote REPL.

    Returns ``True`` once the REPL completes normally; ``False`` if the
    initial WebSocket connect failed (caller decides whether to fall back
    to embedded mode or surface the error).

    Imports are lazy because the embedded path doesn't need the websockets
    library at runtime when no daemon is targeted.
    """
    from rich.console import Console

    from nano_openclaw.api.backend_websocket import WebSocketBackend
    from nano_openclaw.adapters.cli.ws_repl import ws_repl

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
