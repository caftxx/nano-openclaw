"""Verify that wechat per-uid sessions are visible to ``/sessions`` (TUI/WebUI).

Phase 9 fix: WechatBot now resolves each uid via ``BackendSessionManager``
instead of using its own in-memory ``_sessions`` dict. The uid → session_id
mapping persists at ``state_dir/wechat-sessions.{account}.json`` so daemon
restarts don't fork fresh empty sessions on every contact.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nano_openclaw.services.backend_embedded import EmbeddedBackend
from nano_openclaw.services.channels import ChannelManager
from nano_openclaw.services.runs import RunRegistry
from nano_openclaw.services.runtime_update import RuntimeUpdateGuard
from nano_openclaw.core.loop import LoopConfig, Message
from nano_openclaw.core.provider import MessageEnd, TextDelta, ToolUseDelta, ToolUseEnd, ToolUseStart
from nano_openclaw.core.tools import Tool, ToolRegistry
from nano_openclaw.adapters.channels.base import ChannelAccount
from nano_openclaw.adapters.channels.wechat import WechatChannel
from nano_openclaw.wechat import bot as bot_module
from nano_openclaw.wechat.bot import WechatBot


def _fake_runtime(tmp_path: Path) -> SimpleNamespace:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    cfg = LoopConfig(model="test-model", workspace_dir=workspace, session_key="default")
    return SimpleNamespace(
        agent_id="default",
        session_id="default",
        config=SimpleNamespace(),
        warnings=[],
        client=None,
        registry=ToolRegistry(),
        cfg=cfg,
        hook_registry=None,
        state_dir=state,
        session_dir=sd,
        store_path=tmp_path / "sessions.json",
        workspace_dir=workspace,
        model_ref="test/test-model",
        model_id="test-model",
        image_model_ref=None,
        run_registry=RunRegistry(),
        runtime_guard=RuntimeUpdateGuard(),
    )


def _bot_with_manager(tmp_path: Path, uid_map_path: Path | None = None) -> tuple[WechatBot, EmbeddedBackend]:
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    bot = WechatBot(
        base_url="https://example",
        token="t",
        session_manager=backend.manager,
        backend=backend,
        uid_map_path=uid_map_path or (tmp_path / "wechat-sessions.json"),
        account_id="default",
    )
    return bot, backend


# ────────────────────────────────────────────────────────────────────────────
# uid → session_id mapping
# ────────────────────────────────────────────────────────────────────────────


def test_first_contact_creates_session_and_persists_mapping(tmp_path):
    map_path = tmp_path / "wechat-sessions.json"
    bot, backend = _bot_with_manager(tmp_path, uid_map_path=map_path)
    try:
        sess = bot._resolve_session("user-abc")
        assert sess is not None
        # Mapping persisted to disk
        assert map_path.exists()
        loaded = json.loads(map_path.read_text())
        assert loaded == {"user-abc": sess.session_id}
    finally:
        asyncio.run(backend.aclose())


def test_subsequent_contact_reuses_same_session(tmp_path):
    bot, backend = _bot_with_manager(tmp_path)
    try:
        s1 = bot._resolve_session("user-x")
        s2 = bot._resolve_session("user-x")
        assert s1 is s2
        assert s1.session_id == s2.session_id
    finally:
        asyncio.run(backend.aclose())


def test_different_uids_get_distinct_sessions(tmp_path):
    bot, backend = _bot_with_manager(tmp_path)
    try:
        s1 = bot._resolve_session("user-x")
        s2 = bot._resolve_session("user-y")
        assert s1.session_id != s2.session_id
    finally:
        asyncio.run(backend.aclose())


def test_mapping_survives_bot_restart(tmp_path):
    """Daemon restart simulates: new WechatBot instance, same uid_map_path,
    same backend manager (sessions on disk). The uid → session lookup
    should find the original session, not create a new one.
    """
    map_path = tmp_path / "wechat-sessions.json"

    # First "daemon run" — create a session for the uid and persist a message
    bot1, backend1 = _bot_with_manager(tmp_path, uid_map_path=map_path)
    try:
        s1 = bot1._resolve_session("alice")
        # Persist so the session reaches disk (manager.save_metadata gates on
        # transcript-file existence, which only flushes after first append)
        msg = Message("user", [{"type": "text", "text": "hello"}])
        s1.history.append(msg)
        s1.writer.append_message(msg)
        backend1.manager.save_metadata(s1)
        original_session_id = s1.session_id
    finally:
        asyncio.run(backend1.aclose())

    # Second "daemon run" — fresh manager + bot, same map file
    runtime2 = _fake_runtime(tmp_path)
    # Reuse the same disk paths
    runtime2.session_dir = tmp_path / "sessions"
    runtime2.store_path = tmp_path / "sessions.json"
    backend2 = EmbeddedBackend(runtime2)
    bot2 = WechatBot(
        base_url="https://example",
        token="t",
        session_manager=backend2.manager,
        backend=backend2,
        uid_map_path=map_path,
        account_id="default",
    )
    try:
        s2 = bot2._resolve_session("alice")
        assert s2.session_id == original_session_id
        # And the message we wrote is still there
        assert len(s2.history) == 1
    finally:
        asyncio.run(backend2.aclose())


def test_stale_mapping_falls_back_to_new_session(tmp_path):
    """If the persisted session_id no longer exists on disk (e.g., user
    deleted the transcript externally), bot should not crash — instead
    create a fresh session and update the mapping.
    """
    map_path = tmp_path / "wechat-sessions.json"
    map_path.write_text(json.dumps({"ghost-uid": "deadbeef-not-a-real-session"}))

    bot, backend = _bot_with_manager(tmp_path, uid_map_path=map_path)
    try:
        sess = bot._resolve_session("ghost-uid")
        assert sess is not None
        assert sess.session_id != "deadbeef-not-a-real-session"
        # Mapping was updated in memory; persistence happened
        loaded = json.loads(map_path.read_text())
        assert loaded["ghost-uid"] == sess.session_id
    finally:
        asyncio.run(backend.aclose())


# ────────────────────────────────────────────────────────────────────────────
# Visibility — wechat session shows up in /sessions
# ────────────────────────────────────────────────────────────────────────────


def test_wechat_session_appears_in_sessions_list(tmp_path):
    """After a wechat user has had at least one message, the session must
    appear in ``backend.sessions_list()`` — the same call that powers /sessions.
    """
    bot, backend = _bot_with_manager(tmp_path)

    async def run():
        try:
            sess = bot._resolve_session("user-1")
            msg = Message("user", [{"type": "text", "text": "hi"}])
            sess.history.append(msg)
            sess.writer.append_message(msg)
            backend.manager.save_metadata(sess)
            return await backend.sessions_list(), sess.session_id
        finally:
            await backend.aclose()

    result, expected_id = asyncio.run(run())
    assert any(s.session_id == expected_id for s in result.sessions), (
        f"wechat session {expected_id[:8]} not visible to /sessions; "
        f"got: {[s.session_id[:8] for s in result.sessions]}"
    )


def test_wechat_message_routes_through_backend_service(tmp_path, monkeypatch):
    runtime = _fake_runtime(tmp_path)
    embedded = EmbeddedBackend(runtime)
    sent: list[tuple[str, str, str | None]] = []

    async def fake_get_typing_ticket(*_args, **_kwargs):
        return ""

    async def fake_send_text(_client, _base_url, _token, to_user, text, ctx=None):
        sent.append((to_user, text, ctx))

    monkeypatch.setattr(bot_module, "get_typing_ticket", fake_get_typing_ticket)
    monkeypatch.setattr(bot_module, "send_text", fake_send_text)

    class RecordingBackend:
        def __init__(self):
            self.manager = embedded.manager
            self.calls: list[dict[str, Any]] = []

        async def chat_send(self, **kwargs):
            self.calls.append(kwargs)
            kwargs["on_local_event"](TextDelta(text="backend reply"))
            return "turn-1"

        async def await_turn(self, turn_id: str) -> None:
            assert turn_id == "turn-1"

    backend = RecordingBackend()
    bot = WechatBot(
        base_url="https://example",
        token="t",
        session_manager=embedded.manager,
        backend=backend,
        uid_map_path=tmp_path / "wechat-sessions.json",
        account_id="work",
    )
    msg = {
        "from_user_id": "user-1",
        "context_token": "ctx-1",
        "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
    }

    async def run():
        try:
            await bot._handle_message(msg)
        finally:
            await embedded.aclose()

    asyncio.run(run())

    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["text"] == "hello"
    assert call["turn_source"] == "wechat"
    assert call["channel_id"] == "wechat"
    assert call["channel_account_id"] == "work"
    assert call["channel_sender_key"] == "user-1"
    assert sent == [("user-1", "backend reply", "ctx-1")]


def test_wechat_message_uses_embedded_backend_channel_decoration(tmp_path, monkeypatch):
    runtime = _fake_runtime(tmp_path)
    cron_args: list[dict[str, Any]] = []
    sent: list[str] = []

    runtime.registry.register(Tool(
        name="cron_create",
        description="create cron",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        run=lambda args: cron_args.append(dict(args)) or "created",
    ))

    channels = ChannelManager()
    channels._instances[("wechat", "work")] = WechatChannel(ChannelAccount(id="work"))
    backend = EmbeddedBackend(runtime, channel_manager=channels)

    async def fake_get_typing_ticket(*_args, **_kwargs):
        return ""

    async def fake_send_text(_client, _base_url, _token, _to_user, text, ctx=None):
        sent.append(text)

    call_count = {"n": 0}

    async def fake_stream_response(**_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            yield ToolUseStart(id="tool-1", name="cron_create")
            yield ToolUseDelta(id="tool-1", partial_json=json.dumps({"name": "daily"}))
            yield ToolUseEnd(id="tool-1")
            yield MessageEnd(stop_reason="tool_use", usage={})
            return
        yield TextDelta(text="scheduled")
        yield MessageEnd(stop_reason="end_turn", usage={})

    monkeypatch.setattr(bot_module, "get_typing_ticket", fake_get_typing_ticket)
    monkeypatch.setattr(bot_module, "send_text", fake_send_text)
    monkeypatch.setattr("nano_openclaw.core.loop.stream_response", fake_stream_response)

    async def async_dispatch(self, tool_use_id, name, args, cancellation_token=None, context=None):
        tool = self.get(name)
        assert tool is not None
        result = tool.run(args)
        output = await result if asyncio.iscoroutine(result) else result
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": [{"type": "text", "text": str(output)}],
        }

    monkeypatch.setattr(ToolRegistry, "dispatch", async_dispatch)

    bot = WechatBot(
        base_url="https://example",
        token="t",
        session_manager=backend.manager,
        backend=backend,
        uid_map_path=tmp_path / "wechat-sessions.json",
        account_id="work",
    )
    msg = {
        "from_user_id": "user-1",
        "context_token": "ctx-1",
        "item_list": [{"type": 1, "text_item": {"text": "schedule it"}}],
    }

    async def run():
        try:
            await bot._handle_message(msg)
            sessions = await backend.sessions_list()
            bot._ensure_uid_map_loaded()
            return sessions, bot._uid_to_session_id["user-1"]
        finally:
            await backend.aclose()

    sessions, session_id = asyncio.run(run())

    assert cron_args == [{
        "name": "daily",
        "created_by": "wechat:work:user-1",
        "notify_wechat": True,
    }]
    assert sent == ["scheduled"]
    listed = [s for s in sessions.sessions if s.session_id == session_id]
    assert listed
    assert listed[0].message_count >= 4


# ────────────────────────────────────────────────────────────────────────────
# Missing service wiring
# ────────────────────────────────────────────────────────────────────────────


def test_resolve_session_returns_none_when_no_manager():
    """Low-level bot tests may omit the manager; production channel startup wires it."""
    bot = WechatBot(
        base_url="https://example",
        token="t",
    )
    assert bot._resolve_session("anyone") is None
    with pytest.raises(RuntimeError, match="BackendSessionManager"):
        bot._get_or_create_history("anyone")


def test_slash_without_backend_is_rejected_without_mutating_session(tmp_path):
    backend_runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(backend_runtime)
    bot = WechatBot(
        base_url="https://example",
        token="t",
        session_manager=backend.manager,
    )
    try:
        sess = bot._resolve_session("anyone")
        assert sess is not None
        sess.history.append(Message("user", [{"type": "text", "text": "hello"}]))

        reply = asyncio.run(bot._handle_slash_command("anyone", "/clear"))

        assert "daemon backend" in (reply or "")
        assert len(sess.history) == 1
    finally:
        asyncio.run(backend.aclose())


def test_slash_without_backend_does_not_raise_attribute_error(tmp_path):
    bot = WechatBot(
        base_url="https://example",
        token="t",
    )

    help_reply = asyncio.run(bot._handle_slash_command("anyone", "/help"))
    tools_reply = asyncio.run(bot._handle_slash_command("anyone", "/tools"))

    assert "daemon backend" in (help_reply or "")
    assert "daemon backend" in (tools_reply or "")
    assert "AttributeError" not in help_reply
    assert "AttributeError" not in tools_reply
