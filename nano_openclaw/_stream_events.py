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
    id: str
    partial_json: str


@dataclass
class ToolUseEnd:
    id: str


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


@dataclass
class MemoryExtracted:
    """Emitted by the stop-hook extractor after a successful save round.

    Lives here (rather than in loop.py) so frontends (TUI / WebUI / WeChat)
    can import the dataclass without taking a loop.py dependency. Phase 1
    only logs this event; Phase 2 wires it into the transcript renderer
    so the user sees a "Saved N memories" banner (claude-code parity with
    ``createMemorySavedMessage``).

    Fields:
        written_paths: All paths the extractor's guarded ``write_file``
            successfully wrote this round (topic files + the index).
        topic_paths: Subset of ``written_paths`` under ``memory/topics/``
            (i.e. excluding ``MEMORY.md`` — useful for "N memories saved"
            counting).
        duration_ms: Wall-clock duration of the subagent run.
    """
    written_paths: list[str]
    topic_paths: list[str]
    duration_ms: int


StreamEvent = Union[TextDelta, ToolUseStart, ToolUseDelta, ToolUseEnd, MessageEnd, ThinkingDelta, ThinkingBlockComplete]
