"""ChannelManager — tracks ChannelAdapter classes + running instances.

Two roles:

1. **Class registry** (process-wide): ``register(WechatChannel)`` makes the
   "wechat" name resolvable. Subclass registration happens at import time
   (see ``adapters/channels/wechat.py``).
2. **Instance registry** (per gateway): ``start(channel_id, account)`` /
   ``stop(...)`` track running ``ChannelAdapter`` instances keyed by
   ``(channel_id, account_id)``. ``dispatch_notify`` routes a cron
   completion to the right instance.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from nano_openclaw.logger import get_logger

if TYPE_CHECKING:
    from nano_openclaw.core.runtime import AgentRuntime
    from nano_openclaw.features.schedule.types import CronJob, CronRunRecord
    from nano_openclaw.core.tools import ToolRegistry


log = get_logger(__name__)


ChannelState = Literal["stopped", "starting", "running", "error"]


@dataclass
class ChannelAccount:
    """One configured account of a channel adapter."""

    id: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelStatus:
    """Public-facing status of one channel adapter instance."""

    channel_id: str
    account_id: str
    state: ChannelState
    error: str | None = None
    started_at: float | None = None


class ChannelAdapter(ABC):
    """One running instance = one channel adapter x one account."""

    id: ClassVar[str] = ""

    def __init__(self, account: ChannelAccount) -> None:
        if not self.id:
            raise TypeError(f"{type(self).__name__}.id class attribute must be set")
        self.account = account
        self._state: ChannelState = "stopped"
        self._error: str | None = None
        self._started_at: float | None = None

    @abstractmethod
    async def start(self, runtime: "AgentRuntime", gateway: Any | None = None) -> None:
        """Launch the channel adapter's background task(s)."""

    @abstractmethod
    async def stop(self) -> None:
        """Tear down the channel adapter's background tasks. Idempotent."""

    def status(self) -> ChannelStatus:
        return ChannelStatus(
            channel_id=self.id,
            account_id=self.account.id,
            state=self._state,
            error=self._error,
            started_at=self._started_at,
        )

    def decorate_tools(self, base: "ToolRegistry", sender_key: str) -> "ToolRegistry":
        """Return a registry suitable for one turn started by ``sender_key``."""
        return base

    async def notify_completion(
        self,
        *,
        target_key: str,
        status: str,
        summary: str,
        job: "CronJob",
        record: "CronRunRecord",
    ) -> None:
        """Cron scheduler calls this when a channel-created job finishes."""
        return None

    def make_created_by(self, sender_key: str) -> str:
        """Three-segment marker: ``{channel_id}:{account_id}:{sender_key}``."""
        return f"{self.id}:{self.account.id}:{sender_key}"


