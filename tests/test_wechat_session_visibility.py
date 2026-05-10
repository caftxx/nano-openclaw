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

from nano_openclaw.gateway.backend_embedded import EmbeddedBackend
from nano_openclaw.gateway.run_registry import RunRegistry
from nano_openclaw.gateway.runtime_lock import RuntimeUpdateGuard
from nano_openclaw.loop import LoopConfig, Message
from nano_openclaw.tools import ToolRegistry
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
        runtime=runtime,
        base_url="https://example",
        token="t",
        session_manager=backend.manager,
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
        runtime=runtime2,
        base_url="https://example",
        token="t",
        session_manager=backend2.manager,
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


# ────────────────────────────────────────────────────────────────────────────
# Legacy fallback when session_manager is None
# ────────────────────────────────────────────────────────────────────────────


def test_resolve_session_returns_none_when_no_manager(tmp_path):
    """Bot without ``session_manager`` falls back to legacy in-memory dict —
    _resolve_session returns None, and _get_or_create_history still works.
    """
    runtime = _fake_runtime(tmp_path)
    bot = WechatBot(
        runtime=runtime,
        base_url="https://example",
        token="t",
        # no session_manager / uid_map_path → legacy
    )
    assert bot._resolve_session("anyone") is None
    history = bot._get_or_create_history("anyone")
    assert history == []
