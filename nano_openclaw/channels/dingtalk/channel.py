"""``DingtalkChannel`` — adapts :class:`DingtalkBot` to the :class:`Channel` Protocol.

One ``DingtalkChannel`` instance = one ``ChannelAccount`` = one ``clientId``
worth of credentials at ``state_dir/dingtalk-creds.{clientId}.json``. The
daemon spawns N instances for N persisted creds files.

PR2 wires the channel to a real :class:`DingtalkBot`: inbound messages are
extracted, policy-checked, and run through ``AgentSession.run_turn``; the
reply goes back via the inbound message's ``sessionWebhook``. AI Card
streaming, media handling, and cron-completion routing land in PR3/PR4.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, ClassVar

from nano_openclaw.channels.base import Channel, ChannelAccount
from nano_openclaw.dingtalk.bot import DingtalkBot
from nano_openclaw.dingtalk.login_cli import load_persisted_creds
from nano_openclaw.dingtalk.policy import DingtalkPolicy
from nano_openclaw.dingtalk.token import DingtalkTokenManager
from nano_openclaw.logger import get_logger

if TYPE_CHECKING:
    from nano_openclaw.runtime import AgentRuntime


log = get_logger(__name__)


class DingtalkChannel(Channel):
    """One DingTalk robot hosted as a daemon-managed channel.

    ``account.id`` is the ``clientId`` — the creds file is named by it and
    it's the most useful identity in logs and ``/channels`` output.
    """

    id: ClassVar[str] = "dingtalk"

    def __init__(self, account: ChannelAccount) -> None:
        super().__init__(account)
        self._bot: DingtalkBot | None = None
        self._token_mgr: DingtalkTokenManager | None = None
        self._task: asyncio.Task[None] | None = None

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

        # Daemon-mode: route per-conversation sessions through the shared
        # BackendSessionManager so they show up in /sessions / WebUI alongside
        # TUI sessions. Standalone runs don't get session persistence.
        session_manager = None
        backend = None
        conv_map_path = None
        if gateway is not None and getattr(gateway, "backend", None) is not None:
            backend = gateway.backend
            session_manager = backend.manager
            conv_map_path = runtime.state_dir / f"dingtalk-sessions.{client_id}.json"

        self._token_mgr = DingtalkTokenManager()
        policy = DingtalkPolicy.from_creds(creds)
        self._bot = DingtalkBot(
            runtime=runtime,
            account_id=self.account.id,
            client_id=client_id,
            client_secret=client_secret,
            policy=policy,
            session_manager=session_manager,
            backend=backend,
            conv_map_path=conv_map_path,
        )

        self._task = asyncio.create_task(
            self._bot.run(),
            name=f"dingtalk:{client_id[:8]}",
        )
        self._state = "running"
        self._started_at = time.time()
        log.info(
            "dingtalk.channel.start",
            f"client_id={client_id[:8]}… dm_policy={policy.dm_policy} "
            f"group_policy={policy.group_policy} require_mention={policy.require_mention} "
            f"session_manager={'wired' if session_manager else 'standalone'}",
        )

    async def stop(self) -> None:
        if self._task is None:
            self._state = "stopped"
            return
        if self._bot is not None:
            await self._bot.stop()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, BaseException):  # noqa: BLE001
            pass
        self._task = None
        self._bot = None
        self._token_mgr = None
        self._state = "stopped"
        self._started_at = None
        log.info("dingtalk.channel.stop", f"account={self.account.id}")
