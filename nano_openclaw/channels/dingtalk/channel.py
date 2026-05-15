"""``DingtalkChannel`` — adapts :class:`DingtalkStreamClient` to the
:class:`Channel` Protocol.

One ``DingtalkChannel`` instance = one ``ChannelAccount`` = one ``clientId``
worth of credentials at ``state_dir/dingtalk-creds.{clientId}.json``. The
daemon spawns N instances for N persisted creds files.

PR1 scope: lifecycle only. Inbound CALLBACK frames are logged but not
processed — message extraction, policy, session lookup, and replies all land
in PR2+. Cron-completion routing (``decorate_tools`` /
``notify_completion``) lands in PR4.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, ClassVar

from nano_openclaw.channels.base import Channel, ChannelAccount
from nano_openclaw.dingtalk.login_cli import load_persisted_creds
from nano_openclaw.dingtalk.stream_client import DingtalkStreamClient
from nano_openclaw.dingtalk.token import DingtalkTokenManager
from nano_openclaw.logger import get_logger

if TYPE_CHECKING:
    from nano_openclaw.dingtalk.frames import CallbackFrame, EventFrame, SystemFrame
    from nano_openclaw.runtime import AgentRuntime


log = get_logger(__name__)


class DingtalkChannel(Channel):
    """One DingTalk robot hosted as a daemon-managed channel.

    ``account.id`` is the ``clientId`` — that's how the creds file is
    addressed and also the most useful identity in logs.
    """

    id: ClassVar[str] = "dingtalk"

    def __init__(self, account: ChannelAccount) -> None:
        super().__init__(account)
        self._stream: DingtalkStreamClient | None = None
        self._token_mgr: DingtalkTokenManager | None = None
        self._task: asyncio.Task[None] | None = None
        # PR2+ business state hangs off here (conv→session map, reply
        # dispatcher, notify queue, …). Kept as fields rather than imported
        # names so the type-checker sees nothing PR1 can't actually use.

    async def start(self, runtime: "AgentRuntime", gateway: Any | None = None) -> None:
        if self._task is not None and not self._task.done():
            return

        self._state = "starting"
        self._error = None

        creds = load_persisted_creds(runtime.state_dir, self.account.id)
        client_id = str(creds.get("clientId") or "")
        client_secret = str(creds.get("clientSecret") or "")
        if not client_id or not client_secret:
            self._state = "error"
            self._error = (
                f"dingtalk account {self.account.id!r}: missing clientId/clientSecret; "
                f"run `nano-openclaw dingtalk register --client-id ... --client-secret ...`"
            )
            log.warning("dingtalk.channel.missing_creds", self._error)
            raise ValueError(self._error)

        self._token_mgr = DingtalkTokenManager()
        self._stream = DingtalkStreamClient(
            client_id=client_id,
            client_secret=client_secret,
            on_callback=self._on_callback,
            on_event=self._on_event,
            on_system=self._on_system,
        )
        self._stream.set_status_callback(self._on_stream_status)

        self._task = asyncio.create_task(
            self._stream.run(),
            name=f"dingtalk:{client_id[:8]}",
        )
        self._state = "running"
        self._started_at = time.time()
        log.info(
            "dingtalk.channel.start",
            f"client_id={client_id[:8]}… dm_policy={creds.get('dmPolicy')} "
            f"group_policy={creds.get('groupPolicy')} require_mention={creds.get('requireMention')}",
        )

    async def stop(self) -> None:
        if self._task is None:
            self._state = "stopped"
            return
        if self._stream is not None:
            await self._stream.stop()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, BaseException):  # noqa: BLE001
            pass
        self._task = None
        self._stream = None
        self._token_mgr = None
        self._state = "stopped"
        self._started_at = None
        log.info("dingtalk.channel.stop", f"account={self.account.id}")

    # ── Stream callbacks (PR1: log only) ───────────────────────────────────

    async def _on_callback(self, frame: "CallbackFrame") -> None:
        log.info(
            "dingtalk.channel.callback",
            f"account={self.account.id} topic={frame.headers.topic} "
            f"messageId={frame.headers.messageId}",
        )

    async def _on_event(self, frame: "EventFrame") -> None:
        log.debug(
            "dingtalk.channel.event",
            f"account={self.account.id} topic={frame.headers.topic} "
            f"eventType={frame.headers.eventType}",
        )

    async def _on_system(self, frame: "SystemFrame") -> None:
        log.debug(
            "dingtalk.channel.system",
            f"account={self.account.id} topic={frame.headers.topic}",
        )

    def _on_stream_status(self, state: str) -> None:
        if state in ("connecting", "connected", "reconnecting"):
            self._state = "running" if state == "connected" else "starting"
        elif state == "stopped":
            self._state = "stopped"
