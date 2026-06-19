"""Tests for the gateway self-restart machinery.

Covers the restart primitive (both ``exec`` and ``exit`` strategies), the
``Backend.gateway_restart`` RPC, the ``restart`` LLM tool's deferred trigger,
and the ApprovalPolicy gating that keeps cron / channel auto-runs from
restarting the daemon without user opt-in.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nano_openclaw.approvals.types import ApprovalPolicy
from nano_openclaw.gateway import restart as restart_mod
from nano_openclaw.services.backend_embedded import EmbeddedBackend
from nano_openclaw.gateway.methods import CORE_HANDLERS
from nano_openclaw.gateway.protocol import METHODS_V1
from nano_openclaw.services.runs import RunRegistry
from nano_openclaw.services.runtime_update import RuntimeUpdateGuard
from nano_openclaw.gateway.slash import _HANDLERS as SLASH_HANDLERS
from nano_openclaw.core.loop import CancellationToken, LoopConfig
from nano_openclaw.core.tools import ToolRegistry


def _fake_runtime(tmp_path: Path, *, restart_strategy: str = "exec") -> SimpleNamespace:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    store_path = tmp_path / "sessions.json"
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    cfg = LoopConfig(model="test-model", workspace_dir=workspace_dir, session_key="default")
    registry = ToolRegistry()
    config = SimpleNamespace(gateway=SimpleNamespace(restart_strategy=restart_strategy))

    return SimpleNamespace(
        agent_id="default",
        session_id="default",
        config=config,
        warnings=[],
        client=None,
        registry=registry,
        cfg=cfg,
        hook_registry=None,
        state_dir=state_dir,
        session_dir=session_dir,
        store_path=store_path,
        workspace_dir=workspace_dir,
        model_ref="test/test-model",
        model_id="test-model",
        image_model_ref=None,
        run_registry=RunRegistry(),
        runtime_guard=RuntimeUpdateGuard(),
        pending_restart=False,
    )


# ─── Restart primitive ───

def test_perform_restart_exec_calls_execv_with_argv(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_execv(path, args):
        captured["path"] = path
        captured["args"] = list(args)

    monkeypatch.setattr(restart_mod.os, "execv", fake_execv)
    # Tweak sys.argv to a deterministic value.
    monkeypatch.setattr(sys, "argv", ["original-argv-0", "gateway", "run"])

    restart_mod.perform_restart("exec")

    assert captured["path"] == sys.executable
    # First slot is replaced with sys.executable; rest mirrors sys.argv.
    assert captured["args"][0] == sys.executable
    assert captured["args"][1:] == ["original-argv-0", "gateway", "run"]


def test_perform_restart_exit_calls_underscore_exit(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_exit(code):
        captured["code"] = code
        # raise so test stops here (real _exit is no-return)
        raise SystemExit(code)

    monkeypatch.setattr(restart_mod.os, "_exit", fake_exit)

    with pytest.raises(SystemExit):
        restart_mod.perform_restart("exit")

    assert captured["code"] == 0


# ─── RPC / Backend ───

def test_gateway_restart_in_method_catalog():
    assert "gateway.restart" in METHODS_V1
    assert "gateway.restart" in CORE_HANDLERS


def test_embedded_backend_gateway_restart_schedules_call_later(monkeypatch, tmp_path):
    """The handler returns synchronously and arms a delayed restart."""
    runtime = _fake_runtime(tmp_path, restart_strategy="exit")
    backend = EmbeddedBackend(runtime)

    scheduled: list[tuple[float, Any, tuple]] = []

    class _FakeLoop:
        def call_later(self, delay, fn, *args):
            scheduled.append((delay, fn, args))

            class _Handle:
                def cancel(self_inner):
                    pass

            return _Handle()

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _FakeLoop())

    async def run():
        return await backend.gateway_restart()

    info = asyncio.run(run())

    assert info["strategy"] == "exit"
    assert isinstance(info["pid"], int)
    assert len(scheduled) == 1
    delay, fn, args = scheduled[0]
    assert 0.0 < delay <= 1.0
    assert fn is restart_mod.perform_restart
    assert args == ("exit",)


# ─── Slash command ───

def test_restart_slash_registered():
    assert "/restart" in SLASH_HANDLERS


def test_restart_slash_calls_backend_gateway_restart():
    """Direct-call test against the underlying handler. ``_cmd_restart`` now
    expects a ``SlashRenderer``-shaped collaborator; we supply a tiny capture
    object with the methods the handler actually invokes (``warning`` /
    ``dim`` / ``error``).
    """
    calls: list[str] = []

    class _Backend:
        async def gateway_restart(self):
            calls.append("called")
            return {"strategy": "exec", "pid": 4242}

    class _CaptureRenderer:
        def __init__(self):
            self.lines: list[str] = []

        def warning(self, s): self.lines.append(s)
        def dim(self, s): self.lines.append(s)
        def error(self, s): self.lines.append(s)

    handler = SLASH_HANDLERS["/restart"]
    renderer = _CaptureRenderer()

    async def run():
        await handler(_Backend(), renderer, {}, [], None)

    asyncio.run(run())
    assert calls == ["called"]
    # Sanity: the user sees a confirmation, not a silent restart.
    assert any("restart" in line.lower() for line in renderer.lines)


# ─── LLM tool ───

def test_restart_tool_registered_after_runtime_build(tmp_path):
    """``_register_restart_tool`` (invoked from build_agent_runtime) wires
    a ``restart`` Tool into the ToolRegistry and binds it to the runtime."""
    from nano_openclaw.core.runtime import _register_restart_tool

    runtime = _fake_runtime(tmp_path)
    _register_restart_tool(runtime)
    assert "restart" in runtime.registry.names()


def test_restart_tool_sets_pending_flag_and_schedules_watcher(monkeypatch, tmp_path):
    """Calling the tool flips ``runtime.pending_restart`` and arms one
    watcher task. A second call is a no-op (no stacked watchers)."""
    from nano_openclaw.core.runtime import _register_restart_tool

    runtime = _fake_runtime(tmp_path, restart_strategy="exec")
    _register_restart_tool(runtime)

    created_tasks: list[Any] = []

    async def run_test():
        # Patch on the running loop so the test doesn't actually exec.
        loop = asyncio.get_running_loop()
        original_create_task = loop.create_task

        def tracked_create_task(coro, *args, **kwargs):
            task = original_create_task(coro, *args, **kwargs)
            created_tasks.append(task)
            return task

        monkeypatch.setattr(loop, "create_task", tracked_create_task)

        tool = runtime.registry.get("restart")
        assert tool is not None
        first = tool.run({})
        assert runtime.pending_restart is True
        assert "scheduled" in first.lower()
        assert len(created_tasks) == 1

        second = tool.run({})
        assert "already pending" in second.lower()
        assert len(created_tasks) == 1  # no new watcher

        # Cancel the watcher so the test event loop can shut down cleanly.
        for task in created_tasks:
            task.cancel()
        for task in created_tasks:
            with pytest.raises((asyncio.CancelledError, BaseException)):
                await task

    asyncio.run(run_test())


def test_restart_watcher_only_fires_when_registry_drains(monkeypatch, tmp_path):
    """The deferred watcher must wait for the run_registry to empty before
    invoking ``perform_restart`` — otherwise it would kill the calling
    turn mid-execution."""
    from nano_openclaw.core.runtime import _register_restart_tool

    runtime = _fake_runtime(tmp_path)
    _register_restart_tool(runtime)

    fire_count = 0

    def fake_perform_restart(strategy):
        nonlocal fire_count
        fire_count += 1

    monkeypatch.setattr(restart_mod, "perform_restart", fake_perform_restart)

    # Pre-register a fake in-flight run so the watcher initially blocks.
    runtime.run_registry.register(
        turn_id="turn-1",
        origin="chat",
        cancellation_token=CancellationToken(),
    )

    async def run_test():
        tool = runtime.registry.get("restart")
        tool.run({})  # arms the watcher

        # Watcher polls every 200ms; give it a couple of cycles to confirm
        # it does NOT fire while a run is registered.
        await asyncio.sleep(0.5)
        assert fire_count == 0
        assert runtime.pending_restart is True

        # Drain the registry — watcher should fire shortly after.
        runtime.run_registry.unregister("turn-1")
        # Wait up to ~2s for the watcher's poll + final 0.2s flush.
        for _ in range(20):
            if fire_count > 0:
                break
            await asyncio.sleep(0.1)
        assert fire_count == 1

    asyncio.run(run_test())


# ─── Approval gating ───

def test_restart_in_default_approval_policy():
    """ApprovalPolicy ships with ``restart`` always-approval so cron /
    channel non-interactive turns can't silently restart the daemon."""
    policy = ApprovalPolicy()
    assert "restart" in policy.dangerous_tools
    cfg = policy.tool_configs.get("restart")
    assert cfg is not None
    assert cfg.requires_approval is True
