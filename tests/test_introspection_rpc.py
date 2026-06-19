"""Tests for tools.list / skills.list / plugins.list / hooks.list RPCs.

These reads back the ``/tools`` ``/skills`` ``/plugins`` ``/hooks`` slash
commands when the TUI runs in remote mode. EmbeddedBackend reads the same
data directly off ``runtime.registry``; the RPC parity is what makes both
modes render identical panels.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nano_openclaw.services.channels import ChannelManager
from nano_openclaw.services.backend import Backend
from nano_openclaw.services.backend_embedded import EmbeddedBackend
from nano_openclaw.api.context import GatewayContext
from nano_openclaw.api.protocol import METHODS
from nano_openclaw.services.runs import RunRegistry
from nano_openclaw.services.runtime_update import RuntimeUpdateGuard
from nano_openclaw.api.ws_route import _dispatch_one
from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.core.tools import Tool, ToolRegistry


def _registry_with_two_tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(Tool(name="bash", description="run shell command", input_schema={}, run=lambda a: ""))
    reg.register(Tool(name="read_file", description="read a file", input_schema={}, run=lambda a: ""))
    return reg


def _fake_runtime(tmp_path: Path, *, registry: ToolRegistry | None = None) -> SimpleNamespace:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    cfg = LoopConfig(model="test-model", workspace_dir=workspace, session_key="default")
    # ``noTools=True`` so EmbeddedBackend skips its runtime introspection
    # tool registration (list_models / switch_model / get_runtime / …) —
    # these tests assert exact tool counts and the runtime-tool surface is
    # covered separately in test_runtime_tools.
    return SimpleNamespace(
        agent_id="default",
        session_id="default",
        config=SimpleNamespace(
            noTools=True,
        ),
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


def _ctx(tmp_path: Path, *, registry: ToolRegistry | None = None) -> tuple[GatewayContext, EmbeddedBackend]:
    runtime = _fake_runtime(tmp_path, registry=registry)
    backend = EmbeddedBackend(runtime)
    return GatewayContext(runtime=runtime, backend=backend, channel_manager=ChannelManager()), backend


# ────────────────────────────────────────────────────────────────────────────
# Catalog sync — adding a new method without wiring breaks here
# ────────────────────────────────────────────────────────────────────────────


def test_methods_v1_includes_introspection():
    expected = {"tools.list", "skills.list", "plugins.list", "hooks.list"}
    assert expected.issubset(METHODS)


def test_handlers_match_methods_v1_with_introspection():
    from nano_openclaw.api.methods import CORE_HANDLERS
    assert set(CORE_HANDLERS.keys()) == set(METHODS)


# ────────────────────────────────────────────────────────────────────────────
# Backend-level reads
# ────────────────────────────────────────────────────────────────────────────


def test_tools_list_returns_name_and_description(tmp_path):
    async def run():
        _, backend = _ctx(tmp_path, registry=_registry_with_two_tools())
        try:
            tools = await backend.tools_list()
            return tools
        finally:
            await backend.aclose()

    tools = asyncio.run(run())
    assert {t["name"] for t in tools} == {"bash", "read_file"}
    descs = {t["name"]: t["description"] for t in tools}
    assert descs["bash"] == "run shell command"


def test_tools_list_empty_when_no_tools(tmp_path):
    async def run():
        _, backend = _ctx(tmp_path)  # fresh empty registry
        try:
            return await backend.tools_list()
        finally:
            await backend.aclose()

    assert asyncio.run(run()) == []


def test_skills_list_returns_empty_when_no_workspace(tmp_path):
    """Workspace dir exists but has no skills directory — get_or_load_skills
    yields nothing eligible.
    """
    async def run():
        _, backend = _ctx(tmp_path)
        try:
            return await backend.skills_list()
        finally:
            await backend.aclose()

    skills = asyncio.run(run())
    assert isinstance(skills, list)


def test_plugins_list_empty_when_no_hook_registry(tmp_path):
    async def run():
        _, backend = _ctx(tmp_path)
        try:
            return await backend.plugins_list()
        finally:
            await backend.aclose()

    assert asyncio.run(run()) == []


def test_hooks_list_empty_when_no_hook_registry(tmp_path):
    async def run():
        _, backend = _ctx(tmp_path)
        try:
            return await backend.hooks_list()
        finally:
            await backend.aclose()

    assert asyncio.run(run()) == {}


# ────────────────────────────────────────────────────────────────────────────
# RuntimeSnapshot — context fields must round-trip through runtime.get
# ────────────────────────────────────────────────────────────────────────────


def test_runtime_get_includes_context_budget(tmp_path):
    async def run():
        _, backend = _ctx(tmp_path)
        backend.runtime.cfg.context_budget = 200000
        backend.runtime.cfg.context_threshold = 0.85
        backend.runtime.cfg.context_recent_turns = 5
        try:
            return await backend.runtime_get()
        finally:
            await backend.aclose()

    snap = asyncio.run(run())
    assert snap.context_budget == 200000
    assert snap.context_threshold == 0.85
    assert snap.context_recent_turns == 5


def test_runtime_get_payload_round_trip_via_dispatch(tmp_path):
    """The wire path must surface the new context fields so a remote TUI's
    ``/context`` slash can render without a separate RPC.
    """
    async def run():
        ctx, backend = _ctx(tmp_path)
        ctx.runtime.cfg.context_budget = 100_000
        try:
            raw = json.dumps({"id": "rg", "method": "runtime.get", "params": {}})
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["context_budget"] == 100_000
    assert "context_threshold" in resp.payload
    assert "context_recent_turns" in resp.payload


# ────────────────────────────────────────────────────────────────────────────
# RPC dispatch round-trip
# ────────────────────────────────────────────────────────────────────────────


def test_rpc_tools_list(tmp_path):
    async def run():
        ctx, backend = _ctx(tmp_path, registry=_registry_with_two_tools())
        try:
            raw = json.dumps({"id": "t", "method": "tools.list"})
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    names = {t["name"] for t in resp.payload["tools"]}
    assert names == {"bash", "read_file"}


def test_rpc_skills_list_empty_workspace(tmp_path):
    async def run():
        ctx, backend = _ctx(tmp_path)
        try:
            raw = json.dumps({"id": "s", "method": "skills.list"})
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert isinstance(resp.payload["skills"], list)


def test_rpc_plugins_list_empty(tmp_path):
    async def run():
        ctx, backend = _ctx(tmp_path)
        try:
            raw = json.dumps({"id": "p", "method": "plugins.list"})
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["plugins"] == []


def test_rpc_hooks_list_empty(tmp_path):
    async def run():
        ctx, backend = _ctx(tmp_path)
        try:
            raw = json.dumps({"id": "h", "method": "hooks.list"})
            return await _dispatch_one(ctx, raw)
        finally:
            await backend.aclose()

    resp = asyncio.run(run())
    assert resp.ok is True
    assert resp.payload["hooks"] == {}
