"""Daemon self-restart primitive.

Two strategies, picked via ``GatewayConfig.restart_strategy``:

- ``"exec"`` (default): ``os.execv`` the same Python entry with the same
  ``sys.argv``. PID stays the same — systemd ``Type=simple``, supervisord,
  Docker and standalone ``gateway start`` all see the process keep running
  through the swap. ``Type=notify`` / ``WatchdogSec=`` units need to
  re-arm sd_notify after exec; the default unit shape doesn't, so this
  works with zero unit-file changes.
- ``"exit"``: ``os._exit(0)`` and rely on the supervisor (systemd
  ``Restart=always``, docker ``restart: unless-stopped``, ...) to bring
  the daemon back. Cleaner separation but **requires a supervisor** —
  the standalone ``gateway start`` detached path has none, so picking
  ``exit`` there leaves the service dead.

Both strategies flush stdout/stderr first so any "restarting…" line the
caller printed makes it to the user before the swap. The grace delay is
the caller's responsibility (so push frames over /rpc finish flushing).
"""

from __future__ import annotations

import os
import sys
from typing import Literal, NoReturn

from nano_openclaw.logger import get_logger

log = get_logger(__name__)


RestartStrategy = Literal["exec", "exit"]


def perform_restart(strategy: RestartStrategy = "exec") -> NoReturn:
    """Replace or terminate the current process. Never returns."""
    log.info("gateway.restart", f"strategy={strategy} pid={os.getpid()}")

    # Flush so the last log line + any console writes hit the journal/file
    # before we're swapped. After execv/exit there's no chance to drain.
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass

    if strategy == "exit":
        # Hard exit (skip atexit / asyncio shutdown). The supervisor restarts.
        # We use _exit not sys.exit because we don't want an asyncio cleanup
        # storm to mask the restart intent.
        os._exit(0)

    # Default: exec. Same PID, same FDs, same env. sys.argv reflects what
    # was originally invoked, including ``gateway run`` or ``gateway start``
    # subcommand and any --host/--port flags.
    os.execv(sys.executable, [sys.executable, *sys.argv])
