"""Memory runtime ports used by the core loop."""

from __future__ import annotations

from typing import Any

from nano_openclaw.features.memory.active import ActiveMemoryManager


async def recall_active_memory(
    *,
    client: Any,
    model: str,
    workspace_dir: str,
    config: Any,
    memory_search_config: Any | None = None,
    messages: list[dict[str, Any]],
) -> Any | None:
    if not config or not getattr(config, "enabled", False):
        return None
    manager = ActiveMemoryManager(
        client=client,
        model=model,
        workspace_dir=workspace_dir,
        config=config,
        memory_search_config=memory_search_config,
    )
    return await manager.run(messages)
