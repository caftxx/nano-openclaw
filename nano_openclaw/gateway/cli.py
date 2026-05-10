"""``nano-openclaw gateway {status|start|stop|run}`` argparse handler.

The four subverbs:

- ``status`` (default if none given): one-line health report
- ``start``: spawn ``gateway run`` as a detached child, write pidfile, ping
  port until it answers (or 10s timeout), exit 0/1
- ``stop``: read pidfile, SIGTERM, poll port-closed for 5s, SIGKILL fallback,
  remove pidfile
- ``run``: foreground; ``asyncio.run(run_daemon(...))`` with signal handlers

All four respect ``--config`` (so the right state_dir / port resolves) and
``--agent``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from rich.console import Console

from nano_openclaw.config import resolve_state_dir
from nano_openclaw.config.io import load_config
from nano_openclaw.gateway.pidfile import (
    DaemonStatus,
    gateway_status,
    is_alive,
    pidfile_path,
    port_responds,
    read_pidfile,
    remove_pidfile,
)


# ────────────────────────────────────────────────────────────────────────────
# Argparse wiring
# ────────────────────────────────────────────────────────────────────────────


def add_gateway_subparser(subparsers) -> argparse.ArgumentParser:
    """Attach the ``gateway`` subparser to a top-level argparser.

    Returns the gateway parser so callers can inspect / extend it.
    """
    gateway_parser = subparsers.add_parser(
        "gateway",
        help="Manage the gateway daemon (status / start / stop / run)",
    )
    gateway_parser.add_argument(
        "verb",
        nargs="?",
        choices=("status", "start", "stop", "run"),
        default="status",
        help="status (default) | start (background) | stop | run (foreground)",
    )
    gateway_parser.add_argument("--host", default=None, help="Override config.gateway.host")
    gateway_parser.add_argument("--port", type=int, default=None, help="Override config.gateway.port")
    return gateway_parser


# ────────────────────────────────────────────────────────────────────────────
# Top-level dispatch — called from __main__.py
# ────────────────────────────────────────────────────────────────────────────


def run_gateway_cli(args: argparse.Namespace) -> int:
    """Return process exit code (0 success, non-zero failure)."""
    state_dir = resolve_state_dir()
    # Config path resolution falls through to ``$NANO_OPENCLAW_CONFIG_PATH``
    # / state_dir / cwd / home; the explicit --config CLI flag was removed.
    config, _ = load_config(None)
    gw_cfg = config.gateway
    host = args.host or gw_cfg.host
    port = args.port or gw_cfg.port

    verb = args.verb or "status"
    if verb == "status":
        return _verb_status(state_dir)
    if verb == "start":
        return _verb_start(state_dir, args=args, host=host, port=port)
    if verb == "stop":
        return _verb_stop(state_dir)
    if verb == "run":
        return _verb_run(args=args, host_override=args.host, port_override=args.port)
    raise ValueError(f"unknown gateway verb: {verb!r}")


# ────────────────────────────────────────────────────────────────────────────
# Verbs
# ────────────────────────────────────────────────────────────────────────────


def _verb_status(state_dir: Path) -> int:
    """Multi-line status output. Mirrors openclaw's structure:

    - Header line (running/stopped/stale + host:port + pid)
    - Process metadata: uptime, config path, log path
    - URLs: webui, /rpc
    - When running, RPC-probed runtime details: agent, model, workspace,
      channels list, session counts, in-flight turn count.

    Single-line back-compat: ``running on host:port (pid X)`` substring is
    preserved for tests/scripts that grep stdout.
    """
    status = gateway_status(state_dir)
    console = Console()

    if not status.running and not status.stale:
        console.print("[dim]gateway[/dim]: not running")
        config_path = _find_config_path()
        if config_path:
            console.print(f"  config:    {_short_home(config_path)}")
        return 1

    if status.stale:
        console.print(f"[yellow]gateway[/yellow]: {status.as_summary()}")
        return 1

    # ── Running — gather rich info ────────────────────────────────────────
    assert status.entry is not None
    entry = status.entry
    console.print(
        f"[green]gateway[/green]: running on [bold]{entry.host}:{entry.port}[/bold] (pid {entry.pid})"
    )

    # Process metadata: uptime (best-effort via pidfile mtime — close enough
    # to start time without bringing in psutil).
    uptime = _uptime_from_pidfile(state_dir)
    if uptime:
        console.print(f"  uptime:    {uptime}")

    config_path = _find_config_path()
    if config_path:
        console.print(f"  config:    {_short_home(config_path)}")
    log_path = _resolve_log_path(state_dir)
    console.print(f"  log:       {_short_home(log_path)}")

    # URLs the user can hit
    web_host = "localhost" if entry.host in ("0.0.0.0", "::") else entry.host
    console.print(f"  webui:     http://{web_host}:{entry.port}/")
    console.print(f"  rpc:       ws://{web_host}:{entry.port}/rpc")

    # ── RPC probe — runtime + health + channels ──────────────────────────
    probe = _probe_gateway_rpc(entry.host, entry.port, timeout=2.0)
    if probe is None:
        console.print()
        console.print("[yellow]rpc probe:[/yellow] timed out (gateway listening but not responding)")
        return 0

    runtime_info = probe.get("runtime") or {}
    health_info = probe.get("health") or {}
    channels = probe.get("channels") or []

    console.print()
    if runtime_info:
        console.print(f"  agent:     {runtime_info.get('agent_id', '?')}")
        console.print(f"  model:     {runtime_info.get('model_ref') or runtime_info.get('model_id', '?')}")
        thinking = runtime_info.get("thinking_level") or "off"
        console.print(f"  thinking:  {thinking}")
        ws_dir = runtime_info.get("workspace_dir") or ""
        if ws_dir:
            console.print(f"  workspace: {_short_home(Path(ws_dir))}")

    if channels:
        console.print()
        console.print("  channels:")
        for c in channels:
            cid = c.get("channel_id", "?")
            aid = c.get("account_id", "?")
            state = c.get("state", "?")
            color = "green" if state == "running" else "yellow"
            line = f"    [{color}]{cid}/{aid}[/{color}] · {state}"
            err = c.get("error")
            if err:
                line += f" · [red]{err}[/red]"
            console.print(line)
    else:
        console.print("  channels:  [dim](none)[/dim]")

    if health_info:
        console.print()
        sessions_loaded = health_info.get("sessions_loaded", 0)
        in_flight = health_info.get("in_flight_turns", 0)
        in_flight_color = "yellow" if in_flight > 0 else "dim"
        console.print(
            f"  sessions:  {sessions_loaded} loaded · "
            f"[{in_flight_color}]{in_flight} in flight[/{in_flight_color}]"
        )

    return 0


# ────────────────────────────────────────────────────────────────────────────
# Status helpers
# ────────────────────────────────────────────────────────────────────────────


def _short_home(path: Path) -> str:
    """Replace ``$HOME`` prefix with ``~`` for compact display."""
    try:
        home = Path.home()
        rel = path.resolve().relative_to(home.resolve())
        return f"~/{rel}"
    except (ValueError, OSError):
        return str(path)


def _find_config_path() -> Path | None:
    """Re-resolve which config file the daemon would read. Mirrors load_config."""
    try:
        from nano_openclaw.config.io import find_config_file
        path = find_config_file(None, dict(os.environ))
        return path
    except Exception:  # noqa: BLE001
        return None


def _uptime_from_pidfile(state_dir: Path) -> str | None:
    """Approximate uptime from pidfile mtime (the file is rewritten at start).

    Not perfectly accurate (pidfile re-write or clock skew), but plenty
    accurate enough for ``status`` output and avoids a psutil dependency.
    """
    from nano_openclaw.gateway.pidfile import pidfile_path
    try:
        mtime = pidfile_path(state_dir).stat().st_mtime
    except OSError:
        return None
    elapsed = max(0.0, time.time() - mtime)
    return _format_duration(elapsed)


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m {s:02d}s"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h}h {m:02d}m {s:02d}s"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d {h}h"


def _probe_gateway_rpc(host: str, port: int, *, timeout: float = 2.0) -> dict | None:
    """Synchronous wrapper that opens a one-shot WS connection to ``/rpc``
    and pulls health + runtime + channels.status.

    Returns ``None`` on any failure (timeout, refused, malformed) so the
    caller can degrade gracefully — status output should still be useful
    when RPC isn't responding.
    """
    import asyncio
    import json as _json

    async def _do() -> dict | None:
        import websockets
        # Connect with a short timeout — status should never hang.
        try:
            ws_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
            url = f"ws://{ws_host}:{port}/rpc"
            async with websockets.connect(url, open_timeout=timeout, close_timeout=timeout) as ws:
                async def _call(method: str) -> dict | None:
                    await ws.send(_json.dumps({"id": method, "method": method, "params": {}}))
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    obj = _json.loads(raw)
                    if obj.get("ok"):
                        return obj.get("payload") or {}
                    return None

                health = await _call("health") or {}
                runtime = await _call("runtime.get") or {}
                channels_payload = await _call("channels.status") or {}
                return {
                    "health": health,
                    "runtime": runtime,
                    "channels": channels_payload.get("channels") or [],
                }
        except Exception:  # noqa: BLE001 — graceful degrade for any failure
            return None

    try:
        return asyncio.run(_do())
    except Exception:  # noqa: BLE001
        return None


def _verb_start(
    state_dir: Path,
    *,
    args: argparse.Namespace,
    host: str,
    port: int,
) -> int:
    console = Console()
    status = gateway_status(state_dir)
    if status.running:
        console.print(f"[yellow]gateway[/yellow]: already {status.as_summary()}")
        return 1
    if status.stale:
        console.print(f"[dim]clearing stale pidfile ({status.as_summary()})[/dim]")
        remove_pidfile(state_dir)

    # Ensure port isn't already in use by something else
    if port_responds(host, port):
        console.print(
            f"[red]gateway start failed:[/red] port {host}:{port} already in use by another process"
        )
        return 1

    # Build the child command. Config + agent come from env / standard
    # search-path resolution rather than CLI flags.
    cmd = [sys.executable, "-m", "nano_openclaw", "gateway", "run"]
    if args.host:
        cmd += ["--host", args.host]
    if args.port:
        cmd += ["--port", str(args.port)]

    log_path = _resolve_log_path(state_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 — kept open for child stdout

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        log_fp.close()
        console.print(f"[red]gateway start failed:[/red] {type(exc).__name__}: {exc}")
        return 1

    # Don't keep the parent's fd around once the child has it.
    log_fp.close()

    # Poll port until it responds or we give up
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            console.print(
                f"[red]gateway start failed:[/red] child exited with code {proc.returncode} "
                f"before binding port — see {log_path}"
            )
            return 1
        if port_responds(host, port):
            console.print(
                f"[green]gateway started[/green] on [bold]{host}:{port}[/bold] (pid {proc.pid}); "
                f"logs at {log_path}"
            )
            return 0
        time.sleep(0.2)

    console.print(
        f"[red]gateway start timed out[/red]: child pid {proc.pid} didn't bind {host}:{port} "
        f"within 10s — see {log_path}"
    )
    return 1


def _verb_stop(state_dir: Path) -> int:
    console = Console()
    entry = read_pidfile(state_dir)
    if entry is None:
        console.print("[dim]gateway[/dim]: not running (no pidfile)")
        return 0  # idempotent — stopping a stopped daemon is success

    if not is_alive(entry.pid):
        console.print("[dim]gateway[/dim]: pidfile present but process already gone — clearing")
        remove_pidfile(state_dir)
        return 0

    # Send SIGTERM, then poll for shutdown
    try:
        os.kill(entry.pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pidfile(state_dir)
        return 0

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not is_alive(entry.pid) and not port_responds(entry.host, entry.port):
            console.print(f"[green]gateway stopped[/green] (pid {entry.pid})")
            remove_pidfile(state_dir)
            return 0
        time.sleep(0.2)

    # SIGTERM didn't take — escalate to SIGKILL
    console.print(f"[yellow]gateway didn't respond to SIGTERM in 5s — sending SIGKILL[/yellow]")
    try:
        os.kill(entry.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    # Brief follow-up wait, then declare done
    time.sleep(0.5)
    remove_pidfile(state_dir)
    console.print(f"[green]gateway killed[/green] (pid {entry.pid})")
    return 0


def _verb_run(
    *,
    args: argparse.Namespace,
    host_override: str | None,
    port_override: int | None,
) -> int:
    """Foreground run. ``run_daemon`` returns the exit code."""
    from nano_openclaw.gateway.server import run_daemon

    return asyncio.run(
        run_daemon(
            config_path=None,
            agent_id="default",
            host_override=host_override,
            port_override=port_override,
        )
    )


def _resolve_log_path(state_dir: Path) -> Path:
    """The detached daemon redirects stdout/stderr here.

    Co-located with ``state_dir/log/nano-openclaw.log`` (the structured
    logger output) so all logs land in one directory. Future:
    ``config.gateway.log_path`` overrides this.
    """
    return state_dir / "log" / "gateway.log"
