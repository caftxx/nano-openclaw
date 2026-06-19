"""``WechatBot.run`` long-backs-off on errcode=-14 instead of hot-retrying.

When iLink revokes the token, ``getUpdates`` returns ``errcode=-14``. The bot
must not tight-loop (it would spam the server and the log) — it should sleep
``POLL_SESSION_EXPIRED_BACKOFF`` and emit a single high-priority log line
telling the user to re-login.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.core.tools import ToolRegistry
from nano_openclaw.wechat import bot as bot_module
from nano_openclaw.wechat.bot import WechatBot
from nano_openclaw.wechat.ilink import is_session_expired


def test_discover_persisted_account_ids(tmp_path):
    from nano_openclaw.wechat.login_cli import discover_persisted_account_ids

    # Empty state_dir → empty list, not a crash.
    assert discover_persisted_account_ids(tmp_path / "missing") == []
    assert discover_persisted_account_ids(tmp_path) == []

    (tmp_path / "wechat-tokens.json").write_text("{}", encoding="utf-8")
    (tmp_path / "wechat-tokens.dj.json").write_text("{}", encoding="utf-8")
    (tmp_path / "wechat-tokens.work.json").write_text("{}", encoding="utf-8")
    # Sibling files that should NOT be picked up.
    (tmp_path / "wechat-sessions.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notify-queue.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "wechat-tokens.bak").write_text("{}", encoding="utf-8")  # wrong suffix

    assert sorted(discover_persisted_account_ids(tmp_path)) == ["default", "dj", "work"]


def test_is_session_expired_detects_minus_14():
    assert is_session_expired({"errcode": -14}) is True
    assert is_session_expired({"ret": -14}) is True
    assert is_session_expired({"errcode": -14, "ret": 0}) is True
    assert is_session_expired({"errcode": 0, "ret": 0}) is False
    assert is_session_expired({"ret": -1, "errcode": -1}) is False
    assert is_session_expired({}) is False


def _fake_runtime(tmp_path: Path) -> SimpleNamespace:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    cfg = LoopConfig(model="test-model", workspace_dir=workspace, session_key="default")
    return SimpleNamespace(
        agent_id="default",
        config=SimpleNamespace(),
        warnings=[],
        client=None,
        registry=ToolRegistry(),
        cfg=cfg,
        hook_registry=None,
        state_dir=state,
        workspace_dir=workspace,
    )


def test_session_expired_triggers_long_backoff(tmp_path, monkeypatch):
    bot = WechatBot(base_url="http://x", token="bad")
    bot.POLL_SESSION_EXPIRED_BACKOFF = 0.01  # speed up; semantic is 'long', value irrelevant for test

    sleep_durations: list[float] = []
    update_calls = {"n": 0}

    async def fake_get_updates(client, base_url, token, buf, timeout):
        update_calls["n"] += 1
        return {"errcode": -14, "errmsg": "session expired"}

    async def fake_sleep(d: float) -> None:
        sleep_durations.append(d)
        # Stop after the first long backoff to keep the test bounded.
        if d == bot.POLL_SESSION_EXPIRED_BACKOFF:
            raise asyncio.CancelledError

    monkeypatch.setattr(bot_module, "get_updates", fake_get_updates)
    monkeypatch.setattr(bot_module.asyncio, "sleep", fake_sleep)
    # _poll_notifications would otherwise sleep on its own loop forever.
    async def noop_notify(self): return None
    monkeypatch.setattr(bot_module.WechatBot, "_poll_notifications", noop_notify)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.run())

    assert update_calls["n"] == 1
    # First (and only) sleep should be the session-expired backoff, not the
    # short retry — the fingerprint that distinguishes the new code path.
    assert sleep_durations[0] == bot.POLL_SESSION_EXPIRED_BACKOFF
    assert bot._session_expired_at is not None
