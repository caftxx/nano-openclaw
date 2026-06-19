"""Tests for the extended ``models_list`` catalog + ``runtime_update`` lock.

Covers Phase 1 + Phase 2:

- ``EmbeddedBackend.models_list`` enumerates every model declared under
  ``config.models.providers``, marking the active runtime entry with
  ``is_default=True`` and exposing the new fields (name, input modalities,
  reasoning, max_tokens).
- ``RuntimeUpdateGuard.writer()`` is fail-fast: when a reader (chat /
  cron turn) holds the lock, the writer attempt raises ``BusyError``
  immediately rather than blocking. Exercised via the public path
  ``EmbeddedBackend.runtime_update``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nano_openclaw.services.backend import BusyError
from nano_openclaw.services.backend_embedded import EmbeddedBackend
from nano_openclaw.services.runs import RunRegistry
from nano_openclaw.services.runtime_update import RuntimeUpdateGuard
from nano_openclaw.core.loop import LoopConfig
from nano_openclaw.core.tools import ToolRegistry


def _model_def(model_id: str, *, name: str | None = None, inputs=("text",), reasoning=False, ctx=200000, max_tokens=8192):
    return SimpleNamespace(
        id=model_id,
        name=name,
        input=list(inputs),
        reasoning=reasoning,
        contextWindow=ctx,
        maxTokens=max_tokens,
    )


def _provider(api: str, models):
    return SimpleNamespace(api=api, baseUrl=None, apiKey=None, models=list(models))


def _fake_runtime(tmp_path: Path, *, providers: dict | None = None) -> SimpleNamespace:
    """A minimal AgentRuntime stand-in used for backend smoke tests.

    ``noTools=True`` so EmbeddedBackend skips runtime-tool registration; we
    test models_list / runtime_update purely.
    """
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
            models=SimpleNamespace(providers=providers or {}),
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


def test_models_list_enumerates_full_catalog(tmp_path):
    """Every model under ``config.models.providers.*.models`` gets a
    ModelChoice; the active runtime ref carries ``is_default=True`` exactly
    once, and the extended fields (name, input, reasoning, max_tokens)
    come through.
    """
    providers = {
        "anthropic": _provider("anthropic-messages", [
            _model_def("claude-sonnet-4-5", name="Claude Sonnet 4.5",
                       inputs=("text", "image"), reasoning=True, ctx=200000, max_tokens=8192),
            _model_def("claude-haiku-4-5", name="Claude Haiku 4.5", ctx=200000),
        ]),
        "openai": _provider("openai-completions", [
            _model_def("gpt-4o", name="GPT-4o", inputs=("text", "image", "audio")),
        ]),
    }
    runtime = _fake_runtime(tmp_path, providers=providers)
    backend = EmbeddedBackend(runtime)

    async def run():
        try:
            return await backend.models_list()
        finally:
            await backend.aclose()

    choices = asyncio.run(run())
    refs = [c.ref for c in choices]
    assert refs == [
        "anthropic/claude-sonnet-4-5",
        "anthropic/claude-haiku-4-5",
        "openai/gpt-4o",
    ]
    defaults = [c for c in choices if c.is_default]
    assert len(defaults) == 1
    assert defaults[0].ref == "anthropic/claude-sonnet-4-5"

    sonnet = choices[0]
    assert sonnet.name == "Claude Sonnet 4.5"
    assert sonnet.input == ("text", "image")
    assert sonnet.reasoning is True
    assert sonnet.max_tokens == 8192
    assert sonnet.context_window == 200000


def test_models_list_falls_back_to_runtime_when_providers_empty(tmp_path):
    """Empty providers map should still produce a single-entry list synthesized
    from the active runtime so ``/models`` is never blank."""
    runtime = _fake_runtime(tmp_path, providers={})
    backend = EmbeddedBackend(runtime)

    async def run():
        try:
            return await backend.models_list()
        finally:
            await backend.aclose()

    choices = asyncio.run(run())
    assert len(choices) == 1
    assert choices[0].ref == "anthropic/claude-sonnet-4-5"
    assert choices[0].is_default is True


def test_runtime_update_busy_when_reader_holds(tmp_path):
    """``RuntimeUpdateGuard.writer()`` is fail-fast: with a reader active
    (e.g. an in-flight chat turn), the writer raises BusyError immediately
    instead of blocking. We hold a reader manually and call
    ``runtime_update``; build_agent_runtime is never reached because the
    guard rejects up front.
    """
    runtime = _fake_runtime(tmp_path)
    backend = EmbeddedBackend(runtime)

    async def run():
        try:
            async with runtime.runtime_guard.reader():
                with pytest.raises(BusyError) as excinfo:
                    await backend.runtime_update(model_ref="anthropic/claude-haiku-4-5")
                assert "in flight" in str(excinfo.value).lower()
                assert excinfo.value.retry_after_ms > 0
        finally:
            await backend.aclose()

    asyncio.run(run())
