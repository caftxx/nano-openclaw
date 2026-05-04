"""Hook registry for nano-openclaw plugins."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from nano_openclaw.plugins.types import HookHandler

logger = logging.getLogger(__name__)


@dataclass
class HookRegistry:
    _handlers: dict[str, list[tuple[int, HookHandler]]] = field(default_factory=dict)

    def register(self, event: str, handler: HookHandler, priority: int = 0) -> None:
        bucket = self._handlers.setdefault(event, [])
        bucket.append((priority, handler))
        bucket.sort(key=lambda item: item[0])

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
                logger.warning("hook %s handler error: %s", event, exc)
        return payload
