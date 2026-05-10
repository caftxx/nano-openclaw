"""Phase 8 — features RPC (active_memory / dreaming) tests.

Three layers:

1. ``EmbeddedBackend`` field-by-field get / set behavior.
2. ``_dispatch_one`` round-trip for each new RPC method (uses the same
   fake-runtime fixture as the other gateway tests).
3. METHODS_V1 ↔ CORE_HANDLERS sync still holds.

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

from nano_openclaw.channels.registry import ChannelRegistry
from nano_openclaw.gateway.backend import BackendError, NotFoundError
from nano_openclaw.gateway.backend_embedded import EmbeddedBackend
from nano_openclaw.gateway.context import GatewayContext
from nano_openclaw.gateway.protocol import ErrorCode, METHODS_V1
from nano_openclaw.gateway.run_registry import RunRegistry
from nano_openclaw.gateway.runtime_lock import RuntimeUpdateGuard
from nano_openclaw.gateway.ws_route import _dispatch_one
from nano_openclaw.loop import LoopConfig
from nano_openclaw.memory.active import ActiveMemoryConfig, PromptStyle, QueryMode
from nano_openclaw.memory.dreaming import DreamingConfig
from nano_openclaw.tools import ToolRegistry


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
        config=SimpleNamespace(wechat=SimpleNamespace(accounts=[])),
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
        runtime=runtime, backend=backend, channel_registry=ChannelRegistry(),
    )
    return ctx, backend


# ────────────────────────────────────────────────────────────────────────────
# Catalog sync — adding new methods without wiring would break this
# ────────────────────────────────────────────────────────────────────────────


def test_methods_v1_includes_features():
    expected = {
        "active_memory.get", "active_memory.set",
        "dreaming.get", "dreaming.set", "dreaming.run",
    }
    assert expected.issubset(METHODS_V1)


def test_handlers_match_methods_v1_after_phase8():
    from nano_openclaw.gateway.methods import CORE_HANDLERS
    assert set(CORE_HANDLERS.keys()) == set(METHODS_V1)


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
