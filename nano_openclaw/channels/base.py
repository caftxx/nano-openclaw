"""Channel abstract base + per-instance status / account types.

A ``Channel`` subclass defines one **kind** of integration (wechat, telegram).
Each ``Channel`` instance runs **one account** of that kind. The daemon
spawns one instance per (channel_id, account_id) pair from config.

``decorate_tools`` and ``notify_completion`` are the two extension points
where the channel injects its identity:

- ``decorate_tools(base, sender_key)`` wraps cron-creation tools so the
  ``created_by`` field gets the three-segment ``{channel}:{account}:{sender}``
  marker — that's how cron's completion routing knows to ping the right
  channel's right account on behalf of the right sender.
- ``notify_completion(...)`` is invoked by the cron scheduler when a job
  finishes; the channel decides what to do (push a wechat message, post to
  slack, ...).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal

if TYPE_CHECKING:
    from nano_openclaw.runtime import AgentRuntime
    from nano_openclaw.schedule.types import CronJob, CronRunRecord
    from nano_openclaw.tools import ToolRegistry


ChannelState = Literal["stopped", "starting", "running", "error"]


@dataclass
class ChannelAccount:
    """One configured account of a channel.

    ``id`` is the account label ("default" / "personal" / "work"). ``config``
    is whatever the specific channel needs (for wechat: ilink_token +
    base_url + notify_path).
    """

    id: str
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelStatus:
    """Public-facing status of one channel instance."""

    channel_id: str
    account_id: str
    state: ChannelState
    error: str | None = None
    started_at: float | None = None


class Channel(ABC):
    """One running instance = one channel × one account.

    Subclasses set ``id`` (class attribute) and implement ``start`` / ``stop``.
    Defaults for ``decorate_tools`` and ``notify_completion`` are no-ops so
    minimal channels need only the lifecycle pair.
    """

    id: ClassVar[str] = ""  # subclass MUST override (e.g., "wechat")

    def __init__(self, account: ChannelAccount) -> None:
        if not self.id:
            raise TypeError(f"{type(self).__name__}.id class attribute must be set")
        self.account = account
        self._state: ChannelState = "stopped"
        self._error: str | None = None
        self._started_at: float | None = None

    # ─── Lifecycle ───

    @abstractmethod
    async def start(self, runtime: "AgentRuntime", gateway: Any | None = None) -> None:
        """Launch the channel's background task(s).

        ``gateway`` is the ``GatewayContext`` once the daemon is wired up
        (Phase 3). For Phase 2 it's None — channels run with just the
        runtime reference.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Tear down the channel's background tasks. Idempotent."""

    def status(self) -> ChannelStatus:
        return ChannelStatus(
            channel_id=self.id,
            account_id=self.account.id,
            state=self._state,
            error=self._error,
            started_at=self._started_at,
        )

    # ─── Identity injection ───

    def decorate_tools(self, base: "ToolRegistry", sender_key: str) -> "ToolRegistry":
        """Return a registry suitable for one turn started by ``sender_key``.

        Default: no decoration — caller gets ``base`` back. Subclasses that
        care about cron notification routing override this to wrap
        ``cron_create`` / ``schedule_wakeup`` and inject
        ``created_by = "{channel}:{account}:{sender}"``.

        Per-turn shallow cloning of the registry is the **backend**'s job
        (see ``EmbeddedBackend._build_turn_registry``); decorate_tools is
        only about per-channel identity.
        """
        return base

    # ─── Completion notification ───

    async def notify_completion(
        self,
        *,
        target_key: str,
        status: str,
        summary: str,
        job: "CronJob",
        record: "CronRunRecord",
    ) -> None:
        """Cron scheduler calls this when a job created by a turn from this
        channel finishes. Default: no-op. Subclasses push a message, append
        to a queue, etc. Channels capture any runtime state they need at
        ``start()`` time, so this method does not receive ``runtime``.
        """
        return None

    # ─── created_by identity helpers ───

    def make_created_by(self, sender_key: str) -> str:
        """Three-segment marker: ``{channel_id}:{account_id}:{sender_key}``.

        Used by ``decorate_tools`` overrides to tag cron jobs/wakeups created
        on behalf of a specific sender within this channel/account.
        """
        return f"{self.id}:{self.account.id}:{sender_key}"
