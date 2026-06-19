"""Phase 8 — features RPC (active_memory / dreaming) tests.

Three layers:

1. ``EmbeddedBackend`` field-by-field get / set behavior.
2. ``_dispatch_one`` round-trip for each new RPC method (uses the same
   fake-runtime fixture as the other gateway tests).
3. METHODS ↔ CORE_HANDLERS sync still holds.

Real ``dreaming.run`` integration is out of scope (it spawns an LLM call).
We assert the wiring + the not-configured / no-workspace error paths.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nano_openclaw.services.channels import ChannelManager
from nano_openclaw.services.backend import BackendError, NotFoundError
from nano_openclaw.services.backend_embedded import EmbeddedBackend
from nano_openclaw.api.context import GatewayContext
from nano_openclaw.api.protocol import ErrorCode, METHODS
from nano_openclaw.services.runs import RunRegistry
from nano_openclaw.services.runtime_update import RuntimeUpdateGuard
from nano_openclaw.api.ws_route import _dispatch_one
from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.features.memory.active import ActiveMemoryConfig, PromptStyle, QueryMode
from nano_openclaw.features.memory.dreaming import DreamingConfig
from nano_openclaw.core.tools import ToolRegistry


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


def _make_ctx(tmp_path: Path) -> tuple[GatewayContext, EmbeddedBackend]:
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    ctx = GatewayContext(
        runtime=runtime, backend=backend, channel_manager=ChannelManager(),
    )
    return ctx, backend


# ────────────────────────────────────────────────────────────────────────────
# Catalog sync — adding new methods without wiring would break this
# ────────────────────────────────────────────────────────────────────────────


def test_methods_v1_includes_features():
    expected = {
        "active_memory.get", "active_memory.set",
        "dreaming.get", "dreaming.set", "dreaming.run",
        "review_fork.get", "review_fork.set", "review_fork.run",
        "curator.get", "curator.set", "curator.run",
        "checkpoint.list", "checkpoint.create", "checkpoint.restore",
    }
    assert expected.issubset(METHODS)


def test_handlers_match_methods_v1_after_phase8():
    from nano_openclaw.api.methods import CORE_HANDLERS
    assert set(CORE_HANDLERS.keys()) == set(METHODS)


# ────────────────────────────────────────────────────────────────────────────
# active_memory.get / set
# ────────────────────────────────────────────────────────────────────────────


def test_active_memory_get_unconfigured_returns_minimal(tmp_path):
    """When the agent boot didn't wire active-memory, get() returns
    ``configured=False`` so a frontend can render "not configured".
    """
    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            payload = await backend.active_memory_get()
            assert payload["configured"] is False
            assert payload["enabled"] is False
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_curator_and_checkpoint_backend_smoke(tmp_path):
    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            curator_status = await backend.curator_get()
            assert curator_status["configured"] is True
            assert curator_status["total"] == 0

            created = await backend.checkpoint_create(reason="smoke")
            assert created["checkpoint"]["reason"] == "smoke"
            listed = await backend.checkpoint_list()
            assert len(listed["checkpoints"]) == 1
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_active_memory_set_lazy_creates_config(tmp_path):
    """set() on an unconfigured agent must initialise the cfg in place,
    so a follow-up enable→true sticks even though the agent didn't have
    active-memory in its boot config.
    """
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        try:
            payload = await backend.active_memory_set(enabled=True)
            assert payload["configured"] is True
            assert payload["enabled"] is True
            # And it persisted on the runtime cfg so the next get() agrees
            again = await backend.active_memory_get()
            assert again["enabled"] is True
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_active_memory_set_preserves_existing_fields(tmp_path):
    """Toggling one field shouldn't reset the rest — set() is a partial
    mutation, not a full overwrite.
    """
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        ctx.runtime.cfg.active_memory_config = ActiveMemoryConfig(
            enabled=True,
            query_mode=QueryMode.RECENT,
            prompt_style=PromptStyle.STRICT,
            timeout_ms=30000,
        )
        try:
            await backend.active_memory_set(enabled=False)
            payload = await backend.active_memory_get()
            assert payload["enabled"] is False
            assert payload["query_mode"] == "recent"          # unchanged
            assert payload["prompt_style"] == "strict"        # unchanged
            assert payload["timeout_ms"] == 30000             # unchanged
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_active_memory_set_invalid_enum_raises(tmp_path):
    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            with pytest.raises(BackendError):
                await backend.active_memory_set(query_mode="not-a-real-mode")
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_active_memory_set_accepts_known_query_modes(tmp_path):
    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            for mode in ("message", "recent", "full"):
                payload = await backend.active_memory_set(query_mode=mode)
                assert payload["query_mode"] == mode
        finally:
            await backend.aclose()

    asyncio.run(run())


# ────────────────────────────────────────────────────────────────────────────
# dreaming.get / set / run
# ────────────────────────────────────────────────────────────────────────────


def test_dreaming_get_unconfigured(tmp_path):
    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            payload = await backend.dreaming_get()
            assert payload["configured"] is False
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_dreaming_set_lazy_creates_and_includes_status(tmp_path):
    """When workspace_dir is wired, dreaming.get() returns the runtime
    status block from get_dreaming_status alongside the config.
    """
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        try:
            payload = await backend.dreaming_set(enabled=True, frequency="weekly")
            assert payload["configured"] is True
            assert payload["enabled"] is True
            assert payload["frequency"] == "weekly"
            # Status was attached because workspace_dir is set on the fake runtime.
            assert "status" in payload
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_dreaming_set_preserves_existing_fields(tmp_path):
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        ctx.runtime.cfg.dreaming_config = DreamingConfig(
            enabled=True,
            frequency="daily",
            min_score=2.5,
            max_promotions=3,
        )
        try:
            await backend.dreaming_set(enabled=False)
            payload = await backend.dreaming_get()
            assert payload["enabled"] is False
            assert payload["frequency"] == "daily"
            assert payload["min_score"] == 2.5
            assert payload["max_promotions"] == 3
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_dreaming_run_unconfigured_raises_not_found(tmp_path):
    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            with pytest.raises(NotFoundError):
                await backend.dreaming_run()
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_dreaming_run_no_workspace_raises_backend_error(tmp_path):
    """When dreaming is configured but the runtime has no workspace_dir
    the run path can't proceed — should raise BackendError, not crash.
    """
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        ctx.runtime.cfg.dreaming_config = DreamingConfig(enabled=True)
        ctx.runtime.workspace_dir = None
        try:
            with pytest.raises(BackendError):
                await backend.dreaming_run()
        finally:
            await backend.aclose()

    asyncio.run(run())


# ────────────────────────────────────────────────────────────────────────────
# RPC dispatch round-trip
# ────────────────────────────────────────────────────────────────────────────


def test_rpc_active_memory_get_succeeds(tmp_path):
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        raw = json.dumps({"id": "am1", "method": "active_memory.get", "params": {}})
        try:
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["configured"] is False


def test_rpc_active_memory_set_round_trip(tmp_path):
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        try:
            raw = json.dumps({
                "id": "am2",
                "method": "active_memory.set",
                "params": {"enabled": True, "query_mode": "full"},
            })
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["enabled"] is True
    assert resp.payload["query_mode"] == "full"


def test_rpc_active_memory_set_invalid_enum_returns_internal(tmp_path):
    """BackendError surfaces as INTERNAL on the wire (not BUSY/NOT_FOUND)."""
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        try:
            raw = json.dumps({
                "id": "am3",
                "method": "active_memory.set",
                "params": {"query_mode": "bogus"},
            })
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is False
    assert resp.error.code == ErrorCode.INTERNAL


def test_rpc_dreaming_get_succeeds(tmp_path):
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        try:
            raw = json.dumps({"id": "d1", "method": "dreaming.get"})
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["configured"] is False


def test_rpc_dreaming_set_round_trip(tmp_path):
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        try:
            raw = json.dumps({
                "id": "d2",
                "method": "dreaming.set",
                "params": {"enabled": True, "frequency": "monthly"},
            })
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["enabled"] is True
    assert resp.payload["frequency"] == "monthly"


def test_rpc_dreaming_run_unconfigured_returns_not_found(tmp_path):
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        try:
            raw = json.dumps({"id": "d3", "method": "dreaming.run"})
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is False
    assert resp.error.code == ErrorCode.NOT_FOUND


# ────────────────────────────────────────────────────────────────────────────
# review_fork.get / set / run
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def _reset_review_fork_state():
    from nano_openclaw.features.review_fork.plugin import reset_state
    reset_state()
    yield
    reset_state()


def _install_review_fork_state(enabled: bool = True):
    """Install a module-level ReviewForkState (mimic plugin.register)."""
    from nano_openclaw.features.review_fork.plugin import (
        ReviewForkConfig, ReviewForkState, _set_state,
    )
    state = ReviewForkState(ReviewForkConfig(enabled=enabled, trigger_n=10))
    _set_state(state)
    return state


def test_review_fork_get_unloaded_returns_minimal(tmp_path, _reset_review_fork_state):
    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            payload = await backend.review_fork_get()
            assert payload["configured"] is False
            assert payload["enabled"] is False
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_review_fork_get_loaded_returns_status(tmp_path, _reset_review_fork_state):
    _install_review_fork_state(enabled=False)

    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            payload = await backend.review_fork_get()
            assert payload["configured"] is True
            assert payload["enabled"] is False
            assert payload["trigger_n"] == 10
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_review_fork_set_flips_enabled(tmp_path, _reset_review_fork_state):
    _install_review_fork_state(enabled=False)

    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            payload = await backend.review_fork_set(enabled=True, trigger_n=5)
            assert payload["enabled"] is True
            assert payload["trigger_n"] == 5
            again = await backend.review_fork_get()
            assert again["enabled"] is True
            assert again["trigger_n"] == 5
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_review_fork_set_unloaded_raises_not_found(tmp_path, _reset_review_fork_state):
    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            with pytest.raises(NotFoundError):
                await backend.review_fork_set(enabled=True)
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_review_fork_run_disabled_returns_skip(tmp_path, _reset_review_fork_state):
    _install_review_fork_state(enabled=False)

    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            payload = await backend.review_fork_run()
            assert payload["skipped"] is True
            assert payload["run_id"] is None
            assert "disabled" in (payload.get("reason") or "")
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_review_fork_run_unloaded_raises_not_found(tmp_path, _reset_review_fork_state):
    async def run():
        _, backend = _make_ctx(tmp_path)
        try:
            with pytest.raises(NotFoundError):
                await backend.review_fork_run()
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_rpc_review_fork_get_succeeds(tmp_path, _reset_review_fork_state):
    async def run():
        ctx, backend = _make_ctx(tmp_path)
        try:
            raw = json.dumps({"id": "rf1", "method": "review_fork.get"})
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["configured"] is False


def test_rpc_review_fork_set_round_trip(tmp_path, _reset_review_fork_state):
    _install_review_fork_state(enabled=False)

    async def run():
        ctx, backend = _make_ctx(tmp_path)
        try:
            raw = json.dumps({
                "id": "rf2",
                "method": "review_fork.set",
                "params": {"enabled": True, "cooldown_s": 30},
            })
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["enabled"] is True
    assert resp.payload["cooldown_s"] == 30


def test_rpc_review_fork_run_disabled_returns_skip(tmp_path, _reset_review_fork_state):
    _install_review_fork_state(enabled=False)

    async def run():
        ctx, backend = _make_ctx(tmp_path)
        try:
            raw = json.dumps({"id": "rf3", "method": "review_fork.run"})
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["skipped"] is True
