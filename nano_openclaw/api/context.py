"""``GatewayContext`` — the service hub passed to every RPC method.

Holds the daemon-process services every method handler reads/writes:

- ``backend``: the BackendService implementation. RPC handlers delegate to it
  so embedded TUI and remote TUI share one code path.
- ``channel_manager``: lets ``channels.*`` RPC methods inspect/mutate
  channels without re-importing.
- ``state_dir``: convenience for handlers that need disk paths
  (e.g., ``health``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nano_openclaw.services.channels import ChannelManager
    from nano_openclaw.services.backend_embedded import EmbeddedBackend


@dataclass
class GatewayContext:
    """Per-daemon shared state. One instance per ``run_daemon`` invocation."""

    backend: "EmbeddedBackend"
    channel_manager: "ChannelManager"
    state_dir: Path
