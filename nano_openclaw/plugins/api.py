"""Narrow plugin API and hook types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Protocol

if TYPE_CHECKING:
    from nano_openclaw.config.types import NanoOpenClawConfig
    from nano_openclaw.plugins.registry import HookRegistry
    from nano_openclaw.core.tools import Tool, ToolRegistry

HookName = Literal[
    "before_prompt_build",
    "before_tool_call",
    "after_tool_call",
    "session_start",
    "session_end",
    "on_loop_event",
    "after_turn",
]

HookHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]


@dataclass
class PluginApi:
    id: str
    config: "NanoOpenClawConfig"
    plugin_config: dict[str, Any]
    _tool_registry: "ToolRegistry"
    _hook_registry: "HookRegistry"
    _slash_registrations: list[tuple[Any, ...]] = field(default_factory=list)
    _channel_registrations: list[Any] = field(default_factory=list)
    _feature_registrations: list[Any] = field(default_factory=list)

    def config_snapshot(self) -> "NanoOpenClawConfig":
        return self.config

    def register_tool(self, tool: "Tool") -> None:
        self._tool_registry.register(tool)

    def register_hook(
        self,
        event: HookName,
        handler: HookHandler,
        priority: int = 0,
    ) -> None:
        self._hook_registry.register(event, handler, priority)

    def register_slash(self, *registration: Any) -> None:
        self._slash_registrations.append(tuple(registration))
        from nano_openclaw.services.slash import register_slash_command

        register_slash_command(*registration)

    def register_channel(self, channel: Any) -> None:
        self._channel_registrations.append(channel)
        from nano_openclaw.services.channels import get_channel_manager

        get_channel_manager().register(channel)

    def register_feature(self, feature: Any) -> None:
        self._feature_registrations.append(feature)

    def tool_names(self) -> list[str]:
        return self._tool_registry.names()


class Plugin(Protocol):
    id: str
    name: str

    def register(self, api: PluginApi) -> None:
        ...
