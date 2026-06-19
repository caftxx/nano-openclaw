"""Agent runtime state shared by services and adapters."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from nano_openclaw.logger import get_logger
from nano_openclaw.core.loop import LoopConfig

log = get_logger(__name__)
from nano_openclaw.core.tools import ToolRegistry


@dataclass
class AgentRuntime:
    agent_id: str
    session_id: str
    config: Any
    warnings: list[tuple[str, str]]
    client: Any
    registry: ToolRegistry
    cfg: LoopConfig
    hook_registry: Any
    state_dir: Path
    session_dir: Path
    store_path: Path
    workspace_dir: Path
    model_ref: str
    model_id: str
    image_model_ref: str | None
    dreaming_stop: threading.Event
    # ``run_registry`` is the single source of truth for in-flight turn_ids
    # across chat, cron, channels. The services layer injects it so core does
    # not depend on service implementations.
    run_registry: Any
    # ``runtime_guard`` coordinates ``runtime.update`` against in-flight turns
    # and has the same lifetime as ``run_registry``.
    runtime_guard: Any
    # The config path used to build this runtime — needed by Backend's
    # ``runtime_update`` so a hot-reload can re-invoke ``build_agent_runtime``
    # with the same source. ``None`` means "use default discovery".
    config_path: str | None = None
    dreaming_task: Any | None = None
    cron_stop: threading.Event | None = None
    cron_task: Any | None = None
    # Process restart is supplied by the outer daemon layer. Core can schedule
    # the intent, but it must not import daemon process-control modules.
    restart_callback: Callable[[str], Any] | None = None
    # Flipped to True by the ``restart`` tool. The restart watcher waits for
    # ``run_registry`` to drain, then invokes ``restart_callback``. The slash
    # ``/restart`` path bypasses this with an immediate backend call.
    pending_restart: bool = False

    async def close(self) -> None:
        await self.hook_registry.run("session_end", {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "workspace_dir": str(self.workspace_dir),
        })
        self.dreaming_stop.set()
        if self.dreaming_task and not self.dreaming_task.done():
            self.dreaming_task.cancel()
            try:
                await self.dreaming_task
            except BaseException as e:
                log.debug("runtime.close.dreaming", f"Dreaming task cancelled: {type(e).__name__}")
                pass
        if self.cron_stop is not None:
            self.cron_stop.set()
        if self.cron_task and not self.cron_task.done():
            self.cron_task.cancel()
            try:
                await self.cron_task
            except BaseException as e:
                log.debug("runtime.close.cron", f"Cron task cancelled: {type(e).__name__}")
                pass
        if hasattr(self.client, "aclose"):
            await self.client.aclose()
        elif hasattr(self.client, "close"):
            await self.client.close()

def image_model_id_from_ref(image_model_ref: str | None) -> str | None:
    if not image_model_ref:
        return None
    return image_model_ref.split("/", 1)[1] if "/" in image_model_ref else image_model_ref
