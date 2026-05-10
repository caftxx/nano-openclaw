"""``WechatBot._resolve_typing_ticket`` reuses cached tickets within their TTL.

The bot serves many concurrent uids; without caching, every reply pays a
``getconfig`` round-trip for the typing indicator. Verify hit / miss / expiry
semantics so we don't regress that latency win.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nano_openclaw.loop import LoopConfig
from nano_openclaw.tools import ToolRegistry
from nano_openclaw.wechat.bot import WechatBot
from nano_openclaw.wechat import bot as bot_module


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


def test_typing_ticket_cache_hit_skips_getconfig(tmp_path, monkeypatch):
    bot = WechatBot(runtime=_fake_runtime(tmp_path), base_url="http://x", token="tok")

    calls: list[str] = []

    async def fake_get_typing_ticket(client, base_url, token, uid, ctx=None):
        calls.append(uid)
        return f"ticket-{uid}"

    monkeypatch.setattr(bot_module, "get_typing_ticket", fake_get_typing_ticket)

    async def run():
        t1 = await bot._resolve_typing_ticket(client=None, uid="u1", ctx="c")
        t2 = await bot._resolve_typing_ticket(client=None, uid="u1", ctx="c")
        t3 = await bot._resolve_typing_ticket(client=None, uid="u2", ctx="c")
        return t1, t2, t3

    t1, t2, t3 = asyncio.run(run())
    assert t1 == "ticket-u1"
    assert t2 == "ticket-u1"   # same ticket served from cache
    assert t3 == "ticket-u2"
    assert calls == ["u1", "u2"]  # u1 only fetched once, u2 fetched fresh


def test_typing_ticket_expiry_triggers_refetch(tmp_path, monkeypatch):
    bot = WechatBot(
        runtime=_fake_runtime(tmp_path),
        base_url="http://x",
        token="tok",
        typing_ticket_ttl=10.0,
    )

    counter = {"n": 0}

    async def fake_get_typing_ticket(client, base_url, token, uid, ctx=None):
        counter["n"] += 1
        return f"ticket-{counter['n']}"

    monkeypatch.setattr(bot_module, "get_typing_ticket", fake_get_typing_ticket)

    t_clock = {"now": 1000.0}

    def fake_monotonic() -> float:
        return t_clock["now"]

    monkeypatch.setattr(bot_module.time, "monotonic", fake_monotonic)

    async def run():
        first = await bot._resolve_typing_ticket(client=None, uid="u1", ctx="c")
        # Within TTL → cache hit.
        t_clock["now"] = 1005.0
        cached = await bot._resolve_typing_ticket(client=None, uid="u1", ctx="c")
        # Past TTL → refetch.
        t_clock["now"] = 1011.0
        refreshed = await bot._resolve_typing_ticket(client=None, uid="u1", ctx="c")
        return first, cached, refreshed

    first, cached, refreshed = asyncio.run(run())
    assert first == "ticket-1"
    assert cached == "ticket-1"
    assert refreshed == "ticket-2"
    assert counter["n"] == 2


def test_empty_ticket_not_cached(tmp_path, monkeypatch):
    """Server returning an empty ticket should not poison the cache.

    Otherwise the next caller would hit a stale empty entry until TTL.
    """
    bot = WechatBot(runtime=_fake_runtime(tmp_path), base_url="http://x", token="tok")

    responses = iter(["", "real-ticket"])

    async def fake_get_typing_ticket(client, base_url, token, uid, ctx=None):
        return next(responses)

    monkeypatch.setattr(bot_module, "get_typing_ticket", fake_get_typing_ticket)

    async def run():
        first = await bot._resolve_typing_ticket(client=None, uid="u1", ctx="c")
        second = await bot._resolve_typing_ticket(client=None, uid="u1", ctx="c")
        return first, second

    first, second = asyncio.run(run())
    assert first == ""
    assert second == "real-ticket"
    assert bot._typing_ticket_cache["u1"][0] == "real-ticket"
