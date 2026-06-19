"""``WechatChannel`` — adapts ``WechatBot`` to the ``ChannelAdapter`` Protocol.

One ``WechatChannel`` instance = one ``ChannelAccount`` = one configured iLink
token. The daemon spawns N instances for N configured accounts. Each manages
its own background ``WechatBot.run()`` task and its own ``NotifyQueue``.

The legacy ``nano-openclaw wechat`` subcommand path still constructs a
``WechatBot`` directly without going through this ChannelAdapter; that path is
deprecated and will be removed when the daemon (Phase 3) ships.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from nano_openclaw.adapters.channels.base import ChannelAdapter, ChannelAccount
from nano_openclaw.logger import get_logger
from nano_openclaw.services.channels import get_channel_manager
from nano_openclaw.wechat.bot import WechatBot
from nano_openclaw.wechat.login_cli import load_persisted_token
from nano_openclaw.wechat.notify import NotifyItem, NotifyQueue

if TYPE_CHECKING:
    from nano_openclaw.core.runtime import AgentRuntime
    from nano_openclaw.features.schedule.types import CronJob, CronRunRecord


log = get_logger(__name__)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


class WechatChannel(ChannelAdapter):
    """One iLink account hosted as a daemon-managed ChannelAdapter."""

    id: ClassVar[str] = "wechat"

    def __init__(self, account: ChannelAccount) -> None:
        super().__init__(account)
        self._bot: WechatBot | None = None
        self._task: asyncio.Task[None] | None = None
        self._notify_queue: NotifyQueue | None = None

    async def start(self, runtime: "AgentRuntime", gateway: Any | None = None) -> None:
        """Build a ``WechatBot`` for this account and run it as a background task.

        When ``gateway`` is provided (daemon mode), wire the bot's per-uid
        sessions through ``backend.manager`` so they show up in
        ``/sessions`` / WebUI alongside TUI sessions. Each uid → session_id
        mapping persists at ``state_dir/wechat-sessions.{account}.json``
        so daemon restarts don't fork a new session per known user.
        """
        if self._task is not None and not self._task.done():
            return  # already running

        self._state = "starting"
        self._error = None

        # Token resolution order:
        #   1. state_dir/wechat-tokens.{account}.json — written by
        #      ``nano-openclaw wechat login`` after a successful QR login.
        #   2. account.config.ilink_token — what's in nano-openclaw.json5.
        # The persisted file also carries the server-provided base_url, which
        # may differ from the configured one (the iLink server can route bots
        # to a sharded instance after login). When present, the persisted
        # base_url wins so we keep talking to the shard the token belongs to.
        persisted_token, persisted_base_url = load_persisted_token(runtime.state_dir, self.account.id)
        token = persisted_token or str(self.account.config.get("ilink_token") or "")
        if not token:
            self._state = "error"
            self._error = (
                f"wechat account {self.account.id!r}: missing ilink_token; "
                f"run `nano-openclaw wechat login --account={self.account.id}` to log in"
            )
            log.warning("wechat.channel.missing_token", self._error)
            raise ValueError(self._error)

        base_url = (
            persisted_base_url
            or str(self.account.config.get("ilink_base_url") or "")
            or DEFAULT_BASE_URL
        )
        notify_path_str = str(self.account.config.get("notify_queue_path") or "")
        if notify_path_str:
            notify_path = Path(notify_path_str)
        else:
            # One queue file per account so two accounts don't fight over a
            # shared file. Keeps the legacy single-account default at
            # state_dir/notify-queue.jsonl when the legacy migration sets the
            # account id to "default" with no override.
            suffix = "" if self.account.id == "default" else f".{self.account.id}"
            notify_path = runtime.state_dir / f"notify-queue{suffix}.jsonl"
        self._notify_queue = NotifyQueue(notify_path)

        # Pull the daemon's BackendSessionManager off the GatewayContext when
        # available; ``None`` falls back to the bot's legacy in-memory dict.
        session_manager = None
        uid_map_path = None
        if gateway is not None and getattr(gateway, "backend", None) is not None:
            session_manager = gateway.backend.manager
            suffix = "" if self.account.id == "default" else f".{self.account.id}"
            uid_map_path = runtime.state_dir / f"wechat-sessions{suffix}.json"

        # Polling / typing / notify intervals use ``WechatBot``'s built-in
        # defaults — they're not exposed via config any more (no real-world
        # demand to override and the values mirror openilink-sdk-python).
        backend = getattr(gateway, "backend", None) if gateway is not None else None
        self._bot = WechatBot(
            runtime=runtime,
            base_url=base_url,
            token=token,
            notify_queue=self._notify_queue,
            account_id=self.account.id,
            session_manager=session_manager,
            backend=backend,
            uid_map_path=uid_map_path,
        )
        self._task = asyncio.create_task(
            self._bot.run(),
            name=f"wechat:{self.account.id}",
        )
        self._state = "running"
        self._started_at = time.time()
        log.info(
            "wechat.channel.start",
            f"account={self.account.id} base_url={base_url} notify_queue={notify_path} "
            f"session_manager={'wired' if session_manager else 'legacy in-memory'}",
        )

    async def stop(self) -> None:
        """Cancel the bot's background task and release resources."""
        if self._task is None:
            self._state = "stopped"
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, BaseException):
            pass
        self._task = None
        self._bot = None
        self._notify_queue = None
        self._state = "stopped"
        self._started_at = None
        log.info("wechat.channel.stop", f"account={self.account.id}")

    def decorate_tools(self, base, sender_key: str):
        # Wrap cron_create / schedule_wakeup with three-segment created_by.
        # Reuse the existing helper rather than duplicate the logic; it already
        # handles the multi-account marker via its ``account_id`` parameter.
        from nano_openclaw.wechat.bot import _clone_registry
        return _clone_registry(base, sender_key, self.account.id)

    async def notify_completion(
        self,
        *,
        target_key: str,
        status: str,
        summary: str,
        job: "CronJob",
        record: "CronRunRecord",
    ) -> None:
        """Append a notification for the originating user to this account's queue.

        The bot's ``_poll_notifications`` loop drains the queue and pushes each
        item over iLink. Decoupling via the on-disk queue means a temporarily
        offline ChannelAdapter doesn't lose notifications.
        """
        if self._notify_queue is None:
            log.warning(
                "wechat.channel.notify.no_queue",
                f"account={self.account.id}: notify_completion called before start",
            )
            return
        ended_at = record.ended_at or ""
        self._notify_queue.append(NotifyItem(
            job_id=job.id,
            job_name=job.name,
            status=status,
            result_summary=summary,
            created_at=ended_at,
            target_uid=target_key,
            sent=False,
        ))
        log.info(
            "wechat.channel.notify.queued",
            f"account={self.account.id} target={target_key:.16} job={job.name}",
        )


get_channel_manager().register(WechatChannel)
