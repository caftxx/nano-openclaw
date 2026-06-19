"""Built-in memory plugin."""

from pathlib import Path
from typing import Any

from nano_openclaw.features.memory.daily import build_daily_memory_prelude
from nano_openclaw.features.memory.extractor import clear_state as _clear_extractor_state
from nano_openclaw.features.memory.extractor import run_extractor
from nano_openclaw.features.memory.registry import build_memory_tools
from nano_openclaw.plugins.api import PluginApi


class MemoryPlugin:
    id = "nano-memory"
    name = "Memory"

    def register(self, api: PluginApi) -> None:
        for tool in build_memory_tools(api.config.memorySearch):
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

        # Stop-hook memory extractor (Phase 1). Fires after every main-agent
        # turn; ``run_extractor`` itself handles the trigger-source filter +
        # cooldown + mutual-exclusion gating, so we register unconditionally
        # and let the function early-return when disabled. priority=100 so we
        # run after other after_turn hooks (e.g. ReviewFork) have observed
        # the snapshot.
        async def _trigger_extractor(payload: dict[str, Any]) -> None:
            cfg = payload["loop_config"].extract_memories_config
            if cfg is None or not cfg.enabled:
                return
            await run_extractor(payload, cfg)

        async def _cleanup_extractor(payload: dict[str, Any]) -> None:
            # session_end payload uses ``session_id`` (runtime.close); after_turn
            # uses ``session_key`` (cfg.session_key, == session_id in normal
            # operation). Either one identifies the same _states slot.
            key = payload.get("session_key") or payload.get("session_id")
            if isinstance(key, str):
                _clear_extractor_state(key)

        api.register_hook("after_turn", _trigger_extractor, priority=100)
        api.register_hook("session_end", _cleanup_extractor, priority=100)
