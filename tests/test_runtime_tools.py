"""Tests for the LLM-facing runtime introspection tools.

Phase 8 added a curated set of tools (list_models / switch_model /
get_runtime / list_sessions / list_tools / list_skills / list_channels /
get_health / get_context) that the model can invoke through natural
language. These tests verify:

1. The tools are registered onto ``runtime.registry`` after EmbeddedBackend
   construction (no_tools=False path).
2. ``list_models`` returns a JSON-encoded catalog matching ``models_list``.
3. ``switch_model`` is gated by ApprovalManager — without an interactive
   approval handler, dispatch returns an error result rather than silently
   switching.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nano_openclaw.approvals.manager import ApprovalManager
from nano_openclaw.approvals.types import ApprovalPolicy
from nano_openclaw.gateway.backend_embedded import EmbeddedBackend
from nano_openclaw.gateway.run_registry import RunRegistry
from nano_openclaw.gateway.runtime_lock import RuntimeUpdateGuard
from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.core.tools import ToolRegistry


def _model(model_id, *, name=None):
    return SimpleNamespace(
        id=model_id, name=name, input=["text"], reasoning=False,
        contextWindow=200000, maxTokens=8192,
    )


def _fake_runtime(tmp_path: Path, *, no_tools: bool = False) -> SimpleNamespace:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    cfg = LoopConfig(model="claude-sonnet-4-5", workspace_dir=workspace, session_key="default")
    cfg.context_window = 200000
    return SimpleNamespace(
        agent_id="default",
        session_id="default",
        config=SimpleNamespace(
            models=SimpleNamespace(providers={
                "anthropic": SimpleNamespace(
                    api="anthropic-messages", baseUrl=None, apiKey=None,
                    models=[
                        _model("claude-sonnet-4-5", name="Sonnet"),
                        _model("claude-haiku-4-5", name="Haiku"),
                    ],
                ),
            }),
            noTools=no_tools,
        ),
        warnings=[],
        client=None,
        registry=ToolRegistry(),
        cfg=cfg,
        hook_registry=None,
        state_dir=state,
        session_dir=sd,
        store_path=tmp_path / "sessions.json",
        workspace_dir=workspace,
        model_ref="anthropic/claude-sonnet-4-5",
        model_id="claude-sonnet-4-5",
        image_model_ref=None,
        run_registry=RunRegistry(),
        runtime_guard=RuntimeUpdateGuard(),
        config_path=None,
    )


def test_runtime_tools_registered_after_backend_init(tmp_path):
    runtime = _fake_runtime(tmp_path, no_tools=False)
    backend = EmbeddedBackend(runtime)
    try:
        names = set(runtime.registry._tools.keys())
    finally:
        asyncio.run(backend.aclose())
    expected = {
        "list_models", "switch_model", "set_thinking",
        "get_runtime", "get_context",
        "list_sessions", "list_tools", "list_skills", "list_channels",
        "get_health",
    }
    assert expected.issubset(names)


def test_runtime_tools_skipped_when_no_tools(tmp_path):
    """``noTools=True`` keeps the registry pristine — runtime tools not added."""
    runtime = _fake_runtime(tmp_path, no_tools=True)
    backend = EmbeddedBackend(runtime)
    try:
        names = set(runtime.registry._tools.keys())
    finally:
        asyncio.run(backend.aclose())
    assert "list_models" not in names
    assert "switch_model" not in names


def test_list_models_tool_returns_json_catalog(tmp_path):
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)

    async def run():
        try:
            tool = runtime.registry._tools["list_models"]
            raw = tool.run({})
            return await raw if asyncio.iscoroutine(raw) else raw
        finally:
            await backend.aclose()

    out = asyncio.run(run())
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["current_ref"] == "anthropic/claude-sonnet-4-5"
    refs = [m["ref"] for m in payload["models"]]
    assert "anthropic/claude-sonnet-4-5" in refs
    assert "anthropic/claude-haiku-4-5" in refs
    sonnet = next(m for m in payload["models"] if m["ref"] == "anthropic/claude-sonnet-4-5")
    assert sonnet["is_default"] is True


def test_switch_model_in_dangerous_tools_and_requires_approval(tmp_path):
    """``switch_model`` must be in ``dangerous_tools`` so ApprovalManager's
    ``check_request`` flags it for approval; this is what blocks
    non-interactive (cron / channel) auto-turns from silently swapping the
    model. We assert the policy directly rather than driving dispatch — the
    dispatch path itself is covered by tests/test_tools.py with its
    sync-dispatch monkey patch.
    """
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    try:
        manager = ApprovalManager(
            policy=ApprovalPolicy(ask_mode="on-miss", security_mode="allowlist"),
        )
        eval_result = manager.check_request(
            "switch_model", {"model_ref": "anthropic/claude-haiku-4-5"}
        )
        assert eval_result.requires_approval is True
        # Sanity: the policy's default catalog wires ``switch_model`` in.
        assert "switch_model" in manager.policy.dangerous_tools
        assert "switch_model" in manager.policy.tool_configs
    finally:
        asyncio.run(backend.aclose())


def test_set_thinking_tool_queues_change_effective_next_turn(tmp_path):
    """``set_thinking`` runs inside a turn (RuntimeUpdateGuard reader held), so
    it can't mutate the runtime immediately. It queues the change: runtime.cfg
    stays put until the turn-end flush runs, then takes effect — mirroring the
    ``/thinking`` slash command's next-turn semantics."""
    import json

    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)

    async def run():
        try:
            tool = runtime.registry._tools["set_thinking"]
            raw = tool.run({"level": "high"})
            out = await raw if asyncio.iscoroutine(raw) else raw
            payload = json.loads(out)
            # Queued, not yet applied.
            assert payload["ok"] is True
            assert payload["from"] == "off"
            assert payload["to"] == "high"
            assert payload["effective"] == "next_turn"
            assert backend._pending_thinking_level == "high"
            assert runtime.cfg.thinking_level == "off"  # unchanged until flush
            # End-of-turn flush applies it onto the global runtime.
            await backend._flush_pending_thinking()
            assert runtime.cfg.thinking_level == "high"
            assert backend._pending_thinking_level is None
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_set_thinking_flush_requeues_while_turn_in_flight(tmp_path):
    """If a concurrent turn still holds the reader when the flush fires, the
    writer raises BusyError; the change is re-queued so the next flush applies
    it rather than getting silently dropped."""
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)

    async def run():
        try:
            backend.queue_thinking_level("high")
            # Simulate another turn holding the reader → writer BusyError.
            async with runtime.runtime_guard.reader():
                await backend._flush_pending_thinking()
                assert backend._pending_thinking_level == "high"  # re-queued
                assert runtime.cfg.thinking_level == "off"  # untouched
            # Reader released → flush now lands the change.
            await backend._flush_pending_thinking()
            assert runtime.cfg.thinking_level == "high"
            assert backend._pending_thinking_level is None
        finally:
            await backend.aclose()

    asyncio.run(run())


def test_set_thinking_tool_rejects_unknown_level(tmp_path):
    import json

    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)

    async def run():
        try:
            tool = runtime.registry._tools["set_thinking"]
            raw = tool.run({"level": "turbo"})
            return await raw if asyncio.iscoroutine(raw) else raw
        finally:
            await backend.aclose()

    out = asyncio.run(run())
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "unknown" in payload["error"].lower()
    assert runtime.cfg.thinking_level == "off"  # untouched


def test_switch_model_tool_unknown_ref_returns_ok_false(tmp_path):
    """When the registry has no approval gate, the tool runs but the
    underlying ``_resolve_model_option`` flags an unknown ref —
    error is encoded as ``ok=False`` JSON, not a raised exception (preserves
    the dispatcher invariant)."""
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    # No approval_manager → tool runs straight through.

    async def run():
        try:
            tool = runtime.registry._tools["switch_model"]
            raw = tool.run({"model_ref": "nope/missing"})
            return await raw if asyncio.iscoroutine(raw) else raw
        finally:
            await backend.aclose()

    out = asyncio.run(run())
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "unknown" in payload["error"].lower()