class ChannelManager:
    """Holds ChannelAdapter subclasses and their running instances."""

    def __init__(self) -> None:
        self._classes: dict[str, type[ChannelAdapter]] = {}
        self._instances: dict[tuple[str, str], ChannelAdapter] = {}

    # ─── Class registration ───

    def register(self, channel_class: type[ChannelAdapter], *, replace: bool = False) -> None:
        """Register a ChannelAdapter subclass under its ``id``.

        Idempotent on the same class. ``replace=True`` is reserved for
        runtime-scoped plugin reloads where the same channel id is backed by a
        fresh class object after a config/model rebuild.
        """
        cid = channel_class.id
        if not cid:
            raise ValueError(f"{channel_class.__name__}.id must be a non-empty string")
        existing = self._classes.get(cid)
        if existing is channel_class:
            return
        if existing is not None and replace:
            self._classes[cid] = channel_class
            return
        if existing is not None:
            raise ValueError(f"ChannelAdapter id {cid!r} already registered to {existing.__name__}")
        self._classes[cid] = channel_class

    def get_class(self, channel_id: str) -> type[ChannelAdapter] | None:
        return self._classes.get(channel_id)

    def known_channels(self) -> list[str]:
        return list(self._classes.keys())

    # ─── Instance lifecycle ───

    async def start(
        self,
        channel_id: str,
        account: ChannelAccount,
        runtime: "AgentRuntime",
        gateway: Any | None = None,
    ) -> ChannelAdapter:
        """Instantiate and start a ChannelAdapter. Idempotent if already running."""
        cls = self._classes.get(channel_id)
        if cls is None:
            raise KeyError(f"ChannelAdapter {channel_id!r} not registered (known: {sorted(self._classes)})")
        key = (channel_id, account.id)
        existing = self._instances.get(key)
        if existing is not None:
            return existing
        instance = cls(account)
        self._instances[key] = instance
        try:
            await instance.start(runtime, gateway)
        except Exception:
            # Roll back the instance entry on failure so retry is possible.
            self._instances.pop(key, None)
            raise
        return instance

    async def stop(self, channel_id: str, account_id: str) -> None:
        """Stop a running ChannelAdapter. No-op if not running."""
        key = (channel_id, account_id)
        instance = self._instances.pop(key, None)
        if instance is None:
            return
        try:
            await instance.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("channel.stop.error", f"{channel_id}/{account_id}: {type(exc).__name__}: {exc}")

    async def stop_all(self) -> None:
        keys = list(self._instances.keys())
        if not keys:
            return
        await asyncio.gather(
            *(self.stop(cid, aid) for cid, aid in keys),
            return_exceptions=True,
        )

    async def restart_all(self, runtime: "AgentRuntime", gateway: Any | None = None) -> None:
        """Restart every running instance against the current class registry.

        Used after runtime hot reloads so channel adapters stop holding old
        runtime/backend references. Restart failures are logged and leave the
        failed instance stopped; runtime updates should not fail because a
        chat channel cannot reconnect immediately.
        """
        running = [
            (channel_id, instance.account)
            for (channel_id, _account_id), instance in list(self._instances.items())
        ]
        if not running:
            return
        for channel_id, account in running:
            await self.stop(channel_id, account.id)
        for channel_id, account in running:
            try:
                await self.start(channel_id, account, runtime, gateway)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "channel.restart.error",
                    f"{channel_id}/{account.id}: {type(exc).__name__}: {exc}",
                )

    def get_instance(self, channel_id: str, account_id: str) -> ChannelAdapter | None:
        return self._instances.get((channel_id, account_id))

    def list_status(self) -> list[ChannelStatus]:
        return [inst.status() for inst in self._instances.values()]

    # ─── Cron completion dispatch ───

    @staticmethod
    def parse_created_by(created_by: str) -> tuple[str, str, str] | None:
        """Parse a ``created_by`` marker into ``(channel_id, account_id, sender_key)``.

        Accepts both new three-segment form ``{channel}:{account}:{sender}``
        and the legacy two-segment ``{channel}:{sender}`` (assumes account
        ``"default"``). Returns None if the marker isn't channel-routable.
        """
        if not created_by or ":" not in created_by:
            return None
        parts = created_by.split(":", 2)
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return parts[0], "default", parts[1]
        return None

    async def dispatch_notify(
        self,
        *,
        created_by: str,
        status: str,
        summary: str,
        job: "CronJob",
        record: "CronRunRecord",
    ) -> bool:
        """Route a cron completion to the originating ChannelAdapter's notify_completion.

        Returns True if a ChannelAdapter handled it, False otherwise. Never raises:
        the cron scheduler must never crash because a notification target
        went away.
        """
        parsed = self.parse_created_by(created_by)
        if parsed is None:
            return False
        channel_id, account_id, sender_key = parsed
        instance = self._instances.get((channel_id, account_id))
        if instance is None:
            log.debug(
                "channel.notify.no_instance",
                f"channel {channel_id}/{account_id} not running, dropping notify",
            )
            return False
        try:
            await instance.notify_completion(
                target_key=sender_key,
                status=status,
                summary=summary,
                job=job,
                record=record,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "channel.notify.error",
                f"{channel_id}/{account_id} notify_completion: {type(exc).__name__}: {exc}",
            )
            return False


# ─── Process-wide singleton ───

_GLOBAL_REGISTRY: ChannelManager | None = None


def get_channel_manager() -> ChannelManager:
    """Lazy singleton. Subclasses register themselves at import time via this."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = ChannelManager()
    return _GLOBAL_REGISTRY
