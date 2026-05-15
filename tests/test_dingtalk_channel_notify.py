"""``DingtalkChannel.notify_completion`` routes through ``DingtalkBot.send_proactive``.

End-to-end check that a cron-completion event triggers a single
``bot.send_proactive`` call with the originating conversation as
``target_key`` and a non-empty Markdown body assembled from the status +
summary + job name.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nano_openclaw.channels.base import ChannelAccount
from nano_openclaw.channels.dingtalk import DingtalkChannel


@dataclass
class _FakeJob:
    id: str = "j-1"
    name: str = "morning-summary"


@dataclass
class _FakeRecord:
    ended_at: str | None = "2026-05-15T08:00:00"


class _FakeBot:
    """Minimal duck for the ``send_proactive`` path; collects invocations."""

    def __init__(self) -> None:
        self.proactive_calls: list[tuple[str, str]] = []

    async def send_proactive(self, conv_id: str, text: str) -> None:
        self.proactive_calls.append((conv_id, text))


def test_notify_completion_delegates_to_bot_send_proactive():
    ch = DingtalkChannel(ChannelAccount(id="ding-test", config={}))
    fake_bot = _FakeBot()
    ch._bot = fake_bot  # bypass start()

    async def run() -> None:
        await ch.notify_completion(
            target_key="conv-123",
            status="ok",
            summary="produced 3 charts",
            job=_FakeJob(),
            record=_FakeRecord(),
        )

    asyncio.run(run())

    assert len(fake_bot.proactive_calls) == 1
    conv_id, body = fake_bot.proactive_calls[0]
    assert conv_id == "conv-123"
    assert "morning-summary" in body
    assert "produced 3 charts" in body
    assert "ok" in body


def test_notify_completion_without_bot_logs_and_returns():
    """Cron completing before the channel has finished ``start()`` must not crash."""
    ch = DingtalkChannel(ChannelAccount(id="ding-test", config={}))
    assert ch._bot is None  # sanity

    async def run() -> None:
        await ch.notify_completion(
            target_key="conv-x",
            status="ok",
            summary="x",
            job=_FakeJob(),
            record=_FakeRecord(),
        )

    asyncio.run(run())  # no exception
