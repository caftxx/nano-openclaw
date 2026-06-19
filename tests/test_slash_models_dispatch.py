"""End-to-end dispatch tests for /models and /model.

Goes through the full ``handle_slash`` path so we know the new commands
work identically across renderers — same Backend RPCs, same rows, just a
different output skin.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nano_openclaw.gateway.backend_embedded import EmbeddedBackend
from nano_openclaw.gateway.run_registry import RunRegistry
from nano_openclaw.gateway.runtime_lock import RuntimeUpdateGuard
from nano_openclaw.gateway.slash import _resolve_model_option, handle_slash
from nano_openclaw.gateway.slash_renderer import MarkdownRenderer, PlainRenderer
from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.core.tools import ToolRegistry


def _model_def(model_id: str, *, name=None, inputs=("text",)):
    return SimpleNamespace(
        id=model_id, name=name, input=list(inputs), reasoning=False,
        contextWindow=200000, maxTokens=8192,
    )


def _fake_runtime(tmp_path: Path) -> SimpleNamespace:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    cfg = LoopConfig(model="claude-sonnet-4-5", workspace_dir=workspace, session_key="default")
    cfg.context_window = 200000
    providers = {
        "anthropic": SimpleNamespace(
            api="anthropic-messages", baseUrl=None, apiKey=None,
            models=[
                _model_def("claude-sonnet-4-5", name="Sonnet 4.5"),
                _model_def("claude-haiku-4-5", name="Haiku 4.5"),
            ],
        ),
    }
    return SimpleNamespace(
        agent_id="default",
        session_id="default",
        config=SimpleNamespace(
            models=SimpleNamespace(providers=providers),
            noTools=True,
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


def test_slash_models_through_markdown_renderer(tmp_path):
    """``/models`` over MarkdownRenderer renders a GFM table with both models;
    the active model row is bolded and tagged ``← current``."""
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    md = MarkdownRenderer()

    async def run():
        try:
            handled = await handle_slash("/models", backend, md, {"session_key": ""})
            assert handled is True
            return md.collect()
        finally:
            await backend.aclose()

    out = asyncio.run(run())
    assert "anthropic/claude-sonnet-4-5" in out
    assert "anthropic/claude-haiku-4-5" in out
    assert "Sonnet 4.5" in out
    # The active model is highlighted in markdown via bold + ← current.
    assert "**anthropic/claude-sonnet-4-5**" in out
    assert "← current" in out


def test_slash_models_through_plain_renderer(tmp_path):
    """``/models`` over PlainRenderer emits an ASCII table with the current
    row marked by a leading ``* ``.
    """
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    pr = PlainRenderer(emoji=False, width=80)

    async def run():
        try:
            await handle_slash("/models", backend, pr, {"session_key": ""})
            return pr.collect()
        finally:
            await backend.aclose()

    out = asyncio.run(run())
    assert "anthropic/claude-sonnet-4-5" in out
    assert "anthropic/claude-haiku-4-5" in out
    # Sonnet (current) row gets the * prefix.
    star_line = next(line for line in out.splitlines() if "* anthropic/claude-sonnet-4-5" in line)
    assert "Sonnet 4.5" in star_line


def test_slash_model_no_args_shows_current(tmp_path):
    """``/model`` without args renders a single-model panel."""
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    md = MarkdownRenderer()

    async def run():
        try:
            await handle_slash("/model", backend, md, {"session_key": ""})
            return md.collect()
        finally:
            await backend.aclose()

    out = asyncio.run(run())
    assert "anthropic/claude-sonnet-4-5" in out
    assert "claude-sonnet-4-5" in out  # model_id field


def test_slash_model_unknown_returns_error(tmp_path):
    """``/model bogus`` reports unknown without touching runtime."""
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    md = MarkdownRenderer()

    async def run():
        try:
            await handle_slash("/model bogus-model", backend, md, {"session_key": ""})
            return md.collect()
        finally:
            await backend.aclose()

    out = asyncio.run(run())
    assert "unknown model" in out.lower()
    # Runtime model_ref still pointed at the original.
    assert runtime.model_ref == "anthropic/claude-sonnet-4-5"


def test_slash_thinking_no_args_reports_current(tmp_path):
    """``/thinking`` (no args) prints the active level and the allowed set."""
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    md = MarkdownRenderer()

    async def run():
        try:
            await handle_slash("/thinking", backend, md, {"session_key": ""})
            return md.collect()
        finally:
            await backend.aclose()

    out = asyncio.run(run())
    assert "thinking:" in out
    assert "off" in out and "max" in out  # allowed set is rendered


def test_slash_thinking_switch_updates_runtime(tmp_path):
    """``/thinking high`` calls runtime_update — the active level moves and
    the success line names both endpoints."""
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    md = MarkdownRenderer()

    async def run():
        try:
            await handle_slash("/thinking high", backend, md, {"session_key": ""})
            return md.collect()
        finally:
            await backend.aclose()

    out = asyncio.run(run())
    assert "high" in out
    assert runtime.cfg.thinking_level == "high"


def test_slash_thinking_unknown_level_returns_error(tmp_path):
    """Unknown levels are rejected without touching runtime."""
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)
    md = MarkdownRenderer()

    async def run():
        try:
            await handle_slash("/thinking turbo", backend, md, {"session_key": ""})
            return md.collect()
        finally:
            await backend.aclose()

    out = asyncio.run(run())
    assert "unknown thinking level" in out.lower()
    assert runtime.cfg.thinking_level == "off"  # untouched (LoopConfig default)


def test_resolve_model_option_disambiguation():
    """Bare model id matches one provider — accepted; provider/id form
    matches exact ref; unknown raises KeyError."""
    config = SimpleNamespace(
        models=SimpleNamespace(providers={
            "anthropic": SimpleNamespace(api="x", baseUrl=None, apiKey=None, models=[
                _model_def("claude-sonnet-4-5", name="Sonnet"),
            ]),
        })
    )
    found = _resolve_model_option(config, "claude-sonnet-4-5")
    assert found["ref"] == "anthropic/claude-sonnet-4-5"

    found2 = _resolve_model_option(config, "anthropic/claude-sonnet-4-5")
    assert found2["ref"] == "anthropic/claude-sonnet-4-5"

    with pytest.raises(KeyError):
        _resolve_model_option(config, "nope/none")
