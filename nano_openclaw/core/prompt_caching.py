"""Anthropic prompt caching ("system_and_3" strategy).

Reduces input token costs by ~75% on multi-turn conversations by caching
the conversation prefix. Uses up to 4 ``cache_control`` breakpoints — the
maximum Anthropic accepts per request:

  1. System prompt (stable across all turns)
  2-4. Last 3 non-system messages (rolling window)

The system prompt cache survives turns indefinitely (until edited).
The last-3 rolling window re-establishes within 1–2 turns after each
edit / compaction.

Pure functions — no class state, no AgentSession / AgentRuntime
dependency. The provider transport calls into this just before sending
the request, after compaction and message wiring are settled.

Ported from hermes-agent ``agent/prompt_caching.py``. The nano version
drops:

  * the ``native_anthropic`` flag (nano is always Anthropic-native; the
    OpenAI provider does not hit this code path)
  * the ``role == "tool"`` branch (nano stores tool_result blocks inside
    user messages, not separate tool-role messages)
"""

from __future__ import annotations

import copy
from typing import Any


def _apply_cache_marker(msg: dict[str, Any], marker: dict[str, Any]) -> None:
    """Attach ``cache_control`` to a single message, handling content shapes.

    The marker placement depends on what ``msg["content"]`` looks like:

      * ``None`` / missing / empty string → marker goes on the message itself
        (``msg["cache_control"]``)
      * string → content is converted to a list-of-blocks form so the marker
        can attach to the single text block
      * list of blocks → marker goes onto the last block
    """
    content = msg.get("content")

    if content is None or content == "":
        msg["cache_control"] = marker
        return

    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": marker}
        ]
        return

    if isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = marker


def apply_anthropic_cache_control(
    api_messages: list[dict[str, Any]],
    *,
    cache_ttl: str = "5m",
) -> list[dict[str, Any]]:
    """Apply the ``system_and_3`` cache strategy to a message list.

    Places up to 4 ``cache_control`` breakpoints: the leading system
    message (if any) plus the last 3 non-system messages.

    Args:
        api_messages: The wire-format message list about to be sent to
            the Anthropic Messages API. Should already include the
            system prompt as ``role: "system"`` if you want it cached
            via this list — note that nano's transport passes ``system``
            as a separate top-level field, so callers that want to cache
            the system prompt must handle it themselves (see
            ``_provider_anthropic.stream_response``).
        cache_ttl: Either ``"5m"`` (default, ephemeral) or ``"1h"``
            (long-lived, useful for sessions with multi-minute gaps).

    Returns:
        Deep copy of ``api_messages`` with ``cache_control`` markers
        injected. The input is not mutated, so cache markers don't bleed
        into the conversation history stored upstream.
    """
    messages = copy.deepcopy(api_messages)
    if not messages:
        return messages

    marker: dict[str, Any] = {"type": "ephemeral"}
    if cache_ttl == "1h":
        marker["ttl"] = "1h"

    breakpoints_used = 0

    if messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker)
        breakpoints_used += 1

    remaining = 4 - breakpoints_used
    non_sys_indices = [i for i in range(len(messages)) if messages[i].get("role") != "system"]
    for idx in non_sys_indices[-remaining:]:
        _apply_cache_marker(messages[idx], marker)

    return messages


def build_cacheable_system(system: str, *, cache_ttl: str = "5m") -> list[dict[str, Any]]:
    """Build a typed-block system prompt with cache_control attached.

    Anthropic's caching requires the system prompt to be in list-of-blocks
    form (``[{type:"text", text:..., cache_control:{...}}]``) — a bare
    string can't carry a cache marker.

    Args:
        system: The system prompt body.
        cache_ttl: ``"5m"`` (ephemeral, default) or ``"1h"`` (long-lived).

    Returns:
        A single-element list containing the text block with cache marker.
    """
    marker: dict[str, Any] = {"type": "ephemeral"}
    if cache_ttl == "1h":
        marker["ttl"] = "1h"
    return [{"type": "text", "text": system, "cache_control": marker}]
