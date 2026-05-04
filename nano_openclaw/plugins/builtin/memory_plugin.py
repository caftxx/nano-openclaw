"""Built-in memory plugin."""

from pathlib import Path
from typing import Any

from nano_openclaw.memory.daily import build_daily_memory_prelude
from nano_openclaw.plugins.types import PluginApi
from nano_openclaw.tools import build_memory_tools


class MemoryPlugin:
    id = "nano-memory"
    name = "Memory"

    def register(self, api: PluginApi) -> None:
        for tool in build_memory_tools():
            api.register_tool(tool)

        def inject_daily_memory(payload: dict[str, Any]) -> dict[str, Any] | None:
            workspace = payload.get("workspace_dir")
            if not workspace:
                return None
            prelude = build_daily_memory_prelude(Path(workspace))
            if not prelude:
                return None
            return {"prepend": prelude}

        api.register_hook("before_prompt_build", inject_daily_memory, priority=-100)
