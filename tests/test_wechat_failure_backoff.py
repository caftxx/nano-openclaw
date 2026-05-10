"""``WechatBot.run`` escalates retry delay after consecutive failures.

Goal: stay quick on transient blips (sleep ``POLL_RETRY_DELAY``) but cool off
to ``POLL_BACKOFF_DELAY`` when iLink is genuinely down. Resetting the
counter on success keeps the schedule recurring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nano_openclaw.loop import LoopConfig
from nano_openclaw.tools import ToolRegistry
from nano_openclaw.wechat import bot as bot_module
from nano_openclaw.wechat.bot import WechatBot


def _fake_runtime(tmp_path: Path) -> SimpleNamespace:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    cfg = LoopConfig(model="test-model", workspace_dir=workspace, session_key="default")
    return SimpleNamespace(
        agent_id="default",
        config=SimpleNamespace(wechat=SimpleNamespace(accounts=[])),
        warnings=[],
        client=None,
        registry=ToolRegistry(),
        cfg=cfg,
        hook_registry=None,
        state_dir=state,
        workspace_dir=workspace,
    )


def _patch_run_loop(monkeypatch, bot: WechatBot, response_seq, sleep_record, max_iters: int):
    """Stub ``get_updates`` to walk ``response_seq`` and asyncio.sleep to record + bail."""
    iter_count = {"n": 0}
    seq = iter(response_seq)

    async def fake_get_updates(client, base_url, token, buf, timeout):
        iter_count["n"] += 1
        if iter_count["n"] > max_iters:
            raise asyncio.CancelledError
        try:
            item = next(seq)
        except StopIteration:
            raise asyncio.CancelledError
        if isinstance(item, Exception):
            raise item
        return item

    async def fake_sleep(d: float) -> None:
        sleep_record.append(d)

    monkeypatch.setattr(bot_module, "get_updates", fake_get_updates)
    monkeypatch.setattr(bot_module.asyncio, "sleep", fake_sleep)
    async def noop_notify(self): return None
    monkeypatch.setattr(bot_module.WechatBot, "_poll_notifications", noop_notify)


def test_first_two_failures_use_short_delay(tmp_path, monkeypatch):
    bot = WechatBot(runtime=_fake_runtime(tmp_path), base_url="http://x", token="t")
    sleeps: list[float] = []
    _patch_run_loop(
        monkeypatch, bot,
        response_seq=[RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")],
        sleep_record=sleeps,
        max_iters=3,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.run())
    assert sleeps == [
        bot.POLL_RETRY_DELAY,
        bot.POLL_RETRY_DELAY,
        bot.POLL_BACKOFF_DELAY,
    ]


def test_success_resets_failure_counter(tmp_path, monkeypatch):
    """Two failures → success → two more failures must NOT yet hit BACKOFF_DELAY.

    If success didn't reset the counter, the third overall failure would
    trip the long backoff. With the reset, it should still be RETRY_DELAY.
    """
    bot = WechatBot(runtime=_fake_runtime(tmp_path), base_url="http://x", token="t")
    sleeps: list[float] = []
    _patch_run_loop(
        monkeypatch, bot,
        response_seq=[
            RuntimeError("boom"),     # fail 1 → retry
            RuntimeError("boom"),     # fail 2 → retry
            {"ret": 0, "msgs": [], "get_updates_buf": "b"},  # success → reset
            RuntimeError("boom"),     # fail 1 again → retry (would be backoff w/o reset)
            RuntimeError("boom"),     # fail 2 → retry
        ],
        sleep_record=sleeps,
        max_iters=5,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.run())
    # All four failure sleeps should be short retries; success itself doesn't sleep.
    assert sleeps == [
        bot.POLL_RETRY_DELAY,
        bot.POLL_RETRY_DELAY,
        bot.POLL_RETRY_DELAY,
        bot.POLL_RETRY_DELAY,
    ]
