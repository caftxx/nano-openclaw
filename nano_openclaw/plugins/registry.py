"""Hook registry for nano-openclaw plugins."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from nano_openclaw.logger import get_logger
from nano_openclaw.plugins.api import HookHandler

logger = get_logger(__name__)


@dataclass(frozen=True)
class LoadedPlugin:
    id: str
    name: str
    source: str
    entry: str
    tools: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedHook:
    event: str
    plugin_id: str
    plugin_name: str
    priority: int


@dataclass
class HookRegistry:
    _handlers: dict[str, list[tuple[int, HookHandler]]] = field(default_factory=dict)
    _plugins: list[LoadedPlugin] = field(default_factory=list)
    _hooks: list[LoadedHook] = field(default_factory=list)

    def register(self, event: str, handler: HookHandler, priority: int = 0) -> None:
        bucket = self._handlers.setdefault(event, [])
        bucket.append((priority, handler))
        bucket.sort(key=lambda item: item[0])

    def handler_counts(self) -> dict[str, int]:
        return {event: len(handlers) for event, handlers in self._handlers.items()}

    def record_plugin(self, plugin: LoadedPlugin) -> None:
        self._plugins.append(plugin)

    def plugins(self) -> list[LoadedPlugin]:
        return list(self._plugins)

    def record_plugin_hooks(self, hooks: list[LoadedHook]) -> None:
        self._hooks.extend(hooks)

    def hooks_by_event(self) -> dict[str, list[LoadedHook]]:
        by_event: dict[str, list[LoadedHook]] = {}
        for hook in self._hooks:
            by_event.setdefault(hook.event, []).append(hook)
        for hooks in by_event.values():
            hooks.sort(key=lambda h: (h.priority, h.plugin_id))
        return by_event

    async def run(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Run hooks in priority order.

        Handlers may return a partial payload update. Hook failures are fail-open:
        the error is logged and subsequent handlers still run.
        """
        for _priority, handler in self._handlers.get(event, []):
            try:
                result = handler(payload)
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, dict):
                    payload = {**payload, **result}
            except Exception as exc:  # noqa: BLE001 - plugin errors must not break core loop
                logger.warning("plugin.hook.error", f"hook {event} handler error: {exc}")
        return payload
