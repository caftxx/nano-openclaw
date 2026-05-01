"""Truncate tool results to prevent transcript file bloat.

Mirrors OpenClaw's `src/agents/session-tool-result-guard.ts`:
- MAX_PERSISTED_TOOL_RESULT_DETAILS_BYTES = 8_192
- MAX_PERSISTED_DETAIL_STRING_CHARS = 2_000
"""

from __future__ import annotations

from typing import Any

MAX_TOOL_RESULT_CHARS = 8_192


def truncate_tool_result(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Truncate text content in tool_result blocks if they exceed the limit.

    Returns a new list with truncated content. Non-text blocks (images, etc.)
    are preserved as-is and their size is counted towards the limit.
    """
    text_blocks = [b for b in content if b.get("type") == "text"]
    total_chars = sum(len(b.get("text", "")) for b in text_blocks)

    if total_chars <= MAX_TOOL_RESULT_CHARS:
        return content

    # Single text block: truncate directly.
    # Multiple text blocks: merge into one, then truncate.
    merged_text = "\n".join(b.get("text", "") for b in text_blocks)
    truncated_text = merged_text[:MAX_TOOL_RESULT_CHARS]
    omitted = total_chars - MAX_TOOL_RESULT_CHARS
    truncated_text += f"\n[nano truncated: {omitted:,} chars omitted]"

    # Rebuild content: truncated text first, then non-text blocks
    truncated = [{"type": "text", "text": truncated_text}]
    non_text = [b for b in content if b.get("type") != "text"]
    truncated.extend(non_text)
    return truncated
