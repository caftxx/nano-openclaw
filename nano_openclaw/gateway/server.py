"""Gateway daemon entry — single process owning the AgentRuntime.

``run_daemon`` is the foreground async main: build runtime, mount the
WebUI ASGI app on uvicorn, start every configured channel as an asyncio
task, and serve until SIGTERM/SIGINT. ``gateway start`` (in cli.py) spawns
this same coroutine via ``subprocess.Popen([... "gateway", "run"], ...)``.

Channel hosting (Phase 3 v1):

- Iterate ``runtime.config.wechat.accounts`` and start a ``WechatChannel``
  per account through the global ``ChannelRegistry``. Other channels (when
  added later) follow the same pattern: import for side-effect registration,
  iterate config, ``await registry.start(...)``.
- Channels share the daemon's single ``AgentRuntime`` instance — that's
  what makes cron / dreaming / subagent runner singletons safe.

Phase 4 (later) will add a ``/rpc`` WebSocket route to the same FastAPI app
for remote TUI clients.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from nano_openclaw.channels.registry import get_channel_registry
from nano_openclaw.gateway.backend_embedded import EmbeddedBackend
from nano_openclaw.gateway.context import GatewayContext
from nano_openclaw.gateway.pidfile import remove_pidfile, write_pidfile
from nano_openclaw.gateway.ws_route import register_ws_route
from nano_openclaw.logger import get_logger
from nano_openclaw.runtime import build_agent_runtime

# Side-effect import: registers WechatChannel in the global ChannelRegistry
# so the daemon can spawn it from config. Add similar lines as future
# channels (telegram, slack, ...) come online.
import nano_openclaw.channels.wechat  # noqa: F401

if TYPE_CHECKING:
    from nano_openclaw.channels.base import ChannelAccount
    from nano_openclaw.runtime import AgentRuntime


log = get_logger(__name__)


async def run_daemon(
    *,
    config_path: str | None = None,
    agent_id: str = "default",
    host_override: str | None = None,
    port_override: int | None = None,
    write_pid: bool = True,
) -> int:
    """Foreground daemon. Returns process exit code.

    ``host_override`` / ``port_override`` shadow ``config.gateway.host/port``;
    used by the CLI when the user passes ``--host`` / ``--port``.

    ``write_pid`` lets the legacy / test paths skip pidfile management when
    they want to embed run_daemon for some other purpose.
    """
    import uvicorn  # local import to keep cold-start cheap for non-daemon paths

    from nano_openclaw.webui.server import create_app

    console = Console()

    # ── Build the runtime — single instance shared by webui + channels ───────
    runtime = await build_agent_runtime(config_path=config_path, agent_id=agent_id)

    for var_name, cfg_path in runtime.warnings:
        console.print(f"[yellow]warning:[/yellow] missing env var \"{var_name}\" at {cfg_path}")

    gw_cfg = runtime.config.gateway
    host = host_override or gw_cfg.host
    port = port_override or gw_cfg.port

    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            f"[yellow]warning:[/yellow] binding gateway on non-loopback {host}:{port} with no auth — "
            f"anyone reachable on the network can drive this agent. Restrict the bind host or "
            f"add a reverse proxy."
        )

    # ── PID file ─────────────────────────────────────────────────────────────
    if write_pid:
        write_pidfile(runtime.state_dir, pid=os.getpid(), port=port, host=host)

    # ── Backend + GatewayContext FIRST so channels can share its manager ───
    # Order matters: WeChat (and any future channel) needs ``backend.manager``
    # at start time so per-uid sessions register through the same store the
    # WebUI + /rpc see — single source of truth for sessions across surfaces.
    channel_registry = get_channel_registry()
    started_channels: list[tuple[str, str]] = []
    backend: EmbeddedBackend = EmbeddedBackend(runtime)
    gateway_ctx = GatewayContext(
        runtime=runtime,
        backend=backend,
        channel_registry=channel_registry,
    )

    try:
        # Channels see ``gateway_ctx`` so they can use backend.manager.
        await _start_configured_channels(runtime, channel_registry, started_channels, console, gateway_ctx)

        # ── WebUI ASGI app + uvicorn ────────────────────────────────────────
        # Pass ``backend`` so WebUI shares the daemon's BackendSessionManager
        # rather than building its own — that's the only way WebUI and the
        # /rpc WebSocket see the same session list (including any active
        # in-memory sessions that haven't yet hit disk).
        app = create_app(
            config_path=config_path,
            agent_id=agent_id,
            token=None,           # Phase 3: no auth (per user decision)
            runtime=runtime,
            backend=backend,
        )

        # Mount the JSON-RPC WebSocket on the same FastAPI app — same port,
        # same lifespan as the WebUI, single ``Backend`` instance.
        register_ws_route(app, gateway_ctx)

        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="warning",  # we already have our own structured logger
            access_log=False,
            lifespan="on",
            ws="websockets-sansio",
        )
        server = uvicorn.Server(config)

        # Hook ctrl-c / SIGTERM to shut down uvicorn cleanly
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_uvicorn_exit, server)
            except NotImplementedError:
                # Windows / non-Unix
                pass

        console.print(f"[green]gateway[/green] running on [bold]{host}:{port}[/bold] (pid {os.getpid()})")
        if started_channels:
            for cid, aid in started_channels:
                console.print(f"  ├─ channel [cyan]{cid}[/cyan]/[cyan]{aid}[/cyan]")

        await server.serve()
        return 0

    except Exception as exc:
        log.error("gateway.run.error", f"{type(exc).__name__}: {exc}")
        console.print(f"[red]gateway error:[/red] {type(exc).__name__}: {exc}")
        return 1
    finally:
        # ── Channels stop ────────────────────────────────────────────────────
        try:
            await channel_registry.stop_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("gateway.shutdown.channels", f"{type(exc).__name__}: {exc}")

        # ── Backend close (drains subscribers, cancels in-flight turns) ─────
        try:
            await backend.aclose()
        except Exception as exc:  # noqa: BLE001
            log.warning("gateway.shutdown.backend", f"{type(exc).__name__}: {exc}")

        # ── Runtime close ────────────────────────────────────────────────────
        try:
            await runtime.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("gateway.shutdown.runtime", f"{type(exc).__name__}: {exc}")

        # ── PID file ─────────────────────────────────────────────────────────
        if write_pid:
            try:
                remove_pidfile(runtime.state_dir)
            except Exception as exc:  # noqa: BLE001
                log.warning("gateway.shutdown.pidfile", f"{type(exc).__name__}: {exc}")


def _request_uvicorn_exit(server: "uvicorn.Server") -> None:  # type: ignore[name-defined]
    """SIGINT/SIGTERM handler: tell uvicorn to drain + exit gracefully."""
    server.should_exit = True


async def _start_configured_channels(
    runtime: "AgentRuntime",
    registry,
    started_channels: list[tuple[str, str]],
    console: Console,
    gateway: GatewayContext,
) -> None:
    """Spawn one ``Channel`` instance per ``ChannelAccount`` in config.

    Skips accounts that fail to start (e.g., missing token); the rest of the
    daemon comes up so the WebUI is still usable. The misconfigured account's
    error is surfaced to the console.

    ``gateway`` is forwarded into each channel so per-key sessions register
    through ``backend.manager`` — that's how wechat sessions show up
    alongside webui/tui sessions in the unified ``/sessions`` list.
    """
    from nano_openclaw.channels.base import ChannelAccount
    from nano_openclaw.config.types import WechatAccountConfig
    from nano_openclaw.wechat.login_cli import discover_persisted_account_ids, load_persisted_token

    wechat_cfg = runtime.config.wechat

    # Build the effective account list:
    #   1. Anything explicitly listed under wechat.accounts in config.
    #   2. Anything discovered as state_dir/wechat-tokens.{id}.json that
    #      isn't already in (1) — lets `wechat login` work without anyone
    #      having to also remember to add the account to config.
    accounts_by_id: dict[str, WechatAccountConfig] = {a.id: a for a in wechat_cfg.accounts}
    for discovered_id in discover_persisted_account_ids(runtime.state_dir):
        if discovered_id not in accounts_by_id:
            accounts_by_id[discovered_id] = WechatAccountConfig(id=discovered_id)
            log.info(
                "gateway.channel.discovered",
                f"wechat/{discovered_id}: persisted login found, account auto-registered",
            )

    for account_cfg in accounts_by_id.values():
        # A persisted token from `wechat login` counts the same as a configured
        # one — only skip when *both* sources are empty.
        if not account_cfg.ilink_token:
            persisted, _ = load_persisted_token(runtime.state_dir, account_cfg.id)
            if not persisted:
                log.info(
                    "gateway.channel.skip.no_token",
                    f"wechat/{account_cfg.id}: no ilink_token configured and no persisted "
                    f"login found, skipping (run `nano-openclaw wechat login --account={account_cfg.id}`)",
                )
                continue
        account = ChannelAccount(
            id=account_cfg.id,
            config={
                "ilink_token": account_cfg.ilink_token,
                "ilink_base_url": account_cfg.ilink_base_url,
                "notify_queue_path": account_cfg.notify_queue_path,
            },
        )
        try:
            await registry.start("wechat", account, runtime, gateway)
            started_channels.append(("wechat", account.id))
        except Exception as exc:  # noqa: BLE001 — one bad account shouldn't kill the daemon
            log.error(
                "gateway.channel.start.error",
                f"wechat/{account.id}: {type(exc).__name__}: {exc}",
            )
            console.print(
                f"[red]channel start failed:[/red] wechat/{account.id}: {type(exc).__name__}: {exc}"
            )
