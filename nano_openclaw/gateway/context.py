"""``GatewayContext`` — the single mutable hub passed to every RPC method.

Holds the daemon-process state every method handler reads/writes:

- ``runtime``: the one ``AgentRuntime`` shared by webui + channels + RPC.
- ``backend``: the ``EmbeddedBackend`` wrapping ``runtime``. RPC handlers
  delegate to it so embedded TUI and remote TUI share one code path.
- ``channel_registry``: lets ``channels.*`` RPC methods inspect/mutate
  channels without re-importing.
- ``state_dir``: convenience for handlers that need disk paths
  (e.g., ``health``).

Phase 7 will extend this with ``runtime_lock`` (writer/reader RWLock for
``runtime.update``) and an ``in_flight_turns`` set for the BUSY check.
v1 keeps the surface minimal so the WS dispatch is easy to read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nano_openclaw.channels.registry import ChannelRegistry
    from nano_openclaw.services.backend_embedded import EmbeddedBackend
    from nano_openclaw.core.runtime import AgentRuntime


@dataclass
class GatewayContext:
    """Per-daemon shared state. One instance per ``run_daemon`` invocation."""

    runtime: "AgentRuntime"
    backend: "EmbeddedBackend"
    channel_registry: "ChannelRegistry"

    @property
    def state_dir(self) -> Path:
        return self.runtime.state_dir
