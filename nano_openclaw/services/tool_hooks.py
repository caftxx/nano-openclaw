"""Service-owned hooks installed onto core tool registries."""

from __future__ import annotations

from typing import Any

from nano_openclaw.core.tools import ToolRegistry
from nano_openclaw.features.checkpoint.service import create_checkpoint


def install_checkpoint_write_hook(registry: ToolRegistry) -> None:
    def _checkpoint_before_workspace_write(tool_name: str, ctx: Any) -> None:
        create_checkpoint(
            ctx.state_dir,
            ctx.workspace_dir,
            reason=f"auto-before-{tool_name}",
        )

    registry.set_before_workspace_write(_checkpoint_before_workspace_write)
