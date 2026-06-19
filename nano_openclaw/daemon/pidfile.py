"""PID file management for the gateway daemon.

Tracks ``state_dir/gateway.pid`` and answers "is a daemon running?"
across two probes:

1. **PID liveness** via ``os.kill(pid, 0)`` — cheap, but doesn't tell us
   whether the listener is healthy.
2. **TCP port reachability** — confirms the gateway accepted a connection.

Stale-detection: if either probe fails, the daemon is treated as not
running (gateway start will overwrite the pidfile rather than refuse).
"""

from __future__ import annotations

import errno
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


PIDFILE_NAME = "gateway.pid"


@dataclass(frozen=True)
class PidfileEntry:
    pid: int
    port: int
    host: str
    scheme: str = "http"

    @property
    def line(self) -> str:
        return f"{self.pid} {self.port} {self.host} {self.scheme}\n"


def pidfile_path(state_dir: Path) -> Path:
    return state_dir / PIDFILE_NAME


def write_pidfile(
    state_dir: Path,
    *,
    pid: int,
    port: int,
    host: str = "127.0.0.1",
    scheme: str = "http",
) -> None:
    """Atomically (re-)write the gateway PID file."""
    entry = PidfileEntry(pid=pid, port=port, host=host, scheme=scheme)
    state_dir.mkdir(parents=True, exist_ok=True)
    target = pidfile_path(state_dir)
    tmp = target.with_suffix(".pid.tmp")
    tmp.write_text(entry.line, encoding="utf-8")
    os.replace(tmp, target)


def read_pidfile(state_dir: Path) -> Optional[PidfileEntry]:
    """Return the parsed entry, or None if no pidfile or unreadable/garbled."""
    path = pidfile_path(state_dir)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    parts = raw.split()
    if len(parts) < 2:
        return None
    try:
        pid = int(parts[0])
        port = int(parts[1])
    except ValueError:
        return None
    host = parts[2] if len(parts) >= 3 else "127.0.0.1"
    scheme = parts[3] if len(parts) >= 4 else "http"
    return PidfileEntry(pid=pid, port=port, host=host, scheme=scheme)


def remove_pidfile(state_dir: Path) -> None:
    """Idempotent — silent if file is gone."""
    path = pidfile_path(state_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def is_alive(pid: int) -> bool:
    """True if a process with *pid* exists and is still running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _is_alive_win32(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def _is_alive_win32(pid: int) -> bool:
    """Windows-specific liveness check via OpenProcess + GetExitCodeProcess.

    os.kill(pid, 0) is unreliable on Windows: it may succeed for PIDs that
    don't exist, raise SystemError (CPython bug), or raise OSError with
    inconsistent winerror codes across machines.
    """
    import ctypes
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False

    exit_code = ctypes.c_ulong()
    got_code = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    kernel32.CloseHandle(handle)
    if not got_code:
        return False
    return exit_code.value == STILL_ACTIVE


def lan_ip() -> Optional[str]:
    """Best-effort local IP of the interface carrying the default route.

    Opens a UDP socket "toward" a public address and reads back the local
    endpoint the OS would route through — no packet is actually sent, so this
    works offline and cross-platform. Returns ``None`` (callers fall back to
    ``localhost``) if no route can be determined or the address is loopback.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if not ip or ip.startswith("127."):
        return None
    return ip


def port_responds(host: str, port: int, *, timeout: float = 0.2) -> bool:
    """True if a TCP connect to host:port succeeds quickly."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


@dataclass(frozen=True)
class DaemonStatus:
    """Combined view of pidfile + liveness + port reachability."""
    running: bool
    entry: Optional[PidfileEntry]
    pid_alive: bool
    port_open: bool
    stale: bool

    def as_summary(self) -> str:
        if not self.entry:
            return "not running"
        host_port = f"{self.entry.host}:{self.entry.port}"
        if self.running:
            return f"running on {host_port} (pid {self.entry.pid})"
        if self.stale:
            reasons = []
            if not self.pid_alive:
                reasons.append("pid dead")
            if not self.port_open:
                reasons.append("port closed")
            return f"stale pidfile ({host_port}, pid {self.entry.pid}, {', '.join(reasons)})"
        return "not running"


def gateway_status(state_dir: Path) -> DaemonStatus:
    """Probe the daemon and return a complete status snapshot."""
    entry = read_pidfile(state_dir)
    if entry is None:
        return DaemonStatus(running=False, entry=None, pid_alive=False, port_open=False, stale=False)
    pid_alive = is_alive(entry.pid)
    port_open = port_responds(entry.host, entry.port)
    running = pid_alive and port_open
    stale = not running
    return DaemonStatus(
        running=running,
        entry=entry,
        pid_alive=pid_alive,
        port_open=port_open,
        stale=stale,
    )
