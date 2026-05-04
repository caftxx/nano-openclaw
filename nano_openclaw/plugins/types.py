"""Plugin protocol and hook types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Protocol

if TYPE_CHECKING:
    from nano_openclaw.config.types import NanoOpenClawConfig
    from nano_openclaw.plugins.registry import HookRegistry
    from nano_openclaw.tools import Tool, ToolRegistry

HookName = Literal[
    "before_prompt_build",
    "before_tool_call",
    "after_tool_call",
    "session_start",
    "session_end",
    "on_loop_event",
]

HookHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]


@dataclass
class PluginApi:
    id: str
    config: "NanoOpenClawConfig"
    plugin_config: dict[str, Any]
    _tool_registry: "ToolRegistry"
    _hook_registry: "HookRegistry"

    def register_tool(self, tool: "Tool") -> None:
        self._tool_registry.register(tool)

    def register_hook(
        self,
        event: HookName,
        handler: HookHandler,
        priority: int = 0,
    ) -> None:
        self._hook_registry.register(event, handler, priority)

    def tool_names(self) -> list[str]:
        return self._tool_registry.names()


class Plugin(Protocol):
    id: str
    name: str

    def register(self, api: PluginApi) -> None:
        ...
