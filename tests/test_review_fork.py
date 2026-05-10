"""Tests for the Background Review Fork plugin."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from nano_openclaw.plugins.builtin.review_fork_plugin import (
    REVIEW_FORK_ALLOWLIST,
    ReviewForkConfig,
    ReviewForkPlugin,
    ReviewForkState,
    build_review_fork_registry,
    get_state,
    reset_state,
)
from nano_openclaw.subagent.types import SubagentRunRecord, SubagentStatus
from nano_openclaw.tools import ToolRegistry, build_core_registry


def _make_payload(
    *,
    stop_reason: str = "end_turn",
    session_key: str = "agent:default:session-abc",
    workspace_dir: str = "/tmp/wsp",
    tool_registry: ToolRegistry | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if tool_registry is None:
        tool_registry = build_core_registry()
    return {
        "session_id": session_key.split(":")[-1],
        "agent_id": "default",
        "session_key": session_key,
        "session_dir": "/tmp/sessions",
        "transcript_path": "/tmp/sessions/x.jsonl",
        "workspace_dir": workspace_dir,
        "stop_reason": stop_reason,
        "iteration_count": 1,
        "tools_used": [],
        "messages_snapshot": messages or [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ],
        "user_input": "hi",
        "client": object(),
        "loop_config": object(),
        "tool_registry": tool_registry,
    }


class _FakeRunner:
    """Stand-in SubagentRunner that records spawn calls."""

    def __init__(self) -> None:
        self.spawn_calls: list[dict[str, Any]] = []
        self._can_spawn = True

    def can_spawn(self, _requester_session_key: str) -> bool:
        return self._can_spawn

    def spawn(self, params, requester_session_key, **kwargs) -> SubagentRunRecord:
        self.spawn_calls.append({
            "params": params,
            "requester_session_key": requester_session_key,
            "kwargs": kwargs,
        })
        return SubagentRunRecord(
            run_id=f"r{len(self.spawn_calls)}",
            child_session_key=f"agent:default:subagent:run{len(self.spawn_calls)}",
            requester_session_key=requester_session_key,
            task=params.task,
            label=params.label,
            model=params.model,
            status=SubagentStatus.PENDING,
        )


@pytest.fixture(autouse=True)
def _reset_state():
    reset_state()
    yield
    reset_state()


@pytest.fixture
def fake_runner(monkeypatch) -> _FakeRunner:
    runner = _FakeRunner()

    def _get_runner(*args, **kwargs):
        return runner

    monkeypatch.setattr(
        "nano_openclaw.subagent.runner.get_runner",
        _get_runner,
    )
    return runner


def test_trigger_cadence_every_n_with_cooldown(fake_runner):
    """Trigger N=10: spawn fires on the 10th end_turn, then cools down."""
    cfg = ReviewForkConfig(enabled=True, trigger_n=10, cooldown_s=60)
    state = ReviewForkState(cfg)

    async def go():
        for _ in range(10):
            await state.maybe_fork(_make_payload())
        # 10th end_turn → 1 spawn
        assert len(fake_runner.spawn_calls) == 1
        # 11th end_turn — cooldown is active → no spawn
        await state.maybe_fork(_make_payload())
        assert len(fake_runner.spawn_calls) == 1

    asyncio.run(go())


def test_stop_reason_filter_skips_max_iter(fake_runner):
    cfg = ReviewForkConfig(enabled=True, trigger_n=1, cooldown_s=0)
    state = ReviewForkState(cfg)

    async def go():
        for _ in range(50):
            await state.maybe_fork(_make_payload(stop_reason="max_iter"))
        assert fake_runner.spawn_calls == []
        # And denial too
        for _ in range(50):
            await state.maybe_fork(_make_payload(stop_reason="denial"))
        assert fake_runner.spawn_calls == []

    asyncio.run(go())


def test_recursive_skip_when_session_is_subagent(fake_runner):
    cfg = ReviewForkConfig(enabled=True, trigger_n=1, cooldown_s=0)
    state = ReviewForkState(cfg)
    sub_session_key = "agent:default:subagent:abc123"

    async def go():
        for _ in range(20):
            await state.maybe_fork(_make_payload(session_key=sub_session_key))
        assert fake_runner.spawn_calls == []

    asyncio.run(go())


def test_disabled_plugin_registers_hook_but_does_not_spawn(monkeypatch, fake_runner):
    """Plugin always registers (so runtime set enabled=True works), but
    ``maybe_fork`` early-returns when cfg.enabled is False."""
    from dataclasses import dataclass

    registered: list[tuple[str, Any]] = []

    @dataclass
    class _StubApi:
        plugin_config: dict
        config: Any = None

        def register_hook(self, event, handler, priority=0):
            registered.append((event, handler))

        def register_tool(self, tool):
            pass

    plugin = ReviewForkPlugin()
    api = _StubApi(plugin_config={"enabled": False})
    plugin.register(api)
    # Hook + state are registered up-front so runtime set can flip enabled.
    assert len(registered) == 1
    assert registered[0][0] == "after_turn"
    st = get_state()
    assert st is not None
    assert st.cfg.enabled is False

    # ...but with cfg.enabled=False, maybe_fork is a no-op.
    async def go():
        for _ in range(20):
            await registered[0][1](_make_payload())
        assert fake_runner.spawn_calls == []

    asyncio.run(go())


def test_runtime_enable_flip_starts_spawning(monkeypatch, fake_runner):
    """After plugin registers in disabled mode, flipping cfg.enabled to True
    at runtime causes the next end_turn at the trigger boundary to spawn."""
    from dataclasses import dataclass

    registered: list[tuple[str, Any]] = []

    @dataclass
    class _StubApi:
        plugin_config: dict
        config: Any = None

        def register_hook(self, event, handler, priority=0):
            registered.append((event, handler))

        def register_tool(self, tool):
            pass

    plugin = ReviewForkPlugin()
    api = _StubApi(plugin_config={"enabled": False, "trigger_n": 2, "cooldown_s": 0})
    plugin.register(api)
    handler = registered[0][1]
    st = get_state()
    assert st is not None

    async def go():
        # Disabled — no spawn even at trigger boundary.
        for _ in range(4):
            await handler(_make_payload())
        assert fake_runner.spawn_calls == []
        # Flip on.
        st.cfg.enabled = True
        # Counter is still 0 (early returns didn't increment) — first 2 turns spawn.
        await handler(_make_payload())
        await handler(_make_payload())
        assert len(fake_runner.spawn_calls) == 1

    asyncio.run(go())


def test_force_fork_bypasses_cooldown_and_counter(fake_runner):
    cfg = ReviewForkConfig(enabled=True, trigger_n=10, cooldown_s=600)
    state = ReviewForkState(cfg)
    state.cooldown_until = time.time() + 999.0
    state.turn_counter = 5  # Not a multiple of 10

    async def go():
        run_id = await state.force_fork(_make_payload())
        assert run_id is not None
        assert len(fake_runner.spawn_calls) == 1

    asyncio.run(go())


def test_no_workspace_dir_skips(fake_runner):
    cfg = ReviewForkConfig(enabled=True, trigger_n=1, cooldown_s=0)
    state = ReviewForkState(cfg)

    async def go():
        await state.maybe_fork(_make_payload(workspace_dir=""))
        assert fake_runner.spawn_calls == []

    asyncio.run(go())


def test_can_spawn_false_blocks_fork(fake_runner):
    fake_runner._can_spawn = False
    cfg = ReviewForkConfig(enabled=True, trigger_n=1, cooldown_s=0)
    state = ReviewForkState(cfg)

    async def go():
        await state.maybe_fork(_make_payload())
        assert fake_runner.spawn_calls == []
        # state still records skip
        assert state.total_skipped >= 1

    asyncio.run(go())


def test_build_review_fork_registry_filters_to_allowlist():
    parent = build_core_registry()
    parent.set_workspace_dir("/tmp/ws")
    parent.set_state_dir("/tmp/state")
    restricted = build_review_fork_registry(parent)
    names = set(restricted.names())
    # Every restricted name must be on the allowlist...
    assert names <= REVIEW_FORK_ALLOWLIST
    # ...and bash / list_dir-without-allowlist-name must be absent
    assert "bash" not in names
    # workspace / state dirs forwarded
    assert restricted._workspace_dir == "/tmp/ws"
    assert restricted._state_dir == "/tmp/state"


def test_review_fork_config_from_dict_handles_camelcase():
    cfg = ReviewForkConfig.from_dict({
        "enabled": True,
        "triggerN": 7,
        "cooldownS": 120,
        "timeoutS": 60,
        "modelAux": "anthropic/claude-haiku",
    })
    assert cfg.enabled is True
    assert cfg.trigger_n == 7
    assert cfg.cooldown_s == 120
    assert cfg.timeout_s == 60
    assert cfg.model_aux == "anthropic/claude-haiku"


def test_status_dict_shape(fake_runner):
    cfg = ReviewForkConfig(enabled=True, trigger_n=10, cooldown_s=60)
    state = ReviewForkState(cfg)

    async def go():
        await state.maybe_fork(_make_payload(stop_reason="max_iter"))
        s = state.status()
        for key in (
            "enabled", "trigger_n", "cooldown_s", "timeout_s", "model_aux",
            "turn_counter", "total_runs", "total_skipped", "active_run_id",
            "last_run_at", "cooldown_remaining_s", "last_skip_reason",
        ):
            assert key in s
        assert s["enabled"] is True
        assert s["trigger_n"] == 10

    asyncio.run(go())
