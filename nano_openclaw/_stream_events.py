"""Shared stream event dataclasses — the provider contract.

Both _provider_anthropic and _provider_openai translate their SDK's raw
SSE events into these 5 types. Everything above the provider layer
(loop.py, cli.py) speaks only this vocabulary, never the SDK directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolUseStart:
    id: str
    name: str


@dataclass
class ToolUseDelta:
    partial_json: str


@dataclass
class ToolUseEnd:
    pass


@dataclass
class MessageEnd:
    stop_reason: str
    usage: dict[str, Any]


@dataclass
class ThinkingDelta:
    text: str


@dataclass
class ThinkingBlockComplete:
    thinking: str   # full thinking text (empty for redacted blocks)
    signature: str  # thinking signature, or redacted_thinking data
    redacted: bool = False


StreamEvent = Union[TextDelta, ToolUseStart, ToolUseDelta, ToolUseEnd, MessageEnd, ThinkingDelta, ThinkingBlockComplete]
