"""Provider routing layer — public API for the rest of nano-openclaw.

Mirrors `src/agents/provider-transport-stream.ts`: a single entry-point
that dispatches to the right transport implementation based on the `api`
parameter, exactly as OpenClaw's switch(model.api) does.

Supported apis:
  "anthropic"  — Anthropic Messages API  (client: anthropic.Anthropic)
  "openai"     — OpenAI Chat Completions (client: openai.OpenAI)

Re-exports all StreamEvent types so callers (loop.py, cli.py) import from
one place and never touch the SDK-specific modules directly.
"""

from __future__ import annotations

from typing import Any, Iterator

# Re-export the shared event vocabulary — callers import from here.
from ._stream_events import (  # noqa: F401
    MessageEnd,
    StreamEvent,
    TextDelta,
    ThinkingBlockComplete,
    ThinkingDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
)
from . import _provider_anthropic, _provider_openai

SUPPORTED_APIS = ("anthropic", "openai")


def stream_response(
    *,
    api: str = "anthropic",
    client: Any,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    max_tokens: int = 4096,
    thinking_budget_tokens: int | None = None,
) -> Iterator[StreamEvent]:
    """Route a streaming completion request to the correct provider transport."""
    if api == "anthropic":
        return _provider_anthropic.stream_response(
            client=client,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            thinking_budget_tokens=thinking_budget_tokens,
        )
    if api == "openai":
        return _provider_openai.stream_response(
            client=client,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            thinking_budget_tokens=thinking_budget_tokens,
        )
    raise ValueError(f"unsupported api: {api!r}  (choose from: {SUPPORTED_APIS})")
