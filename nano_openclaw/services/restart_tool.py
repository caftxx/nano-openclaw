"""Runtime restart tool registration."""

from __future__ import annotations

import asyncio
from typing import Any

from nano_openclaw.core.runtime import AgentRuntime
from nano_openclaw.core.tools import Tool
from nano_openclaw.logger import get_logger

log = get_logger(__name__)


def register_restart_tool(runtime: AgentRuntime) -> None:
    """Wire the LLM-facing ``restart`` tool into the runtime's ToolRegistry."""

    async def _wait_and_restart(rt: AgentRuntime, strategy: str) -> None:
        while len(rt.run_registry) > 0:
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.2)
        if rt.restart_callback is None:
            log.warning("runtime.restart.unavailable", "restart requested without daemon callback")
            return
        rt.restart_callback(strategy)

    state: dict[str, Any] = {"watcher_started": False}

    def _restart_tool(_args: dict[str, Any]) -> str:
        runtime.pending_restart = True
        if state["watcher_started"]:
            return "restart already pending — will fire after current turn(s) complete"

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return "restart cannot be scheduled: no running event loop"

        strategy = runtime.config.gateway.restart_strategy
        loop.create_task(_wait_and_restart(runtime, strategy))
        state["watcher_started"] = True
        return f"restart scheduled (strategy={strategy}); will fire once the registry drains"

    runtime.registry.register(Tool(
        name="restart",
        description=(
            "Restart the gateway daemon process. Defers until the current "
            "turn (and any other in-flight turns) finish — the response you "
            "produce after calling this will be delivered before the swap. "
            "Use sparingly: clients lose their WebSocket connection and have "
            "to reconnect; cron / channel jobs in flight are interrupted."
        ),
        input_schema={"type": "object", "properties": {}},
        run=_restart_tool,
    ))
