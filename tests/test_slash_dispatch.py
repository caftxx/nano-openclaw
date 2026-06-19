"""Shared slash dispatcher (services/slash.py) tests.

Verifies that:

1. Each command routes through Backend RPCs and renders something to the
   console (the actual Rich layout isn't asserted — that's UI churn).
2. ``state["session_changed"]`` is set when the slash mutates which session
   the next ``chat.send`` should target — caller can then rebind locals.
3. ``sessions_delete`` actually removes the on-disk transcript + store entry.
4. Enriched ``skills.list`` / ``plugins.list`` / ``hooks.list`` payloads
   don't break the renderers when fields are sparse / empty.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from nano_openclaw.services.channels import ChannelManager
from nano_openclaw.adapters.channels.base import ChannelAdapter
from nano_openclaw.services.backend import NotFoundError
from nano_openclaw.services.backend_embedded import EmbeddedBackend
from nano_openclaw.api.context import GatewayContext
from nano_openclaw.services.runs import RunRegistry
from nano_openclaw.services.runtime_update import RuntimeUpdateGuard
from nano_openclaw.services.slash import HELP_TEXT, QuitREPL, handle_slash
from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.core.tools import Tool, ToolRegistry


class _RecordingChannel(ChannelAdapter):
    id = "recording"

    async def start(self, runtime, gateway=None):
        self._state = "running"

    async def stop(self):
        self._state = "stopped"


def _registry_with_tool() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(name="bash", description="run shell command", input_schema={}, run=lambda a: ""))
    return reg


def _fake_runtime(tmp_path: Path, *, registry: ToolRegistry | None = None) -> SimpleNamespace:
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
        registry=registry or ToolRegistry(),
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


def _backend_and_console(
    tmp_path: Path,
    *,
    registry: ToolRegistry | None = None,
    channel_manager: ChannelManager | None = None,
) -> tuple[EmbeddedBackend, Console, io.StringIO]:
    runtime = _fake_runtime(tmp_path, registry=registry)
    backend = EmbeddedBackend(runtime, channel_manager=channel_manager)
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=200, no_color=True)
    return backend, console, buf


# ────────────────────────────────────────────────────────────────────────────
# Help + unknown commands
# ────────────────────────────────────────────────────────────────────────────


def test_quit_raises_quit_repl(tmp_path):
    backend, console, _ = _backend_and_console(tmp_path)

    async def run():
        try:
            with pytest.raises(QuitREPL):
                await handle_slash("/quit", backend, console, {"session_key": ""})
            with pytest.raises(QuitREPL):
                await handle_slash("/exit", backend, console, {"session_key": ""})
            with pytest.raises(QuitREPL):
                await handle_slash("/q", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_help_prints_help_text(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        try:
            handled = await handle_slash("/help", backend, console, {"session_key": ""})
            assert handled is True
        finally:
            await backend.aclose()

    asyncio.run(run())
    out = buf.getvalue()
    # Must list the major commands so users can discover what's available.
    for cmd in ("/clear", "/new", "/sessions", "/skills", "/tools", "/active-memory"):
        assert cmd in out


def test_non_slash_returns_false(tmp_path):
    backend, console, _ = _backend_and_console(tmp_path)

    async def run():
        try:
            return await handle_slash("hello agent", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    assert asyncio.run(run()) is False


def test_unknown_slash_returns_false(tmp_path):
    backend, console, _ = _backend_and_console(tmp_path)

    async def run():
        try:
            return await handle_slash("/totally-unknown", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    assert asyncio.run(run()) is False


# ────────────────────────────────────────────────────────────────────────────
# Introspection — Tables render without crashing
# ────────────────────────────────────────────────────────────────────────────


def test_tools_command_renders_table(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path, registry=_registry_with_tool())

    async def run():
        try:
            await handle_slash("/tools", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    asyncio.run(run())
    out = buf.getvalue()
    assert "bash" in out
    assert "Tools" in out  # table title


def test_skills_handles_empty_workspace_gracefully(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        try:
            await handle_slash("/skills", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    asyncio.run(run())
    out = buf.getvalue()
    # Either the empty-state hint or an empty Table — never raises
    assert "skill" in out.lower() or "no skills" in out.lower()


def test_plugins_renders_no_op_when_no_plugins(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        try:
            await handle_slash("/plugins", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    asyncio.run(run())
    assert "no plugins loaded" in buf.getvalue().lower()


def test_hooks_renders_no_op_when_no_hooks(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        try:
            await handle_slash("/hooks", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    asyncio.run(run())
    assert "no hooks registered" in buf.getvalue().lower()


# ────────────────────────────────────────────────────────────────────────────
# Session lifecycle — state mutation
# ────────────────────────────────────────────────────────────────────────────


def test_clear_marks_session_changed(tmp_path):
    backend, console, _ = _backend_and_console(tmp_path)

    async def run():
        # Seed a session so /clear has something to act on
        sess = backend.manager.create()
        state = {"session_key": sess.session_id, "session_changed": False}
        try:
            await handle_slash("/clear", backend, console, state)
            return state
        finally:
            await backend.aclose()

    state = asyncio.run(run())
    assert state["session_changed"] is True


def test_new_creates_session_and_updates_session_key(tmp_path):
    backend, console, _ = _backend_and_console(tmp_path)

    async def run():
        state: dict = {"session_key": "old", "session_changed": False}
        try:
            await handle_slash("/new", backend, console, state)
            return state
        finally:
            await backend.aclose()

    state = asyncio.run(run())
    assert state["session_changed"] is True
    assert state["session_key"] != "old"
    assert state["session_key"]  # non-empty


def test_channels_renders_running_channel(tmp_path):
    channel_manager = ChannelManager()
    channel_manager.register(_RecordingChannel)
    backend, console, buf = _backend_and_console(tmp_path, channel_manager=channel_manager)

    async def run():
        try:
            await backend.channels_start("recording", "work")
            await handle_slash("/channels", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    asyncio.run(run())
    out = buf.getvalue()
    assert "recording/work" in out
    assert "running" in out


def test_session_switch_by_index_updates_state(tmp_path):
    backend, console, _ = _backend_and_console(tmp_path)

    async def run():
        from nano_openclaw.core.loop import Message
        # Seed two sessions so /sessions list returns them. Need a message
        # each — sessions_list filters out empty sessions.
        s1 = backend.manager.create()
        m1 = Message("user", [{"type": "text", "text": "first"}])
        s1.history.append(m1)
        s1.writer.append_message(m1)
        backend.manager.save_metadata(s1)
        s2 = backend.manager.create()
        m2 = Message("user", [{"type": "text", "text": "second"}])
        s2.history.append(m2)
        s2.writer.append_message(m2)
        backend.manager.save_metadata(s2)

        state = {"session_key": s1.session_id, "session_changed": False}
        try:
            await handle_slash("/session 1", backend, console, state)
            return state, {s1.session_id, s2.session_id}
        finally:
            await backend.aclose()

    state, known_ids = asyncio.run(run())
    assert state["session_key"] in known_ids


# ────────────────────────────────────────────────────────────────────────────
# /sessions delete
# ────────────────────────────────────────────────────────────────────────────


def test_sessions_delete_removes_transcript_file(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        from nano_openclaw.core.loop import Message
        # Create a session with at least one message so sessions_list surfaces
        # it (the manager filters out empty sessions).
        sess = backend.manager.create()
        msg = Message("user", [{"type": "text", "text": "hello"}])
        sess.history.append(msg)
        sess.writer.append_message(msg)
        backend.manager.save_metadata(sess)
        transcript_path = sess.transcript_path
        assert transcript_path.exists()

        state = {"session_key": "", "session_changed": False}
        try:
            await handle_slash(
                f"/sessions delete {sess.session_id[:8]}",
                backend,
                console,
                state,
            )
        finally:
            await backend.aclose()
        return transcript_path, buf.getvalue()

    transcript_path, output = asyncio.run(run())
    assert not transcript_path.exists(), output


def test_sessions_delete_active_session_returns_busy(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        from nano_openclaw.core.loop import Message
        sess = backend.manager.create()
        msg = Message("user", [{"type": "text", "text": "hi"}])
        sess.history.append(msg)
        sess.writer.append_message(msg)
        backend.manager.save_metadata(sess)
        sess.active_turn_id = "fake-turn"
        try:
            # Slash command catches BusyError and prints; doesn't propagate
            await handle_slash(
                f"/sessions delete {sess.session_id}",
                backend, console, {"session_key": sess.session_id},
            )
            return sess.transcript_path.exists()
        finally:
            sess.active_turn_id = None
            await backend.aclose()

    assert asyncio.run(run()) is True  # transcript NOT deleted


def test_sessions_delete_unknown_id_prints_error(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        try:
            await handle_slash(
                "/sessions delete deadbeef",
                backend, console, {"session_key": ""},
            )
        finally:
            await backend.aclose()

    asyncio.run(run())
    assert "no session" in buf.getvalue().lower()


# ────────────────────────────────────────────────────────────────────────────
# Daemon-introspection passthroughs
# ────────────────────────────────────────────────────────────────────────────


def test_health_renders_summary(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        try:
            await handle_slash("/health", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    asyncio.run(run())
    assert "runtime_ready" in buf.getvalue()


def test_runtime_renders_summary(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        try:
            await handle_slash("/runtime", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    asyncio.run(run())
    out = buf.getvalue()
    assert "agent=default" in out
    assert "model=test-model" in out


def test_active_memory_status_renders_panel(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        try:
            await handle_slash("/active-memory", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    asyncio.run(run())
    out = buf.getvalue()
    assert "Active Memory" in out


def test_dreaming_status_renders_panel(tmp_path):
    backend, console, buf = _backend_and_console(tmp_path)

    async def run():
        try:
            await handle_slash("/dreaming", backend, console, {"session_key": ""})
        finally:
            await backend.aclose()

    asyncio.run(run())
    out = buf.getvalue()
    assert "Dreaming" in out
