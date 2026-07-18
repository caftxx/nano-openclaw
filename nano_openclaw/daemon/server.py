"""Daemon entry point: one process owning the runtime and frontends.

``run_daemon`` is the foreground async main: build runtime, mount the
WebUI ASGI app on uvicorn, start every configured channel as an asyncio
task, and serve until SIGTERM/SIGINT. ``gateway start`` (in cli.py) spawns
this same coroutine via ``subprocess.Popen([... "gateway", "run"], ...)``.

Channel hosting (Phase 3 v1):

- Discover wechat accounts from ``state_dir/wechat-tokens.{id}.json`` files
  (written by ``nano-openclaw wechat login``) and start a ``WechatChannel``
  per account through the global ``ChannelManager``. Other channels (when
  added later) follow the same pattern: import for side-effect registration,
  enumerate accounts, ``await registry.start(...)``.
- Channels share the daemon's single runtime service instance — that's
  what makes cron / dreaming / subagent runner singletons safe.

The daemon mounts the WebUI plus the ``/rpc`` WebSocket API used by remote
frontends.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console

from nano_openclaw.services.channels import get_channel_manager
from nano_openclaw.services.backend_embedded import EmbeddedBackend
from nano_openclaw.api.context import GatewayContext
from nano_openclaw.daemon.pidfile import lan_ip, remove_pidfile, write_pidfile
from nano_openclaw.api.ws_route import register_ws_route
from nano_openclaw.daemon.restart import perform_restart
from nano_openclaw.logger import get_logger
from nano_openclaw.services.runtime_factory import build_agent_runtime

# Side-effect import: registers WechatChannel in the global ChannelManager
# so the daemon can spawn it from config. Add similar lines as future
# channels (telegram, slack, ...) come online.
import nano_openclaw.adapters.channels.wechat  # noqa: F401
import nano_openclaw.adapters.xiaozhi.channel  # noqa: F401

if TYPE_CHECKING:
    from nano_openclaw.services.channels import ChannelAccount


log = get_logger(__name__)


async def run_daemon(
    *,
    config_path: str | None = None,
    agent_id: str = "default",
    host_override: str | None = None,
    port_override: int | None = None,
    tls_cert_override: str | None = None,
    tls_key_override: str | None = None,
    write_pid: bool = True,
) -> int:
    """Foreground daemon. Returns process exit code.

    ``host_override`` / ``port_override`` shadow ``config.gateway.host/port``;
    used by the CLI when the user passes ``--host`` / ``--port``.

    ``tls_cert_override`` / ``tls_key_override`` shadow ``config.gateway.tls_cert/
    tls_key``; when both resolve to a path, uvicorn serves HTTPS/WSS instead of
    plain HTTP — needed so phones on a LAN IP get a secure context for the mic.

    ``write_pid`` lets the legacy / test paths skip pidfile management when
    they want to embed run_daemon for some other purpose.
    """
    import uvicorn  # local import to keep cold-start cheap for non-daemon paths

    from nano_openclaw.adapters.webui.server import create_app
    from nano_openclaw.services.runs import RunRegistry
    from nano_openclaw.services.runtime_update import RuntimeUpdateGuard

    console = Console()

    # ── Build the runtime — single instance shared by webui + channels ───────
    runtime = await build_agent_runtime(
        config_path=config_path,
        agent_id=agent_id,
        run_registry=RunRegistry(),
        runtime_guard=RuntimeUpdateGuard(),
        restart_callback=perform_restart,
    )

    for var_name, cfg_path in runtime.warnings:
        console.print(f"[yellow]warning:[/yellow] missing env var \"{var_name}\" at {cfg_path}")

    gw_cfg = runtime.config.gateway
    host = host_override or gw_cfg.host
    port = port_override or gw_cfg.port

    # ── TLS resolution ───────────────────────────────────────────────────────
    # Both cert and key must be present to enable HTTPS; a half-configured pair
    # is a user mistake we surface loudly rather than silently fall back to HTTP.
    tls_cert = (tls_cert_override or gw_cfg.tls_cert or "").strip()
    tls_key = (tls_key_override or gw_cfg.tls_key or "").strip()
    ssl_kwargs: dict[str, str] = {}
    if tls_cert or tls_key:
        if not (tls_cert and tls_key):
            console.print(
                "[red]gateway error:[/red] TLS needs both a cert and a key; "
                f"got cert={tls_cert or '(unset)'} key={tls_key or '(unset)'}"
            )
            return 1
        for label, path in (("tls_cert", tls_cert), ("tls_key", tls_key)):
            if not Path(path).is_file():
                console.print(f"[red]gateway error:[/red] {label} file not found: {path}")
                return 1
        ssl_kwargs = {"ssl_certfile": tls_cert, "ssl_keyfile": tls_key}
    scheme = "https" if ssl_kwargs else "http"

    if host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            f"[yellow]warning:[/yellow] binding gateway on non-loopback {host}:{port} with no auth — "
            f"anyone reachable on the network can drive this agent. Restrict the bind host or "
            f"add a reverse proxy."
        )

    # ── PID file ─────────────────────────────────────────────────────────────
    if write_pid:
        write_pidfile(runtime.state_dir, pid=os.getpid(), port=port, host=host, scheme=scheme)

    # ── Backend + GatewayContext FIRST so channels can share its manager ───
    # Order matters: WeChat (and any future channel) needs ``backend.manager``
    # at start time so per-uid sessions register through the same store the
    # WebUI + /rpc see — single source of truth for sessions across surfaces.
    channel_manager = get_channel_manager()
    started_channels: list[tuple[str, str]] = []
    backend: EmbeddedBackend = EmbeddedBackend(runtime, channel_manager=channel_manager)
    gateway_ctx = GatewayContext(
        backend=backend,
        channel_manager=channel_manager,
        state_dir=runtime.state_dir,
    )

    try:
        # Channels see ``gateway_ctx`` so they can use backend.manager.
        await _start_configured_channels(runtime, channel_manager, started_channels, console, gateway_ctx)

        # ── WebUI ASGI app + uvicorn ────────────────────────────────────────
        # Pass ``backend`` so WebUI shares the daemon's BackendSessionManager
        # rather than building its own — that's the only way WebUI and the
        # /rpc WebSocket see the same session list (including any active
        # in-memory sessions that haven't yet hit disk).
        app = create_app(
            token=None,           # Phase 3: no auth (per user decision)
            backend=backend,
        )

        # Mount the JSON-RPC WebSocket on the same FastAPI app — same port,
        # same lifespan as the WebUI, single ``Backend`` instance.
        register_ws_route(app, gateway_ctx)
        from nano_openclaw.adapters.xiaozhi.routes import register_xiaozhi_routes
        register_xiaozhi_routes(app, gateway_ctx)

        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="warning",  # we already have our own structured logger
            access_log=False,
            lifespan="on",
            ws="websockets-sansio",
            **ssl_kwargs,
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

        # On a wildcard bind advertise the default-route LAN IP so the printed
        # URL is reachable from another device (phone hitting the WebUI mic over
        # the LAN); fall back to localhost when no route is resolvable.
        if host in ("0.0.0.0", "::"):
            web_host = lan_ip() or "localhost"
        else:
            web_host = host
        console.print(
            f"[green]gateway[/green] running on [bold]{scheme}://{host}:{port}[/bold] (pid {os.getpid()})"
        )
        console.print(f"  webui:  {scheme}://{web_host}:{port}/   voice: {scheme}://{web_host}:{port}/voice")
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
            await channel_manager.stop_all()
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
    runtime: Any,
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
    from nano_openclaw.services.channels import ChannelAccount
    from nano_openclaw.wechat.login_cli import discover_persisted_account_ids

    if runtime.config.xiaozhi.enabled:
        account = ChannelAccount(id="default", config={})
        try:
            instance = await registry.start("xiaozhi", account, runtime, gateway)
            status = instance.status()
            if status.state == "running":
                started_channels.append(("xiaozhi", "default"))
            else:
                message = status.error or "unknown initialization error"
                log.error("gateway.channel.start.error", f"xiaozhi/default: {message}")
                console.print(f"[red]channel error:[/red] xiaozhi/default: {message}")
        except Exception as exc:  # noqa: BLE001
            log.error("gateway.channel.start.error", f"xiaozhi/default: {type(exc).__name__}: {exc}")
            console.print(
                f"[red]channel start failed:[/red] xiaozhi/default: {type(exc).__name__}: {exc}"
            )

    # Wechat accounts are discovered purely from persisted login tokens —
    # there's no config-file accounts list any more. Run
    # ``nano-openclaw wechat login --account=ID`` for each account you want
    # the daemon to host; that writes ``state_dir/wechat-tokens.{id}.json``
    # which we pick up here.
    discovered = discover_persisted_account_ids(runtime.state_dir)
    if not discovered:
        log.info(
            "gateway.channel.no_wechat_logins",
            "no wechat-tokens.*.json files in state_dir — "
            "run `nano-openclaw wechat login` to add a wechat account",
        )
        return

    for account_id in discovered:
        # WechatChannel.start() reads the persisted token + base_url from
        # state_dir directly, so an empty config dict is fine here.
        account = ChannelAccount(id=account_id, config={})
        try:
            await registry.start("wechat", account, runtime, gateway)
            started_channels.append(("wechat", account_id))
        except Exception as exc:  # noqa: BLE001 — one bad account shouldn't kill the daemon
            log.error(
                "gateway.channel.start.error",
                f"wechat/{account_id}: {type(exc).__name__}: {exc}",
            )
            console.print(
                f"[red]channel start failed:[/red] wechat/{account_id}: {type(exc).__name__}: {exc}"
            )
