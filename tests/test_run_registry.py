"""RunRegistry + cron abort plumbing tests.

Two layers:

1. **RunRegistry primitives** — register / cancel / unregister, idempotency.
2. **Cron abort path** — registering a cron-origin entry and cancelling via
   ``EmbeddedBackend.chat_abort(turn_id)`` flips the same CancellationToken
   the scheduler is using. Verified without spawning the actual cron
   ``_execute_job`` (that needs a real LLM client); we drive RunRegistry
   directly to keep the test deterministic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nano_openclaw.services.backend_embedded import EmbeddedBackend
from nano_openclaw.services.runs import RunEntry, RunRegistry, cron_turn_id
from nano_openclaw.services.runtime_update import RuntimeUpdateGuard
from nano_openclaw.core.loop import CancellationToken, LoopConfig
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


# ────────────────────────────────────────────────────────────────────────────
# RunRegistry primitives
# ────────────────────────────────────────────────────────────────────────────


def test_register_get_unregister():
    reg = RunRegistry()
    tok = CancellationToken()
    entry = reg.register(turn_id="t1", origin="chat", cancellation_token=tok)
    assert reg.get("t1") is entry
    assert "t1" in reg
    assert len(reg) == 1
    reg.unregister("t1")
    assert reg.get("t1") is None
    assert len(reg) == 0


def test_register_overwrites_existing():
    reg = RunRegistry()
    tok1 = CancellationToken()
    tok2 = CancellationToken()
    e1 = reg.register(turn_id="t1", origin="chat", cancellation_token=tok1)
    e2 = reg.register(turn_id="t1", origin="cron", cancellation_token=tok2, label="job-x")
    assert reg.get("t1") is e2
    assert e2 is not e1
    assert e2.origin == "cron"


def test_cancel_returns_false_for_unknown_turn():
    reg = RunRegistry()
    assert reg.cancel("nonexistent") is False


def test_cancel_flips_token_but_keeps_entry():
    """Cancel issues the cancel signal; the runner that owns the turn
    is responsible for unregistering in its finally clause.
    """
    reg = RunRegistry()
    tok = CancellationToken()
    reg.register(turn_id="t1", origin="cron", cancellation_token=tok)
    ok = reg.cancel("t1")
    assert ok
    assert tok.is_cancelled
    assert "t1" in reg  # still registered until runner observes the cancel
    reg.unregister("t1")


def test_list_returns_all_entries():
    reg = RunRegistry()
    reg.register(turn_id="t1", origin="chat", cancellation_token=CancellationToken())
    reg.register(turn_id="t2", origin="cron", cancellation_token=CancellationToken())
    entries = reg.list()
    assert {e.turn_id for e in entries} == {"t1", "t2"}


def test_unregister_unknown_is_idempotent():
    reg = RunRegistry()
    assert reg.unregister("nope") is None  # no exception


# ────────────────────────────────────────────────────────────────────────────
# Deterministic cron turn_id
# ────────────────────────────────────────────────────────────────────────────


def test_cron_turn_id_format():
    assert cron_turn_id("abcdef0123456", "uvwxyz9876543") == "cron:abcdef01:uvwxyz98"


def test_cron_turn_id_is_stable_for_same_inputs():
    a = cron_turn_id("job-1", "run-1")
    b = cron_turn_id("job-1", "run-1")
    assert a == b


# ────────────────────────────────────────────────────────────────────────────
# EmbeddedBackend ↔ RunRegistry integration
# ────────────────────────────────────────────────────────────────────────────


def test_embedded_backend_uses_runtime_run_registry(tmp_path):
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    assert backend._run_registry is runtime.run_registry


def test_chat_abort_cancels_cron_origin_turn(tmp_path):
    """The whole point of Phase 6: ``EmbeddedBackend.chat_abort(turn_id)``
    targets ANY entry in the registry — chat or cron. Here we register a
    cron-origin entry directly (simulating what scheduler._execute_job does)
    and verify chat_abort flips its cancellation token.
    """
    async def run():
        runtime = _fake_runtime(tmp_path)
        backend = EmbeddedBackend(runtime)

        cron_token = CancellationToken()
        runtime.run_registry.register(
            turn_id="cron:job1234:run5678",
            origin="cron",
            cancellation_token=cron_token,
            label="cron job: backup",
        )
        try:
            await backend.chat_abort(turn_id="cron:job1234:run5678")
            assert cron_token.is_cancelled is True
        finally:
            runtime.run_registry.unregister("cron:job1234:run5678")
            await backend.aclose()

    asyncio.run(run())


def test_chat_abort_unknown_turn_is_noop(tmp_path):
    async def run():
        backend = EmbeddedBackend(_fake_runtime(tmp_path))
        # Should not raise even if the registry has nothing.
        await backend.chat_abort(turn_id="does-not-exist")
        await backend.aclose()

    asyncio.run(run())


def test_health_reports_in_flight_from_registry(tmp_path):
    async def run():
        runtime = _fake_runtime(tmp_path)
        backend = EmbeddedBackend(runtime)
        try:
            runtime.run_registry.register(
                turn_id="t1", origin="chat", cancellation_token=CancellationToken(),
            )
            runtime.run_registry.register(
                turn_id="t2", origin="cron", cancellation_token=CancellationToken(),
            )
            health = await backend.health()
            assert health.in_flight_turns == 2
        finally:
            runtime.run_registry.unregister("t1")
            runtime.run_registry.unregister("t2")
            await backend.aclose()

    asyncio.run(run())


def test_aclose_only_cancels_chat_origin_turns(tmp_path):
    """The Backend's aclose tears down chat-origin turns it spawned, but
    cron-origin turns belong to the scheduler and shouldn't be touched —
    they get cancelled via daemon shutdown's ``cron_stop`` event instead.
    """
    async def run():
        runtime = _fake_runtime(tmp_path)
        backend = EmbeddedBackend(runtime)

        chat_token = CancellationToken()
        cron_token = CancellationToken()
        runtime.run_registry.register(
            turn_id="chat-1", origin="chat", cancellation_token=chat_token, task=None,
        )
        runtime.run_registry.register(
            turn_id="cron-1", origin="cron", cancellation_token=cron_token, task=None,
        )

        await backend.aclose()

        assert chat_token.is_cancelled is True
        # Cron token is owned by the scheduler — Backend leaves it alone.
        assert cron_token.is_cancelled is False

        # Cleanup
        runtime.run_registry.unregister("chat-1")
        runtime.run_registry.unregister("cron-1")

    asyncio.run(run())


# ────────────────────────────────────────────────────────────────────────────
# RPC chat.abort dispatches to RunRegistry
# ────────────────────────────────────────────────────────────────────────────


def test_rpc_chat_abort_cancels_cron_turn(tmp_path):
    """End-to-end via the gateway dispatch layer — ``chat.abort`` over the
    method registry should cancel cron turns just like chat turns.
    """
    import json
    from nano_openclaw.channels.registry import ChannelRegistry
    from nano_openclaw.gateway.context import GatewayContext
    from nano_openclaw.gateway.ws_route import _dispatch_one

    async def run():
        runtime = _fake_runtime(tmp_path)
        backend = EmbeddedBackend(runtime)
        ctx = GatewayContext(
            runtime=runtime, backend=backend, channel_registry=ChannelRegistry(),
        )
        cron_token = CancellationToken()
        runtime.run_registry.register(
            turn_id="cron:abc:def",
            origin="cron",
            cancellation_token=cron_token,
        )
        try:
            raw = json.dumps({
                "id": "abort-1",
                "method": "chat.abort",
                "params": {"turn_id": "cron:abc:def"},
            })
            resp = await _dispatch_one(ctx, raw)
            assert resp.ok is True
            assert cron_token.is_cancelled is True
        finally:
            runtime.run_registry.unregister("cron:abc:def")
            await backend.aclose()

    asyncio.run(run())
