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
from nano_openclaw.dingtalk.bot import DingtalkBot, _clone_registry_for_dingtalk
from nano_openclaw.dingtalk.login_cli import load_persisted_creds
from nano_openclaw.dingtalk.policy import DingtalkPolicy
from nano_openclaw.dingtalk.token import DingtalkTokenManager
from nano_openclaw.logger import get_logger

if TYPE_CHECKING:
    from nano_openclaw.runtime import AgentRuntime
    from nano_openclaw.schedule.types import CronJob, CronRunRecord
    from nano_openclaw.tools import ToolRegistry


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

    def decorate_tools(self, base: "ToolRegistry", sender_key: str) -> "ToolRegistry":
        """Tag cron jobs and wakeups created during ``sender_key``'s turn.

        Same shape as :func:`_clone_registry_for_dingtalk`. ``sender_key``
        is the ``conversationId`` (DingTalk's natural session granularity)
        so cron completion routes back to the originating chat.
        """
        return _clone_registry_for_dingtalk(
            base,
            account_id=self.account.id,
            conversation_id=sender_key,
        )

    async def notify_completion(
        self,
        *,
        target_key: str,
        status: str,
        summary: str,
        job: "CronJob",
        record: "CronRunRecord",
    ) -> None:
        """Deliver a cron completion message back into ``target_key`` conv.

        ``target_key`` is the conversationId of the turn that created the
        job. We use the proactive message API rather than ``sessionWebhook``
        because the original webhook URL has long since expired by the time
        a scheduled job fires.

        Format mirrors WeChat's notification body but in Markdown since
        DingTalk's group/DM proactive endpoints render Markdown natively.
        """
        if self._bot is None:
            log.warning(
                "dingtalk.notify.no_bot",
                f"account={self.account.id}: notify_completion before start",
            )
            return
        body = (
            f"**任务通知** · {status}\n\n"
            f"`{job.name or job.id}`\n\n"
            f"{summary}".strip()
        )
        try:
            await self._bot.send_proactive(target_key, body)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "dingtalk.notify.send.error",
                f"target={target_key[:12]}… {type(exc).__name__}: {exc}",
            )
